"""
Bouncer - Risk Scoring Engine
風險評分引擎：基於多維度因素計算 AWS CLI 命令的風險分數

設計原則：
1. Fail-closed: 任何錯誤 fallback 到 manual 審批
2. 決策延遲 <50ms
3. 可測試性高（純函數 + 依賴注入）
4. 規則可配置（支援 JSON 載入）

評分公式：
    總分 = (動詞基礎分 × 40%) + (參數風險 × 30%) + (上下文 × 20%) + (帳號敏感度 × 10%)

分數區間：
    0-25:  自動批准 (auto_approve)
    26-45: 自動批准 + 詳細記錄 (log)
    46-65: 確認 reason 後可自動批准 (confirm)
    66-85: 需人工審批 (manual)
    86-100: 自動拒絕 (block)

Author: Bouncer Team
Version: 1.0.0
"""

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from aws_lambda_powertools import Logger
from utils import RiskFactor  # canonical definition in utils.py

logger = Logger(service="bouncer")

__all__ = [
    # Core types
    'RiskCategory',
    'RiskFactor',
    'RiskResult',
    'ParsedCommand',
    'RiskRules',
    # Core functions
    'calculate_risk',
    'load_risk_rules',
    'parse_command',
    # Scoring functions
    'score_verb',
    'score_parameters',
    'score_context',
    'score_account',
    # Utilities
    'get_category_from_score',
    'create_default_rules',
]


# ============================================================================
# Enums and Data Classes
# ============================================================================

class RiskCategory(Enum):
    """風險分類，決定審批流程"""
    AUTO_APPROVE = "auto_approve"  # 0-25: 自動批准
    LOG = "log"                     # 26-45: 自動批准但記錄
    CONFIRM = "confirm"             # 46-65: 確認 reason 後可自動批准
    MANUAL = "manual"               # 66-85: 需人工審批
    BLOCK = "block"                 # 86-100: 自動拒絕


@dataclass
class RiskResult:
    """
    風險評估結果

    Attributes:
        score: 最終風險分數 (0-100)
        category: 風險分類決定審批流程
        factors: 所有評分因素明細
        recommendation: 人類可讀的建議說明
        command: 原始命令
        parsed_command: 解析後的命令結構
        evaluation_time_ms: 評估耗時（毫秒）
        rule_version: 使用的規則版本
    """
    score: int
    category: str
    factors: list[RiskFactor]
    recommendation: str
    command: str = ""
    parsed_command: Optional['ParsedCommand'] = None
    evaluation_time_ms: float = 0.0
    rule_version: str = "1.0.0"

    def __post_init__(self):
        """確保分數在有效範圍內"""
        self.score = max(0, min(100, self.score))

    def to_dict(self) -> dict[str, Any]:
        """轉換為字典格式（方便 JSON 序列化）"""
        return {
            'score': self.score,
            'category': self.category,
            'factors': [
                {
                    'name': f.name,
                    'category': f.category,
                    'raw_score': f.raw_score,
                    'weighted_score': f.weighted_score,
                    'weight': f.weight,
                    'details': f.details,
                }
                for f in self.factors
            ],
            'recommendation': self.recommendation,
            'command': self.command,
            'evaluation_time_ms': self.evaluation_time_ms,
            'rule_version': self.rule_version,
        }


@dataclass
class ParsedCommand:
    """
    解析後的 AWS CLI 命令結構

    Attributes:
        original: 原始命令字串
        service: AWS 服務名稱 (e.g., 'ec2', 's3', 'iam')
        action: 操作名稱 (e.g., 'describe-instances', 'delete-bucket')
        verb: 動詞部分 (e.g., 'describe', 'delete', 'create')
        resource_type: 資源類型 (e.g., 'instances', 'bucket')
        parameters: 參數字典 {參數名: 值}
        flags: 旗標列表 (e.g., ['--force', '--yes'])
        targets: 目標資源列表 (instance IDs, bucket names, etc.)
        is_valid: 是否為有效的 AWS CLI 命令
        parse_error: 解析錯誤訊息（如果有）
    """
    original: str
    service: str = ""
    action: str = ""
    verb: str = ""
    resource_type: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    is_valid: bool = True
    parse_error: Optional[str] = None

    def __post_init__(self):
        """自動從 action 提取 verb 和 resource_type"""
        if self.action and not self.verb:
            parts = self.action.split('-', 1)
            self.verb = parts[0] if parts else ""
            self.resource_type = parts[1] if len(parts) > 1 else ""


