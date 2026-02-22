"""
Bouncer - Command Sequence Analyzer
命令序列分析模組：分析命令序列以判斷風險

設計原則：
1. 追蹤執行歷史，判斷是否有前置查詢
2. 有前置查詢的破壞性操作風險較低
3. 直接執行破壞性操作風險較高

例如：
- describe-instances → terminate-instances = 安全（有前置查詢）
- 直接 terminate-instances = 高風險（沒有前置查詢）

Author: Bouncer Team
Version: 1.0.0
"""

import os
import re
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import boto3
from boto3.dynamodb.conditions import Key

__all__ = [
    # Data classes
    'CommandRecord',
    'SequenceAnalysis',
    # Core functions
    'record_command',
    'get_recent_commands',
    'analyze_sequence',
    # Utilities
    'extract_resource_ids',
    'parse_action_from_command',
]


# ============================================================================
# Configuration
# ============================================================================

# DynamoDB 表名 - 使用獨立的歷史記錄表
HISTORY_TABLE_NAME = os.environ.get('COMMAND_HISTORY_TABLE', 'bouncer-command-history')

# 設定
DEFAULT_HISTORY_MINUTES = 30  # 預設查詢最近 30 分鐘
HISTORY_TTL_DAYS = 30  # 歷史記錄保留 30 天

# DynamoDB 客戶端（延遲初始化）
_dynamodb = None
_history_table = None


def _get_history_table():
    """取得 DynamoDB 歷史表（延遲初始化）"""
    global _dynamodb, _history_table
    if _history_table is None:
        _dynamodb = boto3.resource('dynamodb')
        _history_table = _dynamodb.Table(HISTORY_TABLE_NAME)
    return _history_table


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CommandRecord:
    """
    命令記錄

    Attributes:
        source: 來源標識（如 "Private Bot"）
        timestamp: 執行時間 (ISO8601)
        command: 完整命令
        service: AWS 服務名稱 (如 ec2, s3)
        action: 操作名稱 (如 describe-instances, terminate-instances)
        resource_ids: 提取的資源 ID 列表
        account_id: AWS 帳號 ID
    """
    source: str
    timestamp: str
    command: str
    service: str
    action: str
    resource_ids: List[str] = field(default_factory=list)
    account_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            'source': self.source,
            'timestamp': self.timestamp,
            'command': self.command,
            'service': self.service,
            'action': self.action,
            'resource_ids': self.resource_ids,
            'account_id': self.account_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CommandRecord':
        """從字典建立"""
        return cls(
            source=data.get('source', ''),
            timestamp=data.get('timestamp', ''),
            command=data.get('command', ''),
            service=data.get('service', ''),
            action=data.get('action', ''),
            resource_ids=data.get('resource_ids', []),
            account_id=data.get('account_id', ''),
        )


@dataclass
class SequenceAnalysis:
    """
    序列分析結果

    Attributes:
        has_prior_query: 是否有相關的 describe/list 前置查詢
        related_commands: 相關的前置命令列表
        risk_modifier: 風險修正值 (-0.3 到 +0.3)
        reason: 人類可讀的原因說明
        resource_match: 是否有資源 ID 匹配
        matched_resources: 匹配的資源 ID 列表
    """
    has_prior_query: bool
    related_commands: List[str]
    risk_modifier: float
    reason: str
    resource_match: bool = False
    matched_resources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            'has_prior_query': self.has_prior_query,
            'related_commands': self.related_commands,
            'risk_modifier': self.risk_modifier,
            'reason': self.reason,
            'resource_match': self.resource_match,
            'matched_resources': self.matched_resources,
        }


# ============================================================================
# Safe Patterns Definition
# ============================================================================

