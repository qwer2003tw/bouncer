"""
Bouncer Constants
集中管理所有常數和配置
"""
import os

# ============================================================================
# 版本
# ============================================================================

VERSION = '3.0.0'

# ============================================================================
# 環境變數 - Telegram
# ============================================================================

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')
TELEGRAM_API_BASE = "https://api.telegram.org/bot"

# 審批者 Chat ID（支援多個，逗號分隔）
APPROVED_CHAT_IDS = set(os.environ.get('APPROVED_CHAT_ID', '999999999').replace(' ', '').split(','))
APPROVED_CHAT_ID = os.environ.get('APPROVED_CHAT_ID', '999999999').split(',')[0]

# ============================================================================
# 環境變數 - DynamoDB
# ============================================================================

TABLE_NAME = os.environ.get('TABLE_NAME', 'clawdbot-approval-requests')
ACCOUNTS_TABLE_NAME = os.environ.get('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')

# ============================================================================
# 環境變數 - AWS
# ============================================================================

DEFAULT_ACCOUNT_ID = os.environ.get('DEFAULT_ACCOUNT_ID', '111111111111')

# ============================================================================
# 環境變數 - 安全
# ============================================================================

REQUEST_SECRET = os.environ.get('REQUEST_SECRET', '')
ENABLE_HMAC = os.environ.get('ENABLE_HMAC', 'false').lower() == 'true'

# ============================================================================
# MCP 配置
# ============================================================================

MCP_MAX_WAIT = int(os.environ.get('MCP_MAX_WAIT', '840'))  # 14 分鐘

# ============================================================================
# Rate Limiting
# ============================================================================

RATE_LIMIT_WINDOW = 60  # 60 秒視窗
RATE_LIMIT_MAX_REQUESTS = 5  # 每視窗最多 5 個審批請求
MAX_PENDING_PER_SOURCE = 10  # 每 source 最多 10 個 pending
RATE_LIMIT_ENABLED = os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true'

# ============================================================================
# Trust Session - 連續批准功能
# ============================================================================

TRUST_SESSION_DURATION = 600  # 10 分鐘
TRUST_SESSION_MAX_COMMANDS = 20  # 信任時段內最多執行 20 個命令
TRUST_SESSION_ENABLED = os.environ.get('TRUST_SESSION_ENABLED', 'true').lower() == 'true'

# 高危服務 - 即使在信任時段也需要審批
TRUST_EXCLUDED_SERVICES = [
    'iam', 'sts', 'organizations', 'kms', 'secretsmanager',
    'cloudformation', 'cloudtrail'
]

# 高危操作 - 即使在信任時段也需要審批
TRUST_EXCLUDED_ACTIONS = [
    # 通用破壞性操作
    'delete-', 'terminate-', 'remove-', 'destroy-',
    'stop-', 'disable-', 'deregister-',
    # EC2
    'modify-instance-attribute',
    # S3
    's3 rm', 's3 mv', 's3api delete', 's3 sync --delete',
    'put-bucket-policy', 'put-bucket-acl', 'delete-bucket',
    # Lambda
    'update-function-code', 'update-function-configuration',
    # ECS
    'update-service', 'delete-service', 'stop-task',
    # RDS
    'delete-db', 'modify-db', 'reboot-db',
    # DynamoDB
    'delete-table', 'update-table',
    # CloudWatch
    'delete-alarms', 'disable-alarm-actions',
    # Route53
    'delete-hosted-zone', 'change-resource-record-sets',
    # VPC
    'delete-vpc', 'delete-subnet', 'delete-security-group',
    'authorize-security-group', 'revoke-security-group',
    # API Gateway
    'delete-rest-api', 'delete-stage',
    # SNS/SQS
    'delete-topic', 'delete-queue', 'set-queue-attributes',
    # Secrets Manager（非 read）
    'create-secret', 'update-secret', 'delete-secret', 'put-secret-value',
]

# 危險旗標 - 即使在信任時段也需要審批
TRUST_EXCLUDED_FLAGS = [
    '--force',
    '--yes',
    '--no-wait',
    '--no-verify-ssl',
    '--recursive',
    '--include-all-instances',
    '--skip-final-snapshot',
    '--delete-automated-backups',
]

# ============================================================================
# Output Paging - 長輸出分頁
# ============================================================================

OUTPUT_PAGE_SIZE = 3000  # 每頁字元數
OUTPUT_MAX_INLINE = 3500  # 直接回傳的最大長度
OUTPUT_PAGE_TTL = 3600  # 分頁資料保留 1 小時

# ============================================================================
# 命令分類 - 黑名單（絕對禁止）
# ============================================================================

BLOCKED_PATTERNS = [
    # 危險操作
    'iam delete-user',
    'iam delete-role',
    'iam delete-policy',
    'iam create-user',
    'iam attach-user-policy',
    'iam attach-role-policy',
    'iam put-user-policy',
    'iam put-role-policy',
    'iam create-access-key',
    'iam update-access-key',
    'iam delete-access-key',
    'sts assume-role',
    'sts get-session-token',
    'organizations',
    # 高危刪除操作
    'ec2 terminate-instances',
    'rds delete-db-instance',
    'rds delete-db-cluster',
    's3 rb',  # remove bucket
    's3api delete-bucket',
    'lambda delete-function',
    'dynamodb delete-table',
    'cloudformation delete-stack',
    # 新增的危險操作
    'ec2 modify-instance-attribute',
    'ec2 create-key-pair',
    'ec2 import-key-pair',
    'kms create-key',
    'kms schedule-key-deletion',
    'secretsmanager delete-secret',
    'logs delete-log-group',
    'events delete-rule',
]

# ============================================================================
# 命令分類 - 白名單（自動批准）
# ============================================================================

AUTO_APPROVE_PREFIXES = [
    # 讀取操作
    'aws s3 ls',
    'aws s3 cp s3:',  # 只允許從 S3 下載
    'aws ec2 describe-',
    'aws rds describe-',
    'aws lambda list-',
    'aws lambda get-',
    'aws dynamodb describe-',
    'aws dynamodb list-',
    'aws dynamodb scan',
    'aws dynamodb query',
    'aws dynamodb get-item',
    'aws cloudformation describe-',
    'aws cloudformation list-',
    'aws logs describe-',
    'aws logs filter-log-events',
    'aws logs get-log-events',
    'aws sts get-caller-identity',
    'aws iam list-',
    'aws iam get-',
    'aws cloudwatch describe-',
    'aws cloudwatch list-',
    'aws cloudwatch get-',
    'aws events list-',
    'aws events describe-',
    'aws sns list-',
    'aws sqs list-',
    'aws sqs get-queue-attributes',
    'aws route53 list-',
    'aws route53 get-',
    'aws apigateway get-',
    'aws ecs list-',
    'aws ecs describe-',
    'aws ecr describe-',
    'aws ecr list-',
    'aws ecr get-',
    'aws secretsmanager list-secrets',
    'aws secretsmanager describe-secret',
    'aws secretsmanager get-secret-value',
    'aws kms list-',
    'aws kms describe-',
    'aws states list-',
    'aws states describe-',
    'aws stepfunctions list-',
    'aws stepfunctions describe-',
    'aws network-firewall describe-',
    'aws network-firewall list-',
]