@dataclass
class RiskRules:
    """
    風險評估規則配置

    可從 JSON 檔案載入，支援熱更新

    Attributes:
        version: 規則版本
        verb_scores: 動詞基礎分數 {verb: score}
        service_scores: 服務敏感度分數 {service: score}
        parameter_patterns: 參數風險模式 [{pattern, score, description}]
        dangerous_flags: 危險旗標列表 {flag: score}
        blocked_patterns: 黑名單模式（觸發即 100 分）
        account_sensitivity: 帳號敏感度配置 {account_id: score}
        context_rules: 上下文規則 [{condition, score_modifier}]
        weights: 各維度權重 {verb, parameter, context, account}
    """
    version: str = "1.0.0"
    verb_scores: dict[str, int] = field(default_factory=dict)
    service_scores: dict[str, int] = field(default_factory=dict)
    parameter_patterns: list[dict] = field(default_factory=list)
    dangerous_flags: dict[str, int] = field(default_factory=dict)
    blocked_patterns: list[str] = field(default_factory=list)
    account_sensitivity: dict[str, int] = field(default_factory=dict)
    context_rules: list[dict] = field(default_factory=list)
    template_rules: list[dict] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=lambda: {
        'verb': 0.40,
        'parameter': 0.30,
        'context': 0.20,
        'account': 0.10,
    })

    def validate(self) -> tuple[bool, list[str]]:
        """
        驗證規則配置是否有效

        Returns:
            (is_valid, error_messages)
        """
        errors = []

        # 檢查權重總和
        weight_sum = sum(self.weights.values())
        if abs(weight_sum - 1.0) > 0.01:
            errors.append(f"權重總和必須為 1.0，目前為 {weight_sum}")

        # 檢查分數範圍
        for verb, score in self.verb_scores.items():
            if not 0 <= score <= 100:
                errors.append(f"動詞 '{verb}' 分數 {score} 超出範圍 [0, 100]")

        for service, score in self.service_scores.items():
            if not 0 <= score <= 100:
                errors.append(f"服務 '{service}' 分數 {score} 超出範圍 [0, 100]")

        return len(errors) == 0, errors


# ============================================================================
# Default Rules (Inline Fallback)
# ============================================================================

def create_default_rules() -> RiskRules:
    """
    建立預設風險規則

    從 data/risk-rules.json 載入。如果找不到或解析失敗，
    回傳最小可用的 hardcoded fallback（fail-closed: 高風險）。
    """
    default_paths = [
        Path(__file__).parent / 'data' / 'risk-rules.json',
        Path('data/risk-rules.json'),
    ]

    for p in default_paths:
        try:
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                rules = _dict_to_rules(data)
                rules.version = data.get('version', '1.0.0-json')
                return rules
        except (OSError, json.JSONDecodeError) as e:
            logger.exception("Failed to load %s: %s", p, e, extra={"src_module": "risk_scorer", "operation": "load_risk_rules", "path": str(p), "error": str(e)})

    # Hardcoded fallback — 最小化，fail-closed（未知命令 = 高風險）
    logger.warning("No risk-rules.json found, using minimal fallback", extra={"src_module": "risk_scorer", "operation": "load_risk_rules"})
    return RiskRules(
        version="1.0.0-fallback",
        verb_scores={'describe': 0, 'list': 0, 'get': 5},
        service_scores={'iam': 95, 'sts': 85},
        parameter_patterns=[],
        dangerous_flags={},
        blocked_patterns=[],
        account_sensitivity={},
        context_rules=[],
        weights={'verb': 0.40, 'parameter': 0.30, 'context': 0.20, 'account': 0.10},
    )


# ============================================================================
# Rule Loading
# ============================================================================

# 全域規則快取
_rules_cache: Optional[RiskRules] = None
_rules_cache_time: float = 0
_CACHE_TTL = 300  # 5 分鐘快取


