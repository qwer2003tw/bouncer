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
APPROVED_CHAT_IDS = set(os.environ.get('APPROVED_CHAT_ID', '').replace(' ', '').split(','))
APPROVED_CHAT_ID = os.environ.get('APPROVED_CHAT_ID', '').split(',')[0]

# ============================================================================
# 環境變數 - DynamoDB
# ============================================================================

TABLE_NAME = os.environ.get('TABLE_NAME', 'clawdbot-approval-requests')
ACCOUNTS_TABLE_NAME = os.environ.get('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')

# ============================================================================
# 環境變數 - AWS
# ============================================================================

DEFAULT_ACCOUNT_ID = os.environ.get('DEFAULT_ACCOUNT_ID', '')

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
# Approval Timeouts
# ============================================================================

APPROVAL_TIMEOUT_DEFAULT = 300  # 5 分鐘（帳號/上傳/部署）
APPROVAL_TTL_BUFFER = 60  # TTL 額外緩衝秒數
COMMAND_APPROVAL_TIMEOUT = MCP_MAX_WAIT  # 命令審批超時（同 MCP_MAX_WAIT）
UPLOAD_TIMEOUT = 300  # 上傳審批超時

# ============================================================================
# Audit Log TTL
# ============================================================================

AUDIT_TTL_SHORT = 30 * 24 * 60 * 60  # 30 天（blocked/compliance）
AUDIT_TTL_LONG = 90 * 24 * 60 * 60  # 90 天（其他）

# ============================================================================
# Telegram
# ============================================================================

TELEGRAM_TIMESTAMP_MAX_AGE = 300  # 5 分鐘

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
    # IAM 危險操作 - 絕對禁止
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
    # 其他絕對禁止
    # 'ec2 modify-instance-attribute',  # 移到 compliance_checker 做細粒度控制
    'ec2 create-key-pair',
    'ec2 import-key-pair',
    'kms create-key',
    'kms schedule-key-deletion',
]

# ============================================================================
# 命令分類 - 高危操作（需要特殊審批，顯示警告）
# ============================================================================

DANGEROUS_PATTERNS = [
    # S3 刪除
    's3 rb',  # remove bucket
    's3api delete-bucket',
    # EC2 刪除/終止
    'ec2 terminate-instances',
    # RDS 刪除
    'rds delete-db-instance',
    'rds delete-db-cluster',
    # Lambda 刪除
    'lambda delete-function',
    # DynamoDB 刪除
    'dynamodb delete-table',
    # CloudFormation 刪除
    'cloudformation delete-stack',
    # Secrets Manager 刪除
    'secretsmanager delete-secret',
    # Logs 刪除
    'logs delete-log-group',
    # Events 刪除
    'events delete-rule',
]

# ============================================================================
# 命令分類 - 白名單（自動批准）
# ============================================================================

AUTO_APPROVE_PREFIXES = [
    # STS
    'aws sts get-caller-identity',

    # S3 唯讀
    'aws s3 ls',
    'aws s3 cp s3:',  # 只允許從 S3 下載
    'aws s3api head-',
    'aws s3api get-object',
    'aws s3api list-',

    # EC2
    'aws ec2 describe-',

    # RDS
    'aws rds describe-',
    'aws rds list-',

    # Lambda
    'aws lambda list-',
    'aws lambda get-',

    # DynamoDB
    'aws dynamodb describe-',
    'aws dynamodb list-',
    'aws dynamodb scan',
    'aws dynamodb query',
    'aws dynamodb get-item',

    # CloudFormation
    'aws cloudformation describe-',
    'aws cloudformation list-',
    'aws cloudformation get-',

    # CloudWatch Logs
    'aws logs describe-',
    'aws logs filter-log-events',
    'aws logs get-log-events',
    'aws logs get-',
    'aws logs list-',
    'aws logs tail',

    # CloudWatch Metrics
    'aws cloudwatch describe-',
    'aws cloudwatch list-',
    'aws cloudwatch get-',

    # IAM (唯讀)
    'aws iam list-',
    'aws iam get-',

    # ECS/ECR
    'aws ecs list-',
    'aws ecs describe-',
    'aws ecr describe-',
    'aws ecr list-',
    'aws ecr get-',

    # Secrets Manager (唯讀)
    'aws secretsmanager list-secrets',
    'aws secretsmanager describe-secret',
    'aws secretsmanager get-secret-value',

    # KMS (唯讀)
    'aws kms list-',
    'aws kms describe-',

    # SSM Parameter Store
    'aws ssm describe-',
    'aws ssm get-parameter',
    'aws ssm get-parameters',
    'aws ssm list-',

    # SNS/SQS
    'aws sns list-',
    'aws sns get-',
    'aws sqs list-',
    'aws sqs get-queue-attributes',
    'aws sqs get-queue-url',

    # API Gateway
    'aws apigateway get-',
    'aws apigatewayv2 get-',

    # Route53
    'aws route53 list-',
    'aws route53 get-',

    # ACM
    'aws acm describe-',
    'aws acm list-',

    # CloudFront
    'aws cloudfront get-',
    'aws cloudfront list-',

    # Step Functions / States
    'aws states list-',
    'aws states describe-',
    'aws states get-',
    'aws stepfunctions list-',
    'aws stepfunctions describe-',
    'aws stepfunctions get-',

    # EventBridge
    'aws events list-',
    'aws events describe-',

    # Network Firewall
    'aws network-firewall describe-',
    'aws network-firewall list-',

    # Cost Explorer
    'aws ce get-',

    # Organizations (唯讀)
    'aws organizations list-',
    'aws organizations describe-',

    # ElastiCache
    'aws elasticache describe-',
    'aws elasticache list-',

    # Elastic Load Balancing
    'aws elbv2 describe-',
    'aws elb describe-',

    # Auto Scaling
    'aws autoscaling describe-',

    # CodeBuild/CodePipeline
    'aws codebuild list-',
    'aws codebuild batch-get-',
    'aws codepipeline list-',
    'aws codepipeline get-',
]
