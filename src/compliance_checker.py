"""
Compliance Checker - 三份安規合規檢查
在命令執行前檢查是否違反安全規則

安規來源：
- L1-L2: Lambda 安全規則
- P-S1~S5: Palisade 安全規則
- CS: Code Scanning 規則
"""
import json
import re
import os
from dataclasses import dataclass
from typing import Optional

# Read env var directly (not via constants) to avoid test-isolation issues
# where tests set the env var AFTER constants is imported.
# See #358 for the underlying sys.modules mess.
TRUSTED_ACCOUNT_IDS = [x for x in os.environ.get('TRUSTED_ACCOUNT_IDS', '').split(',') if x]


@dataclass
class ComplianceViolation:
    """合規違規結果"""
    rule_id: str
    rule_name: str
    description: str
    remediation: str


# ============================================================================
# 合規攔截規則
# 格式: (pattern, rule_id, rule_name, description, remediation)
# ============================================================================

COMPLIANCE_RULES = [
    # -------------------------------------------------------------------------
    # Lambda 安全規則 (L1-L2)
    # -------------------------------------------------------------------------
    (
        r"lambda\s+add-permission.*--principal\s+['\"]?\*['\"]?",
        "L1",
        "Lambda Principal:* 禁止",
        "Lambda 資源政策不可使用 Principal: * (允許任何人調用)",
        "指定具體的 AWS 帳號或服務作為 Principal",
    ),
    (
        r"lambda\s+create-function-url-config.*--auth-type\s+NONE",
        "L2",
        "Lambda URL 必須認證",
        "Lambda Function URL 必須啟用 IAM 認證",
        "使用 --auth-type AWS_IAM",
    ),
    (
        r"lambda\s+update-function-url-config.*--auth-type\s+NONE",
        "L2",
        "Lambda URL 必須認證",
        "Lambda Function URL 必須啟用 IAM 認證",
        "使用 --auth-type AWS_IAM",
    ),

    # -------------------------------------------------------------------------
    # Palisade - 公開存取禁止 (P-S2)
    # -------------------------------------------------------------------------
    # S3 公開存取
    (
        r"s3api\s+put-bucket-acl.*--acl\s+(public-read|public-read-write|authenticated-read)",
        "P-S2",
        "S3 禁止公開 ACL",
        "S3 Bucket 不可設定公開 ACL",
        "使用 --acl private 或移除 ACL 設定",
    ),
    (
        r"s3\s+.*--acl\s+(public-read|public-read-write)",
        "P-S2",
        "S3 禁止公開 ACL",
        "S3 物件不可設定公開 ACL",
        "移除 --acl 參數或使用 private",
    ),
    (
        r"s3api\s+put-public-access-block.*BlockPublicAcls['\"]?\s*:\s*false",
        "P-S2",
        "S3 Block Public Access 必須啟用",
        "S3 Bucket 必須啟用 Block Public Access",
        "設定 BlockPublicAcls, BlockPublicPolicy, IgnorePublicAcls, RestrictPublicBuckets 為 true",
    ),

    # EBS Snapshot 公開
    (
        r"ec2\s+modify-snapshot-attribute.*--attribute\s+createVolumePermission.*--group-names\s+all",
        "P-S2",
        "EBS Snapshot 禁止公開",
        "EBS Snapshot 不可設定為公開",
        "移除 --group-names all，指定具體帳號",
    ),

    # AMI 公開
    (
        r"ec2\s+modify-image-attribute.*--launch-permission.*['\"]?Group['\"]?\s*:\s*['\"]?all['\"]?",
        "P-S2",
        "AMI 禁止公開",
        "AMI 不可設定為公開",
        "移除公開權限，指定具體帳號",
    ),

    # RDS Snapshot 公開
    (
        r"rds\s+modify-db-snapshot-attribute.*--attribute-name\s+restore.*--values-to-add\s+all",
        "P-S2",
        "RDS Snapshot 禁止公開",
        "RDS Snapshot 不可設定為公開",
        "指定具體帳號而非 all",
    ),
    (
        r"rds\s+modify-db-cluster-snapshot-attribute.*--attribute-name\s+restore.*--values-to-add\s+all",
        "P-S2",
        "RDS Cluster Snapshot 禁止公開",
        "RDS Cluster Snapshot 不可設定為公開",
        "指定具體帳號而非 all",
    ),

    # -------------------------------------------------------------------------
    # Palisade - IAM/KMS 安全 (P-S2, P-S3)
    # -------------------------------------------------------------------------
    # IAM Role Trust Policy 公開
    (
        r"iam\s+update-assume-role-policy.*['\"]?Principal['\"]?\s*:\s*['\"]?\*['\"]?",
        "P-S2",
        "IAM Role Trust Policy 禁止 Principal:*",
        "IAM Role 的信任政策不可使用 Principal: *",
        "指定具體的 AWS 帳號或服務",
    ),
    (
        r"iam\s+create-role.*['\"]?Principal['\"]?\s*:\s*['\"]?\*['\"]?",
        "P-S2",
        "IAM Role Trust Policy 禁止 Principal:*",
        "建立 IAM Role 時信任政策不可使用 Principal: *",
        "指定具體的 AWS 帳號或服務",
    ),

    # KMS Key Policy 公開
    (
        r"kms\s+put-key-policy.*['\"]?Principal['\"]?\s*:\s*['\"]?\*['\"]?",
        "P-S2",
        "KMS Key Policy 禁止 Principal:*",
        "KMS Key 政策不可使用 Principal: *",
        "指定具體的 AWS 帳號或角色",
    ),
    (
        r"kms\s+create-key.*['\"]?Principal['\"]?\s*:\s*['\"]?\*['\"]?",
        "P-S2",
        "KMS Key Policy 禁止 Principal:*",
        "建立 KMS Key 時政策不可使用 Principal: *",
        "指定具體的 AWS 帳號或角色",
    ),

    # -------------------------------------------------------------------------
    # Palisade - SNS/SQS 公開 (P-S2)
    # -------------------------------------------------------------------------
    (
        r"sns\s+add-permission.*--aws-account-id\s+['\"]?\*['\"]?",
        "P-S2",
        "SNS 禁止公開存取",
        "SNS Topic 不可授權給所有人",
        "指定具體的 AWS 帳號",
    ),
    (
        r"sqs\s+add-permission.*--aws-account-ids\s+['\"]?\*['\"]?",
        "P-S2",
        "SQS 禁止公開存取",
        "SQS Queue 不可授權給所有人",
        "指定具體的 AWS 帳號",
    ),
    (
        r"sqs\s+set-queue-attributes.*['\"]?Principal['\"]?\s*:\s*['\"]?\*['\"]?",
        "P-S2",
        "SQS Policy 禁止 Principal:*",
        "SQS Queue 政策不可使用 Principal: *",
        "指定具體的 AWS 帳號或服務",
    ),

    # -------------------------------------------------------------------------
    # Palisade - 跨帳號信任 (P-S3)
    # -------------------------------------------------------------------------
    (
        r"iam\s+(update-assume-role-policy|create-role).*arn:aws:iam::(?!" + "|".join(TRUSTED_ACCOUNT_IDS) + r")\d{12}:",
        "P-S3",
        "IAM Role 禁止信任外部帳號",
        "IAM Role 不可信任組織外的 AWS 帳號",
        "只能信任組織內帳號 (" + ", ".join(TRUSTED_ACCOUNT_IDS) + ")",
    ),

    # -------------------------------------------------------------------------
    # Code Scanning - 硬編碼憑證 (CS)
    # -------------------------------------------------------------------------
    (
        r"AKIA[0-9A-Z]{16}",
        "CS-HC001",
        "禁止硬編碼 Access Key",
        "命令中發現 AWS Access Key ID",
        "使用 IAM Role 或 Secrets Manager 管理憑證",
    ),
    (
        r"(?i)(aws_secret_access_key|secret_access_key)\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}",
        "CS-HC002",
        "禁止硬編碼 Secret Key",
        "命令中發現 AWS Secret Access Key",
        "使用 IAM Role 或 Secrets Manager 管理憑證",
    ),
    (
        r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
        "CS-HC003",
        "禁止硬編碼私鑰",
        "命令中發現私鑰內容",
        "使用 Secrets Manager 或 Parameter Store 管理私鑰",
    ),

    # -------------------------------------------------------------------------
    # 網路安全 - 公開 CIDR (P-S2)
    # -------------------------------------------------------------------------
    (
        r"ec2\s+authorize-security-group-ingress.*--cidr\s+0\.0\.0\.0/0.*--protocol\s+(-1|all)",
        "P-S2",
        "Security Group 禁止全開",
        "Security Group 不可對所有 IP 開放所有流量",
        "指定具體的 IP 範圍和端口",
    ),
    (
        r"ec2\s+authorize-security-group-ingress.*--cidr\s+0\.0\.0\.0/0.*--port\s+(22|3389|3306|5432|1433|27017|6379|11211)",
        "P-S2",
        "敏感端口禁止公開",
        "SSH/RDP/資料庫端口不可對所有 IP 開放",
        "使用 VPN 或 Bastion Host，限制來源 IP",
    ),

    # -------------------------------------------------------------------------
    # Lambda - 危險環境變數操作 (B-LAMBDA-01)
    # -------------------------------------------------------------------------
    # 攔截 lambda update-function-configuration --environment（覆蓋式更新，會清空所有 env vars）
    (
        r"lambda\s+update-function-configuration\s+.*--environment\s+",
        "B-LAMBDA-01",
        "Lambda 危險 --environment 覆蓋",
        "lambda update-function-configuration --environment 會完整覆蓋所有環境變數，清空未列出的 vars",
        "使用 update-function-configuration 前先 get-function-configuration 取得完整 env vars，或用 AWS Console / Terraform 管理",
    ),

    # -------------------------------------------------------------------------
    # EC2 Instance Attribute - 細粒度控制 (B-EC2)
    # -------------------------------------------------------------------------
    # 禁止危險的 attribute 修改
    (
        r"ec2\s+modify-instance-attribute.*--user-data",
        "B-EC2-01",
        "禁止修改 User Data",
        "修改 User Data 可注入啟動腳本執行任意代碼",
        "使用 SSM Run Command 或重建 instance",
    ),
    (
        r"ec2\s+modify-instance-attribute.*(--iam-instance-profile|--instance-profile)",
        "B-EC2-02",
        "禁止直接修改 Instance Profile",
        "修改 Instance Profile 可能導致權限提升",
        "透過 associate-iam-instance-profile 並需要審批",
    ),
    (
        r"ec2\s+modify-instance-attribute.*--source-dest-check\s+false",
        "B-EC2-03",
        "禁止關閉 Source/Dest Check",
        "關閉 Source/Dest Check 可讓 instance 成為網路跳板",
        "只有 NAT instance 需要關閉，請提供具體理由",
    ),
    (
        r"ec2\s+modify-instance-attribute.*--kernel",
        "B-EC2-04",
        "禁止修改 Kernel",
        "修改 Kernel 可能影響系統安全性",
        "使用標準 AMI",
    ),
    (
        r"ec2\s+modify-instance-attribute.*--ramdisk",
        "B-EC2-05",
        "禁止修改 Ramdisk",
        "修改 Ramdisk 可能影響系統安全性",
        "使用標準 AMI",
    ),
    # 允許的 attribute（不在此列表中的會被審批流程處理）：
    # --instance-type, --cpu-options, --disable-api-termination, --ebs-optimized, etc.
]