def load_risk_rules(
    path: Optional[str] = None,
    use_cache: bool = True,
) -> RiskRules:
    """
    從 JSON 檔案載入風險規則

    Args:
        path: JSON 檔案路徑（可選）
               如果不提供，會嘗試載入預設路徑：
               - data/risk-rules.json
               - /opt/bouncer/risk-rules.json
        use_cache: 是否使用快取（預設 True）

    Returns:
        RiskRules 物件

    Note:
        Fail-closed: 任何載入錯誤都會回傳預設規則
    """
    global _rules_cache, _rules_cache_time

    # 檢查快取
    if use_cache and _rules_cache is not None:
        if time.time() - _rules_cache_time < _CACHE_TTL:
            return _rules_cache

    # 嘗試載入外部規則
    try:
        rules = _load_rules_from_file(path)

        # 驗證規則
        is_valid, errors = rules.validate()
        if not is_valid:
            logger.warning("Rule validation errors: %s", errors, extra={"src_module": "risk_scorer", "operation": "validate_rules", "error_count": len(errors)})
            # 仍然使用載入的規則，但記錄警告

        # 更新快取
        _rules_cache = rules
        _rules_cache_time = time.time()

        return rules

    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to load rules, using defaults: %s", e, extra={"src_module": "risk_scorer", "operation": "load_risk_rules", "error": str(e)})
        return create_default_rules()


def _load_rules_from_file(path: Optional[str] = None) -> RiskRules:
    """
    從檔案載入規則（內部函數）

    Args:
        path: 明確指定的路徑

    Returns:
        RiskRules 物件

    Raises:
        FileNotFoundError: 找不到規則檔
        json.JSONDecodeError: JSON 格式錯誤
    """
    # 嘗試路徑列表
    paths_to_try = []

    if path:
        paths_to_try.append(Path(path))

    # 預設路徑
    paths_to_try.extend([
        Path(__file__).parent / 'data' / 'risk-rules.json',
        Path('/opt/bouncer/risk-rules.json'),
        Path('data/risk-rules.json'),
    ])

    for p in paths_to_try:
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _dict_to_rules(data)

    raise FileNotFoundError(f"No risk rules file found in: {paths_to_try}")


def _dict_to_rules(data: dict) -> RiskRules:
    """將字典轉換為 RiskRules 物件"""
    return RiskRules(
        version=data.get('version', '1.0.0'),
        verb_scores=data.get('verb_scores', {}),
        service_scores=data.get('service_scores', {}),
        parameter_patterns=data.get('parameter_patterns', []),
        dangerous_flags=data.get('dangerous_flags', {}),
        blocked_patterns=data.get('blocked_patterns', []),
        account_sensitivity=data.get('account_sensitivity', {}),
        context_rules=data.get('context_rules', []),
        template_rules=data.get('template_rules', []),
        weights=data.get('weights', {
            'verb': 0.40,
            'parameter': 0.30,
            'context': 0.20,
            'account': 0.10,
        }),
    )


# ============================================================================
# Command Parsing
# ============================================================================