# 安全模式：危險操作 → 相關的前置查詢操作
SAFE_PATTERNS: Dict[str, List[str]] = {
    # EC2
    'terminate-instances': [
        'describe-instances',
        'describe-instance-status',
        'describe-instance-attribute',
    ],
    'stop-instances': [
        'describe-instances',
        'describe-instance-status',
    ],
    'delete-security-group': [
        'describe-security-groups',
        'describe-security-group-rules',
    ],
    'delete-snapshot': [
        'describe-snapshots',
    ],
    'delete-volume': [
        'describe-volumes',
    ],
    'deregister-image': [
        'describe-images',
    ],

    # S3
    'delete-bucket': [
        'list-buckets',
        'list-objects',
        'list-objects-v2',
        'head-bucket',
        'get-bucket-location',
    ],
    'delete-object': [
        'list-objects',
        'list-objects-v2',
        'head-object',
        'get-object',
    ],
    'rm': [  # aws s3 rm
        'ls',
        'list-objects',
        'list-objects-v2',
    ],
    'rb': [  # aws s3 rb (remove bucket)
        'ls',
        'list-buckets',
        'list-objects',
    ],

    # Lambda
    'delete-function': [
        'get-function',
        'list-functions',
        'get-function-configuration',
    ],
    'delete-layer-version': [
        'list-layer-versions',
        'get-layer-version',
    ],

    # DynamoDB
    'delete-table': [
        'describe-table',
        'list-tables',
        'scan',
        'query',
    ],
    'delete-item': [
        'get-item',
        'query',
        'scan',
    ],

    # CloudFormation
    'delete-stack': [
        'describe-stacks',
        'describe-stack-events',
        'describe-stack-resources',
        'list-stacks',
        'get-template',
    ],

    # RDS
    'delete-db-instance': [
        'describe-db-instances',
    ],
    'delete-db-cluster': [
        'describe-db-clusters',
    ],
    'delete-db-snapshot': [
        'describe-db-snapshots',
    ],

    # ECS
    'delete-service': [
        'describe-services',
        'list-services',
    ],
    'delete-cluster': [
        'describe-clusters',
        'list-clusters',
    ],
    'stop-task': [
        'describe-tasks',
        'list-tasks',
    ],

    # Secrets Manager
    'delete-secret': [
        'describe-secret',
        'list-secrets',
        'get-secret-value',
    ],

    # CloudWatch Logs
    'delete-log-group': [
        'describe-log-groups',
    ],
    'delete-log-stream': [
        'describe-log-streams',
    ],

    # SNS
    'delete-topic': [
        'list-topics',
        'get-topic-attributes',
    ],

    # SQS
    'delete-queue': [
        'list-queues',
        'get-queue-attributes',
        'get-queue-url',
    ],

    # API Gateway
    'delete-rest-api': [
        'get-rest-apis',
        'get-rest-api',
    ],
    'delete-stage': [
        'get-stages',
    ],

    # Route53
    'delete-hosted-zone': [
        'list-hosted-zones',
        'get-hosted-zone',
    ],

    # KMS
    'schedule-key-deletion': [
        'describe-key',
        'list-keys',
    ],

    # IAM (雖然通常被 block，但定義完整性)
    'delete-role': [
        'get-role',
        'list-roles',
    ],
    'delete-user': [
        'get-user',
        'list-users',
    ],
}

# 服務別名映射（處理 aws s3 vs aws s3api）
SERVICE_ALIASES: Dict[str, str] = {
    's3api': 's3',
    'apigatewayv2': 'apigateway',
    'stepfunctions': 'states',
    'elbv2': 'elb',
}


# ============================================================================
# Resource ID Extraction Patterns
# ============================================================================

