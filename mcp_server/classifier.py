"""
Bouncer MCP Server - 命令分類與執行
從 Lambda 版本移植，移除 AWS Lambda 特定依賴
"""

import os
import shlex
import subprocess
from typing import Tuple, Optional

# ============================================================================
# 命令分類系統（四層）
# ============================================================================

# Layer 1: BLOCKED - 永遠拒絕
BLOCKED_PATTERNS = [
    # IAM 危險操作
    'iam create', 'iam delete', 'iam attach', 'iam detach', 
    'iam put', 'iam update', 'iam add', 'iam remove',
    # STS 危險操作
    'sts assume-role',
    # Organizations
    'organizations ',
    # Shell 注入
    ';', '|', '&&', '||', '`', '$(', '${',
    'rm -rf', 'sudo ', '> /dev', 'chmod 777',
    # 其他危險
    'delete-account', 'close-account',
]

# Layer 2: SAFELIST - 自動批准（Read-only）
SAFELIST_PREFIXES = [
    # EC2
    'aws ec2 describe-',
    # S3 (read-only)
    'aws s3 ls', 'aws s3api list-', 'aws s3api get-',
    # RDS
    'aws rds describe-',
    # Lambda
    'aws lambda list-', 'aws lambda get-',
    # CloudWatch
    'aws logs describe-', 'aws logs get-', 'aws logs filter-log-events',
    'aws cloudwatch describe-', 'aws cloudwatch get-', 'aws cloudwatch list-',
    # IAM (read-only)
    'aws iam list-', 'aws iam get-',
    # STS
    'aws sts get-caller-identity',
    # SSM (read-only)
    'aws ssm describe-', 'aws ssm get-', 'aws ssm list-',
    # Route53 (read-only)
    'aws route53 list-', 'aws route53 get-',
    # ECS/EKS (read-only)
    'aws ecs describe-', 'aws ecs list-',
    'aws eks describe-', 'aws eks list-',
]


def classify_command(command: str) -> str:
    """
    分類命令
    
    Returns:
        'BLOCKED' - 永遠拒絕
        'SAFELIST' - 自動執行
        'APPROVAL' - 需要人工審批
    """
    cmd_lower = command.lower().strip()
    
    # Layer 1: BLOCKED
    for pattern in BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return 'BLOCKED'
    
    # Layer 2: SAFELIST
    for prefix in SAFELIST_PREFIXES:
        if cmd_lower.startswith(prefix):
            return 'SAFELIST'
    
    # Layer 3 & 4: 需要審批
    return 'APPROVAL'


def is_valid_aws_command(command: str) -> Tuple[bool, Optional[str]]:
    """
    驗證是否為有效的 AWS CLI 命令
    
    Returns:
        (is_valid, error_message)
    """
    command = command.strip()
    
    if not command:
        return False, "Command is empty"
    
    if not command.startswith('aws '):
        return False, "Only AWS CLI commands are allowed (must start with 'aws ')"
    
    try:
        args = shlex.split(command)
        if len(args) < 2:
            return False, "Invalid AWS command format"
    except ValueError as e:
        return False, f"Command parsing error: {e}"
    
    return True, None


def execute_command(
    command: str,
    credentials_file: Optional[str] = None,
    timeout: int = 60
) -> Tuple[str, int]:
    """
    執行 AWS CLI 命令
    
    Args:
        command: AWS CLI 命令
        credentials_file: 可選的 AWS credentials 檔案路徑
        timeout: 執行超時（秒）
    
    Returns:
        (output, exit_code)
    """
    # 驗證命令
    is_valid, error = is_valid_aws_command(command)
    if not is_valid:
        return f"❌ {error}", 1
    
    try:
        args = shlex.split(command)
        
        # 設定環境變數
        env = os.environ.copy()
        env['AWS_PAGER'] = ''  # 禁用 pager
        
        if credentials_file:
            env['AWS_SHARED_CREDENTIALS_FILE'] = credentials_file
        
        # 執行命令（使用 subprocess，不用 shell=True）
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        
        output = result.stdout or result.stderr or '(no output)'
        return output.strip()[:10000], result.returncode
        
    except subprocess.TimeoutExpired:
        return f"❌ Command timed out after {timeout} seconds", 124
    except FileNotFoundError:
        return "❌ AWS CLI not found. Is it installed?", 127
    except Exception as e:
        return f"❌ Execution error: {e}", 1


# ============================================================================
# 輔助函數
# ============================================================================

def get_safelist() -> list:
    """取得 safelist 前綴列表"""
    return SAFELIST_PREFIXES.copy()


def get_blocked_patterns() -> list:
    """取得 blocked pattern 列表"""
    return BLOCKED_PATTERNS.copy()