def parse_command(command: str) -> ParsedCommand:
    """
    解析 AWS CLI 命令

    Args:
        command: AWS CLI 命令字串
                 例如: "aws ec2 describe-instances --instance-ids i-1234567890abcdef0"

    Returns:
        ParsedCommand 物件，包含解析後的結構化資訊

    Note:
        此函數不會拋出異常，解析失敗會在 ParsedCommand.is_valid 標記為 False
    """
    try:
        original = command.strip()

        # 基本驗證
        if not original:
            return ParsedCommand(
                original=original,
                is_valid=False,
                parse_error="Empty command",
            )

        # 移除可能的前綴
        cmd = original
        if cmd.startswith('aws '):
            cmd = cmd[4:]

        # 處理 --query 參數中可能包含的特殊字元
        cmd_for_parsing = re.sub(
            r"--query\s+['\"].*?['\"]",
            "--query REDACTED",
            cmd
        )

        # 分割命令
        parts = cmd_for_parsing.split()
        if not parts:
            return ParsedCommand(
                original=original,
                is_valid=False,
                parse_error="No command parts after splitting",
            )

        # 提取服務和動作
        service = parts[0] if parts else ""
        action = parts[1] if len(parts) > 1 else ""

        # 解析動詞和資源類型
        verb = ""
        resource_type = ""
        if action:
            action_parts = action.split('-', 1)
            verb = action_parts[0]
            resource_type = action_parts[1] if len(action_parts) > 1 else ""

        # 解析參數和旗標
        parameters = {}
        flags = []
        targets = []

        i = 2
        while i < len(parts):
            part = parts[i]

            if part.startswith('--'):
                # 檢查是否是布林旗標（沒有值的參數）
                if i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                    # 有值的參數
                    param_name = part[2:]  # 移除 --
                    param_value = parts[i + 1]
                    parameters[param_name] = param_value

                    # 提取目標資源
                    if _is_target_parameter(param_name, param_value):
                        targets.append(param_value)

                    i += 2
                else:
                    # 布林旗標
                    flags.append(part)
                    i += 1
            elif part.startswith('-') and len(part) == 2:
                # 短旗標 (如 -r, -y)
                flags.append(part)
                i += 1
            else:
                # 可能是位置參數（如 s3 cp 的路徑）
                targets.append(part)
                i += 1

        return ParsedCommand(
            original=original,
            service=service,
            action=action,
            verb=verb,
            resource_type=resource_type,
            parameters=parameters,
            flags=flags,
            targets=targets,
            is_valid=True,
        )

    except Exception as e:  # noqa: BLE001
        return ParsedCommand(
            original=command,
            is_valid=False,
            parse_error=str(e),
        )


def _is_target_parameter(param_name: str, param_value: str) -> bool:
    """檢查參數是否是目標資源"""
    target_params = {
        'instance-ids', 'instance-id',
        'bucket', 'bucket-name',
        'table-name',
        'function-name',
        'cluster-name',
        'db-instance-identifier',
        'log-group-name',
        'queue-url', 'queue-name',
        'topic-arn',
        'secret-id',
        'key-id',
        'stack-name',
        'role-name', 'role-arn',
        'user-name',
    }
    return param_name.lower() in target_params


# ============================================================================
# Scoring Functions
# ============================================================================

def score_verb(
    parsed: ParsedCommand,
    rules: RiskRules,
) -> tuple[int, list[RiskFactor]]:
    """
    計算動詞基礎分數

    基於命令的動詞（describe, delete, etc.）和服務（iam, ec2, etc.）

    Args:
        parsed: 解析後的命令
        rules: 風險規則

    Returns:
        (raw_score, factors) - 原始分數和評分因素列表
    """
    factors = []

    # 1. 檢查黑名單（立即回傳 100）
    cmd_lower = parsed.original.lower()
    for pattern in rules.blocked_patterns:
        if re.search(pattern, cmd_lower):
            factors.append(RiskFactor(
                name=f"Blocked pattern: {pattern}",
                category="verb",
                raw_score=100,
                weighted_score=100 * rules.weights['verb'],
                weight=rules.weights['verb'],
                details="Command matches blocked pattern - immediate rejection",
            ))
            return 100, factors

    # 2. 動詞分數
    verb_score = rules.verb_scores.get(parsed.verb, 50)  # 未知動詞預設 50
    factors.append(RiskFactor(
        name=f"Verb: {parsed.verb}",
        category="verb",
        raw_score=verb_score,
        weighted_score=0,  # 稍後計算
        weight=0.6,  # 動詞佔 verb 分數的 60%
        details=f"Base score for verb '{parsed.verb}'",
    ))

    # 3. 服務分數
    service_score = rules.service_scores.get(parsed.service, 40)  # 未知服務預設 40
    factors.append(RiskFactor(
        name=f"Service: {parsed.service}",
        category="verb",
        raw_score=service_score,
        weighted_score=0,
        weight=0.4,  # 服務佔 verb 分數的 40%
        details=f"Sensitivity score for service '{parsed.service}'",
    ))

    # 計算組合分數：verb 60% + service 40%
    combined_score = int(verb_score * 0.6 + service_score * 0.4)

    return combined_score, factors