RESOURCE_ID_PATTERNS: List[Dict[str, Any]] = [
    # EC2
    {
        'pattern': r'--instance-ids?\s+([i]-[a-f0-9]+(?:\s+[i]-[a-f0-9]+)*)',
        'type': 'instance_id',
        'split': True,
    },
    {
        'pattern': r'--security-group-ids?\s+(sg-[a-f0-9]+(?:\s+sg-[a-f0-9]+)*)',
        'type': 'security_group_id',
        'split': True,
    },
    {
        'pattern': r'--snapshot-ids?\s+(snap-[a-f0-9]+(?:\s+snap-[a-f0-9]+)*)',
        'type': 'snapshot_id',
        'split': True,
    },
    {
        'pattern': r'--volume-ids?\s+(vol-[a-f0-9]+(?:\s+vol-[a-f0-9]+)*)',
        'type': 'volume_id',
        'split': True,
    },
    {
        'pattern': r'--image-ids?\s+(ami-[a-f0-9]+(?:\s+ami-[a-f0-9]+)*)',
        'type': 'ami_id',
        'split': True,
    },

    # S3
    {
        'pattern': r'--bucket\s+([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])',
        'type': 'bucket_name',
    },
    {
        'pattern': r'--bucket-name\s+([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])',
        'type': 'bucket_name',
    },
    {
        'pattern': r's3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])',
        'type': 'bucket_name',
    },

    # Lambda
    {
        'pattern': r'--function-name\s+([a-zA-Z0-9_-]+)',
        'type': 'function_name',
    },

    # DynamoDB
    {
        'pattern': r'--table-name\s+([a-zA-Z0-9_.-]+)',
        'type': 'table_name',
    },

    # CloudFormation
    {
        'pattern': r'--stack-name\s+([a-zA-Z0-9-]+)',
        'type': 'stack_name',
    },

    # RDS
    {
        'pattern': r'--db-instance-identifier\s+([a-zA-Z0-9-]+)',
        'type': 'db_instance_id',
    },
    {
        'pattern': r'--db-cluster-identifier\s+([a-zA-Z0-9-]+)',
        'type': 'db_cluster_id',
    },

    # ECS
    {
        'pattern': r'--cluster\s+([a-zA-Z0-9_-]+)',
        'type': 'cluster_name',
    },
    {
        'pattern': r'--service\s+([a-zA-Z0-9_-]+)',
        'type': 'service_name',
    },

    # Secrets Manager
    {
        'pattern': r'--secret-id\s+([a-zA-Z0-9/_+=.@-]+)',
        'type': 'secret_id',
    },

    # CloudWatch Logs
    {
        'pattern': r'--log-group-name\s+([a-zA-Z0-9/_.-]+)',
        'type': 'log_group_name',
    },

    # SNS
    {
        'pattern': r'--topic-arn\s+(arn:aws:sns:[a-z0-9-]+:[0-9]+:[a-zA-Z0-9_-]+)',
        'type': 'topic_arn',
    },

    # SQS
    {
        'pattern': r'--queue-url\s+(https://[a-zA-Z0-9./-]+)',
        'type': 'queue_url',
    },

    # API Gateway
    {
        'pattern': r'--rest-api-id\s+([a-z0-9]+)',
        'type': 'rest_api_id',
    },

    # Route53
    {
        'pattern': r'--hosted-zone-id\s+(/hostedzone/)?([A-Z0-9]+)',
        'type': 'hosted_zone_id',
        'group': 2,
    },

    # KMS
    {
        'pattern': r'--key-id\s+([a-f0-9-]+|alias/[a-zA-Z0-9/_-]+)',
        'type': 'key_id',
    },
]


# ============================================================================
# Core Functions
# ============================================================================

def extract_resource_ids(command: str) -> List[str]:
    """
    從命令中提取資源 ID

    Args:
        command: AWS CLI 命令

    Returns:
        提取到的資源 ID 列表
    """
    resource_ids = []

    for pattern_def in RESOURCE_ID_PATTERNS:
        pattern = pattern_def['pattern']
        match = re.search(pattern, command, re.IGNORECASE)

        if match:
            group_idx = pattern_def.get('group', 1)
            value = match.group(group_idx)

            if pattern_def.get('split'):
                # 處理多個 ID（空格分隔）
                ids = value.split()
                resource_ids.extend(ids)
            else:
                resource_ids.append(value)

    return list(set(resource_ids))  # 去重


def parse_action_from_command(command: str) -> Tuple[str, str]:
    """
    從命令解析服務和動作

    Args:
        command: AWS CLI 命令

    Returns:
        (service, action) 元組
    """
    # 移除 aws 前綴
    cmd = command.strip()
    if cmd.startswith('aws '):
        cmd = cmd[4:]

    parts = cmd.split()
    if len(parts) < 2:
        return '', ''

    service = parts[0]
    action = parts[1]

    # 處理服務別名
    service = SERVICE_ALIASES.get(service, service)

    return service, action