def _normalize_json_payload(command: str) -> str:
    """
    SEC-008: 嘗試 parse + re-serialize JSON payload 正規化。

    目標：將 {"Key": "val"} 和 { "Key" :  "val" } 正規化成同一形式，
    避免透過 JSON 格式變化（多空白、換行、key 順序）繞過正則。

    只對能成功 parse 的 JSON 片段做；無法 parse 則 fallback 原始命令。
    """
    # 找出命令中可能的 JSON 片段（用 { ... } 包住）並嘗試正規化
    def _try_normalize(m: re.Match) -> str:
        fragment = m.group(0)
        try:
            parsed = json.loads(fragment)
            return json.dumps(parsed, separators=(',', ':'))
        except (json.JSONDecodeError, ValueError):
            return fragment

    try:
        return re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', _try_normalize, command)
    except re.error:
        return command


def check_compliance(command: str) -> tuple[bool, Optional[ComplianceViolation]]:
    """
    檢查命令是否違反合規規則

    Args:
        command: AWS CLI 命令

    Returns:
        (is_compliant, violation): 如果違規，返回 violation 詳情
    """
    if not command:
        return True, None

    # SEC-008: 正規化 JSON payload，防止格式變化繞過正則
    normalized_command = _normalize_json_payload(command)

    for pattern, rule_id, rule_name, description, remediation in COMPLIANCE_RULES:
        # 同時對原始命令和正規化命令做檢查
        if re.search(pattern, command, re.IGNORECASE) or re.search(pattern, normalized_command, re.IGNORECASE):
            return False, ComplianceViolation(
                rule_id=rule_id,
                rule_name=rule_name,
                description=description,
                remediation=remediation,
            )

    return True, None


def format_violation_message(violation: ComplianceViolation) -> str:
    """
    格式化違規訊息（用於 Telegram 通知）

    Args:
        violation: ComplianceViolation 物件

    Returns:
        格式化的 Markdown 訊息
    """
    return f"""🚫 *合規違規 \\- 操作已攔截*

📋 *規則*: `{violation.rule_id}` \\- {_escape_md(violation.rule_name)}
❌ *違規說明*: {_escape_md(violation.description)}
💡 *修正建議*: {_escape_md(violation.remediation)}

如有疑問，請聯繫安全團隊。"""


def _escape_md(text: str) -> str:
    """Escape Telegram MarkdownV2 特殊字元"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


def get_all_rules() -> list[dict]:
    """
    取得所有合規規則（用於文檔或 API）

    Returns:
        規則列表
    """
    return [
        {
            'rule_id': rule_id,
            'rule_name': rule_name,
            'description': description,
            'remediation': remediation,
        }
        for _, rule_id, rule_name, description, remediation in COMPLIANCE_RULES
    ]
