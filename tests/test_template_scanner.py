"""
Bouncer - Template Scanner Tests (Phase 4)
模板/Payload 掃描器完整測試

測試類別：
1. JSON Payload 提取 (TestExtractPayloads)
2. TP-001: Action Wildcard (TestActionWildcard)
3. TP-002: Resource Wildcard (TestResourceWildcard)
4. TP-003: Principal Wildcard (TestPrincipalWildcard)
5. TP-004: External Account Trust (TestExternalAccountTrust)
6. TP-005: Open Ingress (TestOpenIngress)
7. TP-006: High Risk Port (TestHighRiskPort)
8. TP-007: Hardcoded Secret (TestHardcodedSecret)
9. TP-008: Admin Policy (TestAdminPolicy)
10. TP-009: Public Access (TestPublicAccess)
11. Negative Tests (TestNegativeCases)
12. Edge Cases (TestEdgeCases)
13. Integration (TestIntegration)
"""

import json
import pytest
import sys
from pathlib import Path

# 確保可以 import src
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from template_scanner import (
    extract_json_payloads,
    scan_payload,
    scan_command_payloads,
    check_action_wildcard,
    check_resource_wildcard,
    check_principal_wildcard,
    check_external_account_trust,
    check_open_ingress,
    check_high_risk_port,
    check_hardcoded_secret,
    check_admin_policy,
    check_public_access,
    KNOWN_ACCOUNT_IDS,
)
from risk_scorer import (
    RiskFactor,
    RiskRules,
    ParsedCommand,
    score_parameters,
    create_default_rules,
    load_risk_rules,
    _dict_to_rules,
)


# ============================================================================
# Helpers
# ============================================================================

def _make_template_rules():
    """建立完整的 template_rules 列表"""
    return [
        {"id": "TP-001", "name": "Wildcard Action", "target": "iam_policy",
         "check": "action_wildcard", "score": 90, "description": "Action:*"},
        {"id": "TP-002", "name": "Wildcard Resource", "target": "iam_policy",
         "check": "resource_wildcard", "score": 85, "description": "Resource:*"},
        {"id": "TP-003", "name": "Wildcard Principal", "target": "trust_policy",
         "check": "principal_wildcard", "score": 90, "description": "Principal:*"},
        {"id": "TP-004", "name": "External Account Trust", "target": "trust_policy",
         "check": "external_account_trust", "score": 80, "description": "External account"},
        {"id": "TP-005", "name": "Open Ingress", "target": "security_group",
         "check": "open_ingress", "score": 75, "description": "Open ingress"},
        {"id": "TP-006", "name": "High Risk Port", "target": "security_group",
         "check": "high_risk_port", "score": 85, "description": "High-risk port"},
        {"id": "TP-007", "name": "Hardcoded Secret", "target": "lambda_env",
         "check": "hardcoded_secret", "score": 80, "description": "Hardcoded secret"},
        {"id": "TP-008", "name": "Admin Policy", "target": "iam_policy",
         "check": "admin_policy", "score": 95, "description": "Admin policy"},
        {"id": "TP-009", "name": "Public Access", "target": "bucket_policy",
         "check": "public_access", "score": 85, "description": "Public access"},
    ]


# ============================================================================
# 1. JSON Payload Extraction Tests
# ============================================================================

class TestExtractPayloads:
    """測試 JSON payload 提取"""

    def test_extract_policy_document(self):
        cmd = (
            'aws iam put-role-policy --role-name test --policy-name test '
            """--policy-document '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'"""
        )
        results = extract_json_payloads(cmd)
        assert len(results) == 1
        assert results[0][0] == '--policy-document'
        assert 'Statement' in results[0][1]

    def test_extract_bare_json(self):
        cmd = (
            'aws iam put-role-policy --role-name test --policy-name test '
            '--policy-document {"Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}]}'
        )
        results = extract_json_payloads(cmd)
        assert len(results) == 1
        assert results[0][0] == '--policy-document'

    def test_extract_ip_permissions(self):
        cmd = (
            'aws ec2 authorize-security-group-ingress --group-id sg-123 '
            """--ip-permissions '[{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'"""
        )
        results = extract_json_payloads(cmd)
        assert len(results) == 1
        assert results[0][0] == '--ip-permissions'

    def test_extract_environment(self):
        cmd = (
            'aws lambda update-function-configuration --function-name test '
            """--environment '{"Variables":{"SECRET_KEY":"abc123"}}'"""
        )
        results = extract_json_payloads(cmd)
        assert len(results) == 1
        assert results[0][0] == '--environment'

    def test_no_json_in_command(self):
        cmd = 'aws ec2 describe-instances --instance-ids i-12345'
        results = extract_json_payloads(cmd)
        assert results == []

    def test_file_reference_skipped(self):
        cmd = 'aws iam put-role-policy --role-name test --policy-document file://policy.json'
        results = extract_json_payloads(cmd)
        assert results == []

    def test_empty_command(self):
        assert extract_json_payloads('') == []
        assert extract_json_payloads(None) == []

    def test_multiple_payloads(self):
        cmd = (
            'aws iam create-role --role-name test '
            """--assume-role-policy-document '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"sts:AssumeRole"}]}' """
            """--policy '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'"""
        )
        results = extract_json_payloads(cmd)
        assert len(results) == 2
        param_names = {r[0] for r in results}
        assert '--assume-role-policy-document' in param_names
        assert '--policy' in param_names