def record_command(
    source: str,
    command: str,
    account_id: str = "",
) -> Optional[CommandRecord]:
    """
    記錄執行的命令到 DynamoDB

    Args:
        source: 來源標識
        command: 完整命令
        account_id: AWS 帳號 ID

    Returns:
        建立的 CommandRecord，或 None（如果失敗）
    """
    try:
        # 解析命令
        service, action = parse_action_from_command(command)
        resource_ids = extract_resource_ids(command)

        # 建立時間戳
        timestamp = datetime.utcnow().isoformat() + 'Z'

        # 計算 TTL（30 天後過期）
        ttl = int(time.time()) + (HISTORY_TTL_DAYS * 24 * 60 * 60)

        # 建立 source hash 作為 PK（避免特殊字元問題）
        source_hash = hashlib.sha256(source.encode()).hexdigest()[:16]

        # 寫入 DynamoDB
        table = _get_history_table()
        item = {
            'pk': f'source#{source_hash}',
            'sk': f'ts#{timestamp}',
            'source': source,
            'command': command,
            'service': service,
            'action': action,
            'resource_ids': resource_ids,
            'account_id': account_id,
            'ttl': ttl,
        }

        table.put_item(Item=item)

        return CommandRecord(
            source=source,
            timestamp=timestamp,
            command=command,
            service=service,
            action=action,
            resource_ids=resource_ids,
            account_id=account_id,
        )

    except Exception as e:
        print(f"[SequenceAnalyzer] Failed to record command: {e}")
        return None


def get_recent_commands(
    source: str,
    minutes: int = DEFAULT_HISTORY_MINUTES,
) -> List[CommandRecord]:
    """
    取得最近 N 分鐘的命令記錄

    Args:
        source: 來源標識
        minutes: 查詢的時間範圍（分鐘）

    Returns:
        CommandRecord 列表，按時間倒序排列
    """
    try:
        table = _get_history_table()

        # 計算時間範圍
        now = datetime.utcnow()
        start_time = now - timedelta(minutes=minutes)

        # 建立 source hash
        source_hash = hashlib.sha256(source.encode()).hexdigest()[:16]

        # 查詢 DynamoDB
        response = table.query(
            KeyConditionExpression=Key('pk').eq(f'source#{source_hash}') &
                                   Key('sk').gte(f'ts#{start_time.isoformat()}Z'),
            ScanIndexForward=False,  # 倒序
            Limit=100,  # 最多 100 筆
        )

        records = []
        for item in response.get('Items', []):
            records.append(CommandRecord(
                source=item.get('source', source),
                timestamp=item.get('sk', '').replace('ts#', ''),
                command=item.get('command', ''),
                service=item.get('service', ''),
                action=item.get('action', ''),
                resource_ids=item.get('resource_ids', []),
                account_id=item.get('account_id', ''),
            ))

        return records

    except Exception as e:
        print(f"[SequenceAnalyzer] Failed to get recent commands: {e}")
        return []


def analyze_sequence(
    source: str,
    new_command: str,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
) -> SequenceAnalysis:
    """
    分析新命令與歷史序列的關係

    Args:
        source: 來源標識
        new_command: 新命令
        history_minutes: 查詢的歷史時間範圍（分鐘）

    Returns:
        SequenceAnalysis 結果
    """
    # 解析新命令
    new_service, new_action = parse_action_from_command(new_command)
    new_resource_ids = set(extract_resource_ids(new_command))

    # 檢查是否是危險操作
    if new_action not in SAFE_PATTERNS:
        # 不是危險操作，不需要前置查詢
        return SequenceAnalysis(
            has_prior_query=True,  # 非危險操作視為「有前置查詢」
            related_commands=[],
            risk_modifier=0.0,
            reason=f"'{new_action}' 不是危險操作，不需要前置查詢",
        )

    # 取得相關的前置查詢模式
    safe_queries = SAFE_PATTERNS[new_action]

    # 取得歷史命令
    try:
        history = get_recent_commands(source, minutes=history_minutes)
    except Exception as e:
        # 無法取得歷史，保守處理
        return SequenceAnalysis(
            has_prior_query=False,
            related_commands=[],
            risk_modifier=0.15,
            reason=f"無法取得命令歷史: {e}",
        )

    # 分析歷史命令
    related_commands = []
    matched_resources = []
    has_service_match = False
    has_resource_match = False

    for record in history:
        # 檢查是否同服務
        record_service = SERVICE_ALIASES.get(record.service, record.service)
        if record_service != new_service:
            continue

        # 檢查是否是相關的前置查詢
        if record.action in safe_queries:
            has_service_match = True
            related_commands.append(f"{record.service} {record.action}")

            # 檢查資源 ID 是否匹配
            if new_resource_ids and record.resource_ids:
                common_resources = new_resource_ids & set(record.resource_ids)
                if common_resources:
                    has_resource_match = True
                    matched_resources.extend(common_resources)

    # 去重
    related_commands = list(set(related_commands))[:5]  # 最多顯示 5 個
    matched_resources = list(set(matched_resources))

    # 計算風險修正值和原因
    if has_resource_match:
        # 最佳情況：有前置查詢 + 資源 ID 匹配
        risk_modifier = -0.25
        reason = f"✅ 有前置查詢且資源匹配: {', '.join(related_commands)}"
    elif has_service_match:
        # 中等情況：有前置查詢但沒有資源 ID 匹配
        risk_modifier = -0.15
        reason = f"⚠️ 有前置查詢但資源未匹配: {', '.join(related_commands)}"
    else:
        # 最差情況：沒有前置查詢
        risk_modifier = 0.20
        reason = f"🚨 未找到相關的前置查詢（建議先執行: {', '.join(safe_queries[:2])}）"

    return SequenceAnalysis(
        has_prior_query=has_service_match,
        related_commands=related_commands,
        risk_modifier=risk_modifier,
        reason=reason,
        resource_match=has_resource_match,
        matched_resources=matched_resources,
    )