def score_parameters(
    parsed: ParsedCommand,
    rules: RiskRules,
) -> tuple[int, list[RiskFactor]]:
    """
    計算參數風險分數

    基於命令中的參數和旗標

    Args:
        parsed: 解析後的命令
        rules: 風險規則

    Returns:
        (raw_score, factors) - 原始分數和評分因素列表
    """
    factors = []
    max_pattern_score = 0
    flag_score_total = 0

    cmd_str = parsed.original.lower()

    # 1. 檢查參數模式
    for pattern_rule in rules.parameter_patterns:
        pattern = pattern_rule.get('pattern', '')
        score = pattern_rule.get('score', 0)
        description = pattern_rule.get('description', pattern)

        if re.search(pattern, cmd_str, re.IGNORECASE):
            factors.append(RiskFactor(
                name=f"Parameter pattern: {description}",
                category="parameter",
                raw_score=score,
                weighted_score=0,
                weight=0,
                details=f"Matched pattern: {pattern}",
            ))
            max_pattern_score = max(max_pattern_score, score)

    # 2. 檢查危險旗標
    for flag in parsed.flags:
        flag_lower = flag.lower()
        if flag_lower in rules.dangerous_flags:
            flag_score = rules.dangerous_flags[flag_lower]
            factors.append(RiskFactor(
                name=f"Dangerous flag: {flag}",
                category="parameter",
                raw_score=flag_score,
                weighted_score=0,
                weight=0,
                details=f"Flag '{flag}' adds risk",
            ))
            flag_score_total += flag_score

    # 3. 計算組合分數
    # 使用最高參數模式分數 + 旗標分數（上限 100）
    combined_score = min(100, max_pattern_score + flag_score_total)

    # 4. Template scanning (Phase 4)
    try:
        from template_scanner import scan_command_payloads
        template_rules = rules.template_rules if hasattr(rules, 'template_rules') else []
        template_score, template_factors = scan_command_payloads(
            parsed.original, template_rules,
        )
        if template_factors:
            factors.extend(template_factors)
            max_pattern_score = max(max_pattern_score, template_score)
            combined_score = min(100, max(combined_score, max_pattern_score + flag_score_total))
    except Exception as e:  # noqa: BLE001
        logger.exception("Template scan error: %s", e, extra={"src_module": "risk_scorer", "operation": "template_scan", "error": str(e)})
        # Fail-open: don't change score on scanner error

    # 如果沒有匹配任何模式或旗標，給予基礎分數
    if not factors:
        combined_score = 20  # 基礎參數分數
        factors.append(RiskFactor(
            name="No risky parameters detected",
            category="parameter",
            raw_score=20,
            weighted_score=0,
            weight=1.0,
            details="Default score for commands without risky parameters",
        ))

    return combined_score, factors


def score_context(
    reason: str,
    source: str,
    rules: RiskRules,
) -> tuple[int, list[RiskFactor]]:
    """
    計算上下文風險分數

    基於 reason 和 source 等上下文資訊

    Args:
        reason: 執行原因
        source: 請求來源
        rules: 風險規則

    Returns:
        (raw_score, factors) - 原始分數和評分因素列表
    """
    factors = []
    base_score = 30  # 基礎上下文分數
    modifier = 0

    reason_lower = (reason or "").lower().strip()
    source_lower = (source or "").lower().strip()

    # 已知來源的基礎分數更低
    known_sources = ['private bot', 'public bot', 'steven', 'openclaw']
    is_known_source = any(ks in source_lower for ks in known_sources)
    base_score = 20 if is_known_source else 30

    for rule in rules.context_rules:
        condition = rule.get('condition', '')
        score_mod = rule.get('score_modifier', 0)
        description = rule.get('description', condition)

        matched = False

        if condition == 'reason_empty':
            matched = not reason_lower
        elif condition == 'reason_short':
            threshold = rule.get('threshold', 10)
            matched = len(reason_lower) < threshold and reason_lower
        elif condition == 'source_unknown':
            matched = not source_lower or source_lower == 'unknown'
        elif condition == 'after_hours':
            # 簡化：假設 UTC 22:00-06:00 是非工作時間
            import datetime
            hour = datetime.datetime.utcnow().hour
            matched = hour >= 22 or hour < 6
        elif condition == 'reason_contains_keywords':
            keywords = rule.get('keywords', [])
            matched = any(kw in reason_lower for kw in keywords)

        if matched:
            factors.append(RiskFactor(
                name=description,
                category="context",
                raw_score=abs(score_mod),
                weighted_score=0,
                weight=0,
                details=f"Condition '{condition}' matched, modifier: {score_mod:+d}",
            ))
            modifier += score_mod

    final_score = max(0, min(100, base_score + modifier))

    # 如果沒有任何規則匹配，記錄預設分數
    if not factors:
        factors.append(RiskFactor(
            name="Default context score",
            category="context",
            raw_score=base_score,
            weighted_score=0,
            weight=1.0,
            details="No context rules matched",
        ))

    return final_score, factors