# ============================================================================
# 2. TP-001: Action Wildcard
# ============================================================================

class TestActionWildcard:
    """TP-001: IAM Policy Action:*"""

    def test_action_star_string(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        result = check_action_wildcard(payload)
        assert result is not None
        assert result[0] == 90

    def test_action_star_list(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": ["*"], "Resource": "*"}]}
        result = check_action_wildcard(payload)
        assert result is not None

    def test_specific_action(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}
        result = check_action_wildcard(payload)
        assert result is None

    def test_multiple_actions(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "*"}]}
        result = check_action_wildcard(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws iam put-role-policy --role-name test --policy-name test '
            """--policy-document '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp001 = [f for f in factors if 'TP-001' in (f.details or '')]
        assert len(tp001) >= 1
        assert score >= 90


# ============================================================================
# 3. TP-002: Resource Wildcard
# ============================================================================

class TestResourceWildcard:
    """TP-002: IAM Policy Resource:*"""

    def test_resource_star_string(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}
        result = check_resource_wildcard(payload)
        assert result is not None
        assert result[0] == 85

    def test_resource_star_list(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": ["*"]}]}
        result = check_resource_wildcard(payload)
        assert result is not None

    def test_specific_resource(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "arn:aws:s3:::my-bucket"}]}
        result = check_resource_wildcard(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws iam put-role-policy --role-name test --policy-name test '
            """--policy-document '{"Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp002 = [f for f in factors if 'TP-002' in (f.details or '')]
        assert len(tp002) >= 1


# ============================================================================
# 4. TP-003: Principal Wildcard
# ============================================================================

class TestPrincipalWildcard:
    """TP-003: Trust/Bucket Policy Principal:*"""

    def test_principal_star(self):
        payload = {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]}
        result = check_principal_wildcard(payload)
        assert result is not None
        assert result[0] == 90

    def test_principal_aws_star(self):
        payload = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}
        result = check_principal_wildcard(payload)
        assert result is not None

    def test_principal_specific_account(self):
        payload = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::190825685292:root"}, "Action": "sts:AssumeRole"}]}
        result = check_principal_wildcard(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws iam create-role --role-name test '
            """--assume-role-policy-document '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"sts:AssumeRole"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp003 = [f for f in factors if 'TP-003' in (f.details or '')]
        assert len(tp003) >= 1


# ============================================================================
# 5. TP-004: External Account Trust
# ============================================================================

class TestExternalAccountTrust:
    """TP-004: Trust Policy with external AWS account"""

    def test_external_account(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::999999999999:root"},
                "Action": "sts:AssumeRole",
            }],
        }
        result = check_external_account_trust(payload)
        assert result is not None
        assert result[0] == 80
        assert '999999999999' in result[1]

    def test_known_account(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::190825685292:root"},
                "Action": "sts:AssumeRole",
            }],
        }
        result = check_external_account_trust(payload)
        assert result is None

    def test_all_known_accounts(self):
        """驗證所有已知帳號都不會觸發"""
        for account_id in KNOWN_ACCOUNT_IDS:
            payload = {
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                    "Action": "sts:AssumeRole",
                }],
            }
            result = check_external_account_trust(payload)
            assert result is None, f"Known account {account_id} should not trigger"

    def test_multiple_principals_with_external(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": [
                    "arn:aws:iam::190825685292:root",
                    "arn:aws:iam::111111111111:root",
                ]},
                "Action": "sts:AssumeRole",
            }],
        }
        result = check_external_account_trust(payload)
        assert result is not None
        assert '111111111111' in result[1]

    def test_command_integration(self):
        cmd = (
            'aws iam create-role --role-name test '
            """--assume-role-policy-document '{"Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::999999999999:root"},"Action":"sts:AssumeRole"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp004 = [f for f in factors if 'TP-004' in (f.details or '')]
        assert len(tp004) >= 1


# ============================================================================
# 6. TP-005: Open Ingress
# ============================================================================

class TestOpenIngress:
    """TP-005: Security Group 0.0.0.0/0"""

    def test_open_ipv4(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_open_ingress(payload)
        assert result is not None
        assert result[0] == 75

    def test_open_ipv6(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                     "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}]
        result = check_open_ingress(payload)
        assert result is not None

    def test_private_cidr(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                     "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]
        result = check_open_ingress(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws ec2 authorize-security-group-ingress --group-id sg-123 '
            """--ip-permissions '[{"IpProtocol":"tcp","FromPort":80,"ToPort":80,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp005 = [f for f in factors if 'TP-005' in (f.details or '')]
        assert len(tp005) >= 1


# ============================================================================
# 7. TP-006: High Risk Port
# ============================================================================

class TestHighRiskPort:
    """TP-006: SG high-risk port + 0.0.0.0/0"""

    def test_ssh_port_22(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_high_risk_port(payload)
        assert result is not None
        assert result[0] == 85
        assert '22' in result[1]

    def test_rdp_port_3389(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 3389, "ToPort": 3389,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_high_risk_port(payload)
        assert result is not None

    def test_mysql_port_3306(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_high_risk_port(payload)
        assert result is not None

    def test_port_range_includes_high_risk(self):
        """Port range 0-65535 includes all high-risk ports"""
        payload = [{"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_high_risk_port(payload)
        assert result is not None

    def test_safe_port_80(self):
        """Port 80 is not a high-risk port"""
        payload = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = check_high_risk_port(payload)
        assert result is None

    def test_high_risk_port_private_cidr(self):
        """High-risk port but private CIDR — should NOT trigger"""
        payload = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                     "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]
        result = check_high_risk_port(payload)
        assert result is None

    def test_high_risk_port_ipv6_open(self):
        payload = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                     "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}]
        result = check_high_risk_port(payload)
        assert result is not None

    def test_command_integration(self):
        cmd = (
            'aws ec2 authorize-security-group-ingress --group-id sg-123 '
            """--ip-permissions '[{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp006 = [f for f in factors if 'TP-006' in (f.details or '')]
        assert len(tp006) >= 1
        assert score >= 85


# ============================================================================
# 8. TP-007: Hardcoded Secret
# ============================================================================

class TestHardcodedSecret:
    """TP-007: Lambda environment with hardcoded secrets"""

    def test_secret_key(self):
        payload = {"Variables": {"SECRET_KEY": "abc123", "APP_NAME": "test"}}
        result = check_hardcoded_secret(payload)
        assert result is not None
        assert result[0] == 80

    def test_password_key(self):
        payload = {"Variables": {"DB_PASSWORD": "p@ss", "DB_HOST": "localhost"}}
        result = check_hardcoded_secret(payload)
        assert result is not None

    def test_api_key(self):
        payload = {"Variables": {"API_KEY": "key123"}}
        result = check_hardcoded_secret(payload)
        assert result is not None

    def test_token_key(self):
        payload = {"Variables": {"AUTH_TOKEN": "tok123"}}
        result = check_hardcoded_secret(payload)
        assert result is not None

    def test_safe_env_vars(self):
        payload = {"Variables": {"APP_NAME": "test", "LOG_LEVEL": "debug", "REGION": "us-east-1"}}
        result = check_hardcoded_secret(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws lambda update-function-configuration --function-name test '
            """--environment '{"Variables":{"SECRET_KEY":"abc123","APP_NAME":"test"}}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp007 = [f for f in factors if 'TP-007' in (f.details or '')]
        assert len(tp007) >= 1


# ============================================================================
# 9. TP-008: Admin Policy
# ============================================================================

class TestAdminPolicy:
    """TP-008: Full admin policy (Allow + Action:* + Resource:*)"""

    def test_full_admin(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        result = check_admin_policy(payload)
        assert result is not None
        assert result[0] == 95

    def test_admin_with_list_values(self):
        payload = {"Statement": [{"Effect": "Allow", "Action": ["*"], "Resource": ["*"]}]}
        result = check_admin_policy(payload)
        assert result is not None

    def test_deny_all(self):
        """Deny + Action:* + Resource:* is NOT admin — it's a deny policy"""
        payload = {"Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}]}
        result = check_admin_policy(payload)
        assert result is None

    def test_specific_action_star_resource(self):
        """Specific Action but Resource:* is NOT full admin"""
        payload = {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
        result = check_admin_policy(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws iam put-role-policy --role-name admin --policy-name admin '
            """--policy-document '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp008 = [f for f in factors if 'TP-008' in (f.details or '')]
        assert len(tp008) >= 1
        assert score >= 95


# ============================================================================
# 10. TP-009: Public Access
# ============================================================================

class TestPublicAccess:
    """TP-009: Public access (Principal:* + Allow)"""

    def test_public_bucket_policy(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::my-bucket/*",
            }],
        }
        result = check_public_access(payload)
        assert result is not None
        assert result[0] == 85

    def test_public_access_aws_star(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::my-bucket/*",
            }],
        }
        result = check_public_access(payload)
        assert result is not None

    def test_deny_principal_star(self):
        """Deny + Principal:* should NOT trigger (it's a restriction)"""
        payload = {
            "Statement": [{
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": "arn:aws:s3:::my-bucket/*",
            }],
        }
        result = check_public_access(payload)
        assert result is None

    def test_specific_principal(self):
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::190825685292:root"},
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::my-bucket/*",
            }],
        }
        result = check_public_access(payload)
        assert result is None

    def test_command_integration(self):
        cmd = (
            'aws s3api put-bucket-policy --bucket my-bucket '
            """--policy '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::my-bucket/*"}]}'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp009 = [f for f in factors if 'TP-009' in (f.details or '')]
        assert len(tp009) >= 1


# ============================================================================
# 11. Negative Tests
# ============================================================================

class TestNegativeCases:
    """安全的 policy 不應觸發任何告警"""

    def test_safe_iam_policy(self):
        """Narrow permissions — no warnings"""
        payload = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": ["arn:aws:s3:::my-bucket", "arn:aws:s3:::my-bucket/*"],
            }],
        }
        for check_fn in [check_action_wildcard, check_resource_wildcard,
                         check_admin_policy, check_public_access]:
            assert check_fn(payload) is None

    def test_safe_trust_policy(self):
        """Trust policy with known account"""
        payload = {
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::190825685292:root"},
                "Action": "sts:AssumeRole",
            }],
        }
        assert check_principal_wildcard(payload) is None
        assert check_external_account_trust(payload) is None
        assert check_public_access(payload) is None

    def test_safe_security_group(self):
        """Private CIDR security group"""
        payload = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                     "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]
        assert check_open_ingress(payload) is None
        assert check_high_risk_port(payload) is None

    def test_safe_lambda_env(self):
        """Normal environment variables"""
        payload = {"Variables": {"APP_NAME": "test", "LOG_LEVEL": "info"}}
        assert check_hardcoded_secret(payload) is None

    def test_readonly_command_no_template_hits(self):
        """Read-only commands shouldn't have template payloads"""
        cmd = 'aws ec2 describe-instances --instance-ids i-12345'
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        assert score == 0
        assert factors == []


# ============================================================================
# 12. Edge Cases
# ============================================================================

class TestEdgeCases:
    """邊界案例和錯誤處理"""

    def test_malformed_json_returns_empty(self):
        """Malformed JSON should not crash"""
        cmd = 'aws iam put-role-policy --policy-document {not-valid-json'
        results = extract_json_payloads(cmd)
        assert results == []

    def test_empty_json_object(self):
        cmd = "aws iam put-role-policy --policy-document '{}'"
        results = extract_json_payloads(cmd)
        assert len(results) == 1
        # Empty policy should not trigger any checks
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        assert score == 0

    def test_empty_statement_list(self):
        payload = {"Statement": []}
        assert check_action_wildcard(payload) is None
        assert check_admin_policy(payload) is None

    def test_no_statement_key(self):
        payload = {"Version": "2012-10-17"}
        assert check_action_wildcard(payload) is None
        assert check_resource_wildcard(payload) is None

    def test_statement_as_dict_not_list(self):
        """Statement as single dict instead of list"""
        payload = {"Statement": {"Effect": "Allow", "Action": "*", "Resource": "*"}}
        result = check_action_wildcard(payload)
        assert result is not None

    def test_ip_permissions_dict_wrapper(self):
        """ip-permissions wrapped in IpPermissions key"""
        payload = {"IpPermissions": [
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ]}
        result = check_open_ingress(payload)
        assert result is not None

    def test_scan_with_empty_rules(self):
        cmd = "aws iam put-role-policy --policy-document '{\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}'"
        score, factors = scan_command_payloads(cmd, [])
        assert score == 0
        assert factors == []

    def test_scan_with_none_command(self):
        score, factors = scan_command_payloads('', _make_template_rules())
        assert score == 0
        assert factors == []

    def test_unknown_check_name_ignored(self):
        """Unknown check name in rules should be silently ignored"""
        rules = [{"id": "TP-999", "name": "Unknown", "check": "nonexistent_check", "score": 50}]
        cmd = "aws iam put-role-policy --policy-document '{\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}'"
        score, factors = scan_command_payloads(cmd, rules)
        # Should not crash, unknown checks ignored
        assert isinstance(score, int)

    def test_multiple_statements_mixed(self):
        """Multiple statements, only some are risky"""
        payload = {
            "Statement": [
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::bucket/*"},
                {"Effect": "Allow", "Action": "*", "Resource": "*"},
            ],
        }
        result = check_admin_policy(payload)
        assert result is not None  # Second statement triggers

    def test_case_sensitivity_effect(self):
        """Effect field case handling"""
        payload = {"Statement": [{"Effect": "allow", "Action": "*", "Resource": "*"}]}
        result = check_admin_policy(payload)
        assert result is not None  # Should handle lowercase


# ============================================================================
# 13. Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests: score_parameters picks up template factors"""

    def test_score_parameters_includes_template_factors(self):
        """verify score_parameters integrates template scanner"""
        cmd = (
            'aws iam put-role-policy --role-name admin --policy-name admin '
            """--policy-document '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'"""
        )
        parsed = ParsedCommand(
            original=cmd,
            service='iam',
            action='put-role-policy',
            verb='put',
            resource_type='role-policy',
            parameters={'--role-name': 'admin', '--policy-name': 'admin'},
            flags=[],
        )
        rules = create_default_rules()
        score, factors = score_parameters(parsed, rules)

        # Template factors should be present
        template_factors = [f for f in factors if 'Template' in f.name]
        assert len(template_factors) > 0, "Template scanner should find violations"

        # At least TP-001 (Action:*), TP-002 (Resource:*), TP-008 (admin) should fire
        tp_ids_found = set()
        for f in template_factors:
            if f.details:
                for tp_id in ['TP-001', 'TP-002', 'TP-008']:
                    if tp_id in f.details:
                        tp_ids_found.add(tp_id)

        assert 'TP-008' in tp_ids_found, "Should detect full admin policy"

    def test_score_parameters_safe_command_no_template_hits(self):
        """Safe command should not get template factors"""
        cmd = 'aws ec2 describe-instances --instance-ids i-12345'
        parsed = ParsedCommand(
            original=cmd,
            service='ec2',
            action='describe-instances',
            verb='describe',
            resource_type='instances',
            parameters={'--instance-ids': 'i-12345'},
            flags=[],
        )
        rules = create_default_rules()
        score, factors = score_parameters(parsed, rules)

        template_factors = [f for f in factors if 'Template' in f.name]
        assert len(template_factors) == 0

    def test_rules_load_template_rules(self):
        """create_default_rules should load template_rules from JSON"""
        rules = create_default_rules()
        assert hasattr(rules, 'template_rules')
        assert len(rules.template_rules) == 9

    def test_dict_to_rules_includes_template_rules(self):
        """_dict_to_rules should include template_rules"""
        data = {
            "version": "test",
            "template_rules": [
                {"id": "TP-001", "check": "action_wildcard", "score": 90},
            ],
        }
        rules = _dict_to_rules(data)
        assert len(rules.template_rules) == 1

    def test_dict_to_rules_default_empty(self):
        """template_rules defaults to empty list"""
        data = {"version": "test"}
        rules = _dict_to_rules(data)
        assert rules.template_rules == []

    def test_security_group_both_tp005_and_tp006(self):
        """SG with SSH on 0.0.0.0/0 should trigger both TP-005 and TP-006"""
        cmd = (
            'aws ec2 authorize-security-group-ingress --group-id sg-123 '
            """--ip-permissions '[{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'"""
        )
        rules = _make_template_rules()
        score, factors = scan_command_payloads(cmd, rules)
        tp_ids = set()
        for f in factors:
            if f.details:
                for tp_id in ['TP-005', 'TP-006']:
                    if tp_id in f.details:
                        tp_ids.add(tp_id)
        assert 'TP-005' in tp_ids, "Should detect open ingress"
        assert 'TP-006' in tp_ids, "Should detect high-risk port"
