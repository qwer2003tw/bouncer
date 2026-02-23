"""
Bouncer - Template/Payload Scanner (Phase 4)
分析 AWS CLI 命令中的 inline JSON payload，進行深度風險分析

設計原則：
1. Fail-open for extraction（JSON 解析失敗不影響分數）
2. Fail-closed for scoring（找到風險就加分）
3. 獨立模組，由 risk_scorer 呼叫
4. 所有 check 函數為純函數，易於測試

Check IDs:
    TP-001: action_wildcard       - IAM Policy Action:*
    TP-002: resource_wildcard     - IAM Policy Resource:*
    TP-003: principal_wildcard    - Trust/Bucket Policy Principal:*
    TP-004: external_account_trust - Trust Policy 含外部帳號
    TP-005: open_ingress          - Security Group 0.0.0.0/0
    TP-006: high_risk_port        - SG 高危端口 + 0.0.0.0/0
    TP-007: hardcoded_secret      - Lambda 環境變數含密碼
    TP-008: admin_policy          - 完全管理員 (Action:* + Resource:* + Allow)
    TP-009: public_access         - S3/Policy Principal:* + Allow
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from risk_scorer import RiskFactor

__all__ = [
    'extract_json_payloads',
    'scan_payload',
    'scan_command_payloads',
]

# ============================================================================
# Constants
# ============================================================================

# 需要掃描的 CLI 參數
TARGET_PARAMETERS = [
    '--policy-document',
    '--assume-role-policy-document',
    '--policy',
    '--ip-permissions',
    '--template-body',
    '--cli-input-json',
    '--environment',
]

# 已知 AWS 帳號 (TP-004 排除清單)
KNOWN_ACCOUNT_IDS = {
    '190825685292',  # Default/2nd
    '992382394211',  # Dev
    '841882238387',  # 1st
}

# 高危端口 (TP-006)
HIGH_RISK_PORTS = {22, 3389, 3306, 1433, 5432, 27017}

# 密碼/金鑰模式 (TP-007)
SECRET_KEY_PATTERN = re.compile(
    r'(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY)',
    re.IGNORECASE,
)


# ============================================================================
# JSON Payload Extraction
# ============================================================================

def extract_json_payloads(command: str) -> List[Tuple[str, dict]]:
    """
    從 AWS CLI 命令中提取 JSON payload

    解析策略：
    1. 尋找目標參數（如 --policy-document）
    2. 提取參數後方的 JSON 字串
    3. 支援單引號、雙引號、file:// 忽略、直接 JSON

    Args:
        command: AWS CLI 命令字串

    Returns:
        list of (param_name, parsed_json) tuples
        解析失敗回傳空 list（fail-open）
    """
    if not command or not isinstance(command, str):
        return []

    results = []

    for param in TARGET_PARAMETERS:
        try:
            payloads = _extract_param_json(command, param)
            for payload in payloads:
                results.append((param, payload))
        except Exception:
            # Fail-open: 解析失敗跳過此參數
            continue

    return results


def _extract_param_json(command: str, param: str) -> List[dict]:
    """
    從命令中提取指定參數的 JSON 值

    支援格式：
    - --param '{"key": "value"}'
    - --param "{"key": "value"}"
    - --param {"key": "value"}
    - --param '[...]'
    - 跳過 file:// 前綴

    Returns:
        解析成功的 JSON 物件列表
    """
    results = []

    # 找到參數位置（不區分大小寫不適合，CLI 參數通常是固定的）
    idx = command.find(param)
    while idx != -1:
        # 取得參數後面的內容
        after = command[idx + len(param):].lstrip()

        if not after:
            break

        # 跳過 file:// 前綴
        if after.startswith('file://'):
            idx = command.find(param, idx + len(param))
            continue

        # 嘗試提取 JSON
        json_str = _extract_json_string(after)
        if json_str is not None:
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, (dict, list)):
                    results.append(parsed)
            except (json.JSONDecodeError, ValueError):
                pass

        idx = command.find(param, idx + len(param))

    return results


def _extract_json_string(text: str) -> Optional[str]:
    """
    從文字開頭提取 JSON 字串

    支援：
    - 單引號包裹: '...'
    - 雙引號包裹: "..."
    - 裸 JSON: {...} 或 [...]
    """
    if not text:
        return None

    # 單引號包裹
    if text.startswith("'"):
        end = _find_matching_quote(text, "'")
        if end > 0:
            return text[1:end]

    # 雙引號包裹（但要區分 JSON 內部雙引號）
    if text.startswith('"'):
        # 檢查是否是 "{ 開頭（JSON 包在引號裡）
        if len(text) > 1 and text[1] in '{[':
            end = _find_matching_quote(text, '"')
            if end > 0:
                return text[1:end]

    # 裸 JSON: { 或 [ 開頭
    if text.startswith('{'):
        return _extract_balanced(text, '{', '}')
    if text.startswith('['):
        return _extract_balanced(text, '[', ']')

    return None


def _find_matching_quote(text: str, quote: str) -> int:
    """找到匹配的結束引號位置（跳過轉義）"""
    i = 1
    while i < len(text):
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return -1


def _extract_balanced(text: str, open_char: str, close_char: str) -> Optional[str]:
    """提取平衡的括號內容"""
    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[:i + 1]
        # 遇到空白且 depth=0 表示 JSON 結束（不應發生因為 depth 從 1 開始）
        # 遇到下一個 -- 參數且不在 JSON 內，表示 JSON 未正確結束
        if depth == 0 and i > 0:
            break

    return None


# ============================================================================
# Check Functions
# ============================================================================

def _is_wildcard(value: Any) -> bool:
    """檢查值是否為萬用字元 '*'"""
    if value == '*':
        return True
    if isinstance(value, list) and len(value) == 1 and value[0] == '*':
        return True
    return False


def _iter_statements(payload: Any) -> List[dict]:
    """從 policy payload 中迭代所有 Statement"""
    statements = []
    if isinstance(payload, dict):
        stmts = payload.get('Statement', [])
        if isinstance(stmts, dict):
            stmts = [stmts]
        if isinstance(stmts, list):
            statements.extend(s for s in stmts if isinstance(s, dict))
    return statements


def check_action_wildcard(payload: dict) -> Optional[Tuple[int, str]]:
    """
    TP-001: 檢查 IAM Policy 中的 Action:*

    Returns:
        (score, description) if found, None otherwise
    """
    for stmt in _iter_statements(payload):
        action = stmt.get('Action', stmt.get('action'))
        if _is_wildcard(action):
            return (90, "IAM Policy contains Action:* (TP-001)")
    return None


def check_resource_wildcard(payload: dict) -> Optional[Tuple[int, str]]:
    """
    TP-002: 檢查 IAM Policy 中的 Resource:*

    Returns:
        (score, description) if found, None otherwise
    """
    for stmt in _iter_statements(payload):
        resource = stmt.get('Resource', stmt.get('resource'))
        if _is_wildcard(resource):
            return (85, "IAM Policy contains Resource:* (TP-002)")
    return None


def check_principal_wildcard(payload: dict) -> Optional[Tuple[int, str]]:
    """
    TP-003: 檢查 Trust/Bucket Policy 中的 Principal:*

    包括：
    - "Principal": "*"
    - "Principal": {"AWS": "*"}
    """
    for stmt in _iter_statements(payload):
        principal = stmt.get('Principal', stmt.get('principal'))
        if principal == '*':
            return (90, "Policy contains Principal:* (TP-003)")
        if isinstance(principal, dict):
            aws_principal = principal.get('AWS', principal.get('aws'))
            if _is_wildcard(aws_principal):
                return (90, "Policy contains Principal AWS:* (TP-003)")
    return None


def check_external_account_trust(
    payload: dict,
    known_accounts: Optional[set] = None,
) -> Optional[Tuple[int, str]]:
    """
    TP-004: 檢查 Trust Policy 含外部 AWS 帳號

    Args:
        payload: Trust policy JSON
        known_accounts: 已知帳號 ID 集合

    Returns:
        (score, description) if external account found, None otherwise
    """
    if known_accounts is None:
        known_accounts = KNOWN_ACCOUNT_IDS

    account_pattern = re.compile(r'arn:aws:iam::(\d{12}):')

    for stmt in _iter_statements(payload):
        principal = stmt.get('Principal', stmt.get('principal'))
        arns = _extract_principal_arns(principal)

        for arn in arns:
            match = account_pattern.search(arn)
            if match:
                account_id = match.group(1)
                if account_id not in known_accounts:
                    return (
                        80,
                        f"Trust policy references external account "
                        f"{account_id} (TP-004)",
                    )
    return None


def _extract_principal_arns(principal: Any) -> List[str]:
    """從 Principal 值中提取所有 ARN"""
    arns = []
    if isinstance(principal, str):
        arns.append(principal)
    elif isinstance(principal, list):
        arns.extend(p for p in principal if isinstance(p, str))
    elif isinstance(principal, dict):
        for key in ('AWS', 'aws', 'Service', 'Federated'):
            val = principal.get(key)
            if isinstance(val, str):
                arns.append(val)
            elif isinstance(val, list):
                arns.extend(v for v in val if isinstance(v, str))
    return arns


def check_open_ingress(payload: Any) -> Optional[Tuple[int, str]]:
    """
    TP-005: 檢查 Security Group 是否開放 0.0.0.0/0 或 ::/0

    payload 可能是：
    - 單一 ip-permissions dict
    - ip-permissions list
    """
    permissions = _normalize_ip_permissions(payload)

    for perm in permissions:
        # 檢查 IPv4
        for ip_range in perm.get('IpRanges', []):
            cidr = ip_range.get('CidrIp', '')
            if cidr == '0.0.0.0/0':
                return (75, "Security Group allows ingress from 0.0.0.0/0 (TP-005)")

        # 檢查 IPv6
        for ip_range in perm.get('Ipv6Ranges', []):
            cidr = ip_range.get('CidrIpv6', '')
            if cidr == '::/0':
                return (75, "Security Group allows ingress from ::/0 (TP-005)")

    return None


def check_high_risk_port(payload: Any) -> Optional[Tuple[int, str]]:
    """
    TP-006: 檢查 Security Group 高危端口 + 0.0.0.0/0

    高危端口: 22, 3389, 3306, 1433, 5432, 27017
    """
    permissions = _normalize_ip_permissions(payload)

    for perm in permissions:
        from_port = perm.get('FromPort', 0)
        to_port = perm.get('ToPort', 0)

        # 檢查是否包含高危端口
        exposed_ports = set()
        for port in HIGH_RISK_PORTS:
            if from_port <= port <= to_port:
                exposed_ports.add(port)

        if not exposed_ports:
            continue

        # 檢查是否對 0.0.0.0/0 或 ::/0 開放
        is_open = False
        for ip_range in perm.get('IpRanges', []):
            if ip_range.get('CidrIp', '') == '0.0.0.0/0':
                is_open = True
                break
        if not is_open:
            for ip_range in perm.get('Ipv6Ranges', []):
                if ip_range.get('CidrIpv6', '') == '::/0':
                    is_open = True
                    break

        if is_open:
            ports_str = ', '.join(str(p) for p in sorted(exposed_ports))
            return (
                85,
                f"Security Group exposes high-risk port(s) "
                f"{ports_str} to 0.0.0.0/0 (TP-006)",
            )

    return None


def _normalize_ip_permissions(payload: Any) -> List[dict]:
    """將 ip-permissions payload 正規化為 list of dicts"""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        # 可能是 {"IpPermissions": [...]} 或直接就是 permission
        if 'IpPermissions' in payload:
            perms = payload['IpPermissions']
            if isinstance(perms, list):
                return [p for p in perms if isinstance(p, dict)]
        # 直接是 permission dict（有 FromPort/ToPort/IpRanges）
        if any(k in payload for k in ('FromPort', 'ToPort', 'IpRanges', 'IpProtocol')):
            return [payload]
    return []


def check_hardcoded_secret(payload: Any) -> Optional[Tuple[int, str]]:
    """
    TP-007: 檢查 Lambda 環境變數中的硬編碼密碼

    偵測 key 名稱匹配 SECRET, PASSWORD, TOKEN, API_KEY 等模式
    """
    env_vars = _extract_env_variables(payload)

    suspicious_keys = []
    for key in env_vars:
        if SECRET_KEY_PATTERN.search(key):
            suspicious_keys.append(key)

    if suspicious_keys:
        keys_str = ', '.join(suspicious_keys[:5])  # 最多顯示 5 個
        suffix = f" (+{len(suspicious_keys) - 5} more)" if len(suspicious_keys) > 5 else ""
        return (
            80,
            f"Lambda environment contains potential secrets: "
            f"{keys_str}{suffix} (TP-007)",
        )
    return None


def _extract_env_variables(payload: Any) -> Dict[str, str]:
    """從 Lambda environment payload 提取環境變數"""
    if isinstance(payload, dict):
        # 格式: {"Variables": {"KEY": "VALUE"}}
        variables = payload.get('Variables', payload.get('variables'))
        if isinstance(variables, dict):
            return variables
        # 直接是 key-value（如果沒有 Variables 包裹）
        # 只在看起來像環境變數時才回傳（排除 Statement 等 policy 結構）
        if not any(k in payload for k in ('Statement', 'Version', 'Effect')):
            return payload
    return {}


def check_admin_policy(payload: dict) -> Optional[Tuple[int, str]]:
    """
    TP-008: 檢查完全管理員 Policy

    同時滿足：Effect:Allow + Action:* + Resource:*
    """
    for stmt in _iter_statements(payload):
        effect = stmt.get('Effect', stmt.get('effect', ''))
        action = stmt.get('Action', stmt.get('action'))
        resource = stmt.get('Resource', stmt.get('resource'))

        if (
            effect.lower() == 'allow'
            and _is_wildcard(action)
            and _is_wildcard(resource)
        ):
            return (
                95,
                "Full admin policy: Effect:Allow + Action:* + Resource:* (TP-008)",
            )
    return None


def check_public_access(payload: dict) -> Optional[Tuple[int, str]]:
    """
    TP-009: 檢查公開存取 Policy

    Principal:* + Effect:Allow = 公開讀寫
    """
    for stmt in _iter_statements(payload):
        effect = stmt.get('Effect', stmt.get('effect', ''))
        principal = stmt.get('Principal', stmt.get('principal'))

        if effect.lower() != 'allow':
            continue

        if principal == '*':
            return (85, "Public access: Principal:* with Effect:Allow (TP-009)")

        if isinstance(principal, dict):
            aws_val = principal.get('AWS', principal.get('aws'))
            if _is_wildcard(aws_val):
                return (85, "Public access: Principal AWS:* with Effect:Allow (TP-009)")

    return None


# ============================================================================
# Check Registry
# ============================================================================

# 映射 check name → (check_function, applicable_param_patterns)
# applicable_param_patterns: 哪些參數適用此 check（None = 全部適用）
CHECK_REGISTRY = {
    'action_wildcard': (
        check_action_wildcard,
        {'--policy-document', '--assume-role-policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
    'resource_wildcard': (
        check_resource_wildcard,
        {'--policy-document', '--assume-role-policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
    'principal_wildcard': (
        check_principal_wildcard,
        {'--policy-document', '--assume-role-policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
    'external_account_trust': (
        check_external_account_trust,
        {'--assume-role-policy-document', '--policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
    'open_ingress': (
        check_open_ingress,
        {'--ip-permissions', '--cli-input-json', '--template-body'},
    ),
    'high_risk_port': (
        check_high_risk_port,
        {'--ip-permissions', '--cli-input-json', '--template-body'},
    ),
    'hardcoded_secret': (
        check_hardcoded_secret,
        {'--environment', '--cli-input-json', '--template-body'},
    ),
    'admin_policy': (
        check_admin_policy,
        {'--policy-document', '--assume-role-policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
    'public_access': (
        check_public_access,
        {'--policy-document', '--assume-role-policy-document', '--policy',
         '--cli-input-json', '--template-body'},
    ),
}


# ============================================================================
# Scanning Functions
# ============================================================================

def scan_payload(
    param_name: str,
    payload: dict,
    rules: List[dict],
) -> List[RiskFactor]:
    """
    對單一 payload 執行所有適用的 template 規則

    Args:
        param_name: 來源參數名稱 (e.g., '--policy-document')
        payload: 解析後的 JSON payload
        rules: template_rules 列表

    Returns:
        匹配的 RiskFactor 列表
    """
    factors = []

    for rule in rules:
        check_name = rule.get('check', '')
        rule_id = rule.get('id', 'TP-???')
        rule_score = rule.get('score', 0)

        if check_name not in CHECK_REGISTRY:
            continue

        check_fn, applicable_params = CHECK_REGISTRY[check_name]

        # 檢查此規則是否適用於此參數
        if applicable_params and param_name not in applicable_params:
            continue

        try:
            result = check_fn(payload)
        except Exception:
            # 個別 check 失敗不影響其他
            continue

        if result is not None:
            score, description = result
            # 使用規則定義的分數（而非 check 函數回傳的分數）
            final_score = rule_score

            factors.append(RiskFactor(
                name=f"Template: {rule.get('name', check_name)}",
                category="parameter",
                raw_score=final_score,
                weighted_score=0,
                weight=0,
                details=f"[{rule_id}] {description} (param: {param_name})",
            ))

    return factors


def scan_command_payloads(
    command: str,
    rules: List[dict],
) -> Tuple[int, List[RiskFactor]]:
    """
    掃描命令中所有 JSON payload 的主入口

    Args:
        command: 原始 AWS CLI 命令
        rules: template_rules 列表

    Returns:
        (max_score, factors) — 最高風險分數和所有風險因素
    """
    if not command or not rules:
        return (0, [])

    payloads = extract_json_payloads(command)
    if not payloads:
        return (0, [])

    all_factors = []
    max_score = 0

    for param_name, payload in payloads:
        factors = scan_payload(param_name, payload, rules)
        all_factors.extend(factors)
        for f in factors:
            max_score = max(max_score, f.raw_score)

    return (max_score, all_factors)