def score_account(
    account_id: str,
    rules: RiskRules,
) -> tuple[int, list[RiskFactor]]:
    """
    計算帳號敏感度分數

    基於帳號的配置（production vs dev）

    Args:
        account_id: AWS 帳號 ID
        rules: 風險規則

    Returns:
        (raw_score, factors) - 原始分數和評分因素列表
    """
    factors = []

    # 查找帳號敏感度配置
    if account_id in rules.account_sensitivity:
        score = rules.account_sensitivity[account_id]
        factors.append(RiskFactor(
            name=f"Account sensitivity: {account_id}",
            category="account",
            raw_score=score,
            weighted_score=0,
            weight=1.0,
            details=f"Configured sensitivity for account {account_id}",
        ))
        return score, factors

    # 未配置的帳號使用預設分數
    default_score = 40  # 中等敏感度
    factors.append(RiskFactor(
        name=f"Unknown account: {account_id}",
        category="account",
        raw_score=default_score,
        weighted_score=0,
        weight=1.0,
        details="Using default sensitivity for unconfigured account",
    ))

    return default_score, factors


# ============================================================================
# Main Scoring Function
# ============================================================================

def get_category_from_score(score: int) -> RiskCategory:
    """
    根據分數決定風險分類

    Args:
        score: 風險分數 (0-100)

    Returns:
        RiskCategory enum
    """
    if score <= 25:
        return RiskCategory.AUTO_APPROVE
    elif score <= 45:
        return RiskCategory.LOG
    elif score <= 65:
        return RiskCategory.CONFIRM
    elif score <= 85:
        return RiskCategory.MANUAL
    else:
        return RiskCategory.BLOCK


def _generate_recommendation(
    score: int,
    category: str,
    parsed: ParsedCommand,
    factors: list[RiskFactor],
) -> str:
    """
    生成人類可讀的建議

    Args:
        score: 風險分數
        category: 風險分類
        parsed: 解析後的命令
        factors: 評分因素列表

    Returns:
        建議字串
    """
    # 基礎訊息
    base_messages = {
        RiskCategory.AUTO_APPROVE: f"✅ 低風險操作 ({score}分)，可自動批准",
        RiskCategory.LOG: f"📝 低風險操作 ({score}分)，建議自動批准並記錄",
        RiskCategory.CONFIRM: f"⚠️ 中等風險 ({score}分)，請確認 reason 後可批准",
        RiskCategory.MANUAL: f"🔒 高風險操作 ({score}分)，需要人工審批",
        RiskCategory.BLOCK: f"🚫 危險操作 ({score}分)，建議自動拒絕",
    }

    message = base_messages.get(category, f"風險分數: {score}")

    # 附加關鍵因素
    high_risk_factors = [f for f in factors if f.raw_score >= 60]
    if high_risk_factors:
        top_factors = high_risk_factors[:3]
        factor_names = [f.name for f in top_factors]
        message += f"\n主要風險: {', '.join(factor_names)}"

    return message