# ============================================================================
# Integration with Risk Scorer
# ============================================================================

def get_sequence_risk_modifier(
    source: str,
    command: str,
    account_id: str = "",
) -> Tuple[float, str]:
    """
    取得序列分析的風險修正值（供 risk_scorer 使用）

    Args:
        source: 來源標識
        command: 命令
        account_id: 帳號 ID

    Returns:
        (risk_modifier, reason) 元組
        - risk_modifier: -0.3 到 +0.3
        - reason: 原因說明
    """
    try:
        analysis = analyze_sequence(source, command)
        return analysis.risk_modifier, analysis.reason
    except Exception as e:
        # 分析失敗，不調整風險
        return 0.0, f"序列分析失敗: {e}"


# ============================================================================
# Testing Support
# ============================================================================

def _test_sequence_analyzer():
    """
    內部測試函數

    Usage:
        python -c "from sequence_analyzer import _test_sequence_analyzer; _test_sequence_analyzer()"
    """
    print("Sequence Analyzer Test Results")
    print("=" * 70)

    # 測試資源 ID 提取
    print("\n1. Resource ID Extraction Tests:")
    test_commands = [
        ("aws ec2 terminate-instances --instance-ids i-1234567890abcdef0", ["i-1234567890abcdef0"]),
        ("aws ec2 describe-instances --instance-ids i-abc123 i-def456", ["i-abc123", "i-def456"]),
        ("aws s3 rm s3://my-bucket/key --recursive", ["my-bucket"]),
        ("aws lambda delete-function --function-name my-function", ["my-function"]),
        ("aws dynamodb delete-table --table-name my-table", ["my-table"]),
        ("aws cloudformation delete-stack --stack-name my-stack", ["my-stack"]),
    ]

    for cmd, expected in test_commands:
        result = extract_resource_ids(cmd)
        status = "✅" if set(result) == set(expected) else "❌"
        print(f"  {status} {cmd[:50]}...")
        print(f"     Expected: {expected}")
        print(f"     Got: {result}")

    # 測試服務/動作解析
    print("\n2. Service/Action Parsing Tests:")
    parse_tests = [
        ("aws ec2 terminate-instances --instance-ids i-123", ("ec2", "terminate-instances")),
        ("aws s3 rm s3://bucket/key", ("s3", "rm")),
        ("aws lambda delete-function --function-name test", ("lambda", "delete-function")),
    ]

    for cmd, expected in parse_tests:
        result = parse_action_from_command(cmd)
        status = "✅" if result == expected else "❌"
        print(f"  {status} {cmd[:50]}...")
        print(f"     Expected: {expected}")
        print(f"     Got: {result}")

    # 測試安全模式定義
    print("\n3. Safe Patterns Coverage:")
    dangerous_actions = list(SAFE_PATTERNS.keys())
    print(f"  Defined dangerous actions: {len(dangerous_actions)}")
    for action in sorted(dangerous_actions)[:10]:
        queries = SAFE_PATTERNS[action][:3]
        print(f"    - {action}: {', '.join(queries)}")
    if len(dangerous_actions) > 10:
        print(f"    ... and {len(dangerous_actions) - 10} more")

    print("\n" + "=" * 70)
    print("Tests completed! (DynamoDB tests skipped - require actual table)")


if __name__ == '__main__':
    _test_sequence_analyzer()