def calculate_risk(
    command: str,
    reason: str = "",
    source: str = "",
    account_id: str = "",
    rules: Optional[RiskRules] = None,
) -> RiskResult:
    """
    計算 AWS CLI 命令的風險分數

    這是風險評分引擎的主要入口函數。

    Args:
        command: AWS CLI 命令
        reason: 執行原因（用於上下文評分）
        source: 請求來源（用於上下文評分）
        account_id: 目標 AWS 帳號 ID（用於帳號敏感度評分）
        rules: 可選的風險規則（用於測試或自定義規則）

    Returns:
        RiskResult 物件，包含：
        - score: 0-100 的風險分數
        - category: 風險分類
        - factors: 評分因素明細
        - recommendation: 人類可讀建議

    Note:
        Fail-closed: 任何錯誤都會回傳 manual 分類（分數 70）

    Example:
        >>> result = calculate_risk(
        ...     command="aws s3 ls",
        ...     reason="List buckets for inventory",
        ...     source="Steven's Private Bot",
        ...     account_id="111111111111"
        ... )
        >>> print(result.category)
        'auto_approve'
        >>> print(result.score)
        15
    """
    start_time = time.perf_counter()

    try:
        # 載入規則
        if rules is None:
            rules = load_risk_rules()

        # 解析命令
        parsed = parse_command(command)

        if not parsed.is_valid:
            # 解析失敗 → Fail-closed
            return RiskResult(
                score=70,
                category=RiskCategory.MANUAL,
                factors=[RiskFactor(
                    name="Parse error",
                    category="error",
                    raw_score=70,
                    weighted_score=70,
                    weight=1.0,
                    details=f"Failed to parse command: {parsed.parse_error}",
                )],
                recommendation="⚠️ 命令解析失敗，需要人工審批",
                command=command,
                parsed_command=parsed,
                evaluation_time_ms=(time.perf_counter() - start_time) * 1000,
                rule_version=rules.version,
            )

        # 收集所有因素
        all_factors: list[RiskFactor] = []

        # 1. 動詞基礎分數 (40%)
        verb_score, verb_factors = score_verb(parsed, rules)
        all_factors.extend(verb_factors)

        # 檢查是否觸發黑名單
        if verb_score >= 100:
            return RiskResult(
                score=100,
                category=RiskCategory.BLOCK,
                factors=verb_factors,
                recommendation="🚫 命令被封鎖：觸發安全規則",
                command=command,
                parsed_command=parsed,
                evaluation_time_ms=(time.perf_counter() - start_time) * 1000,
                rule_version=rules.version,
            )

        # 2. 參數風險分數 (30%)
        param_score, param_factors = score_parameters(parsed, rules)
        all_factors.extend(param_factors)

        # 3. 上下文分數 (20%)
        context_score, context_factors = score_context(reason, source, rules)
        all_factors.extend(context_factors)

        # 4. 帳號敏感度分數 (10%)
        account_score, account_factors = score_account(account_id, rules)
        all_factors.extend(account_factors)

        # 計算加權總分
        weights = rules.weights
        final_score = int(
            verb_score * weights['verb'] +
            param_score * weights['parameter'] +
            context_score * weights['context'] +
            account_score * weights['account']
        )

        # 確保分數在有效範圍
        final_score = max(0, min(100, final_score))

        # 決定分類
        category = get_category_from_score(final_score)

        # 更新因素的加權分數
        for factor in all_factors:
            if factor.category == 'verb':
                factor.weighted_score = factor.raw_score * weights['verb'] * factor.weight
            elif factor.category == 'parameter':
                factor.weighted_score = factor.raw_score * weights['parameter']
            elif factor.category == 'context':
                factor.weighted_score = factor.raw_score * weights['context']
            elif factor.category == 'account':
                factor.weighted_score = factor.raw_score * weights['account']

        # 生成建議
        recommendation = _generate_recommendation(
            final_score, category, parsed, all_factors
        )

        return RiskResult(
            score=final_score,
            category=category,
            factors=all_factors,
            recommendation=recommendation,
            command=command,
            parsed_command=parsed,
            evaluation_time_ms=(time.perf_counter() - start_time) * 1000,
            rule_version=rules.version,
        )

    except Exception as e:  # noqa: BLE001
        # Fail-closed: 任何錯誤都回傳 manual
        return RiskResult(
            score=70,
            category=RiskCategory.MANUAL,
            factors=[RiskFactor(
                name="Scoring error",
                category="error",
                raw_score=70,
                weighted_score=70,
                weight=1.0,
                details=f"Error during risk scoring: {str(e)}",
            )],
            recommendation=f"⚠️ 風險評估失敗 ({str(e)})，需要人工審批",
            command=command,
            evaluation_time_ms=(time.perf_counter() - start_time) * 1000,
            rule_version=rules.version if rules else "unknown",
        )
