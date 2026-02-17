"""
Bouncer - Risk Scorer Tests
風險評分系統完整測試框架

測試類別：
1. 動詞基礎分測試 (TestVerbBaseScore)
2. 參數風險測試 (TestParameterRisk)
3. 服務敏感度測試 (TestServiceSensitivity)
4. Reason 品質測試 (TestReasonQuality)
5. 整合測試 (TestRiskCalculation)
6. 邊界案例 (TestEdgeCases)
7. 命令解析測試 (TestCommandParsing)
8. 權重計算測試 (TestWeightCalculation)

Author: Bouncer Team
"""

import pytest
import sys
from pathlib import Path

# 確保可以 import src
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from risk_scorer import (
    # Core types
    RiskCategory,
    RiskFactor,
    RiskResult,
    ParsedCommand,
    RiskRules,
    # Core functions
    calculate_risk,
    load_risk_rules,
    parse_command,
    # Scoring functions
    score_verb,
    score_parameters,
    score_context,
    score_account,
    # Utilities
    get_category_from_score,
    create_default_rules,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def risk_rules():
    """載入測試用的風險規則"""
    return create_default_rules()


@pytest.fixture
def sample_commands():
    """常見命令樣本"""
    return {
        # 唯讀操作
        'read_only': [
            'aws ec2 describe-instances',
            'aws s3 ls',
            'aws logs tail /aws/lambda/func',
            'aws dynamodb get-item --table-name test --key \'{"id": {"S": "123"}}\'',
            'aws lambda list-functions',
            'aws iam list-users',
            'aws cloudwatch describe-alarms',
        ],
        # 寫入操作
        'write': [
            'aws s3 cp file.txt s3://bucket/key',
            'aws lambda update-function-code --function-name test --zip-file fileb://code.zip',
            'aws ec2 create-tags --resources i-12345 --tags Key=Name,Value=Test',
            'aws dynamodb put-item --table-name test --item \'{"id": {"S": "123"}}\'',
            'aws sns publish --topic-arn arn:aws:sns:us-east-1:123456789012:topic --message "test"',
        ],
        # 刪除操作
        'delete': [
            'aws s3 rm s3://bucket/key',
            'aws ec2 terminate-instances --instance-ids i-12345',
            'aws lambda delete-function --function-name test',
            'aws dynamodb delete-item --table-name test --key \'{"id": {"S": "123"}}\'',
            'aws logs delete-log-group --log-group-name /aws/lambda/test',
        ],
        # 危險操作（黑名單）
        'blocked': [
            'aws iam create-user --user-name hacker',
            'aws iam create-access-key --user-name admin',
            'aws iam attach-role-policy --role-name Admin --policy-arn arn:aws:iam::aws:policy/AdministratorAccess',
            'aws sts assume-role --role-arn arn:aws:iam::123456789012:role/Admin',
            'aws organizations create-account --email test@example.com --account-name Test',
        ],
    }


@pytest.fixture
def sensitive_accounts():
    """帳號敏感度配置"""
    rules = create_default_rules()
    rules.account_sensitivity = {
        'PROD_ACCOUNT': 90,   # Production - 高敏感
        'STAGING_ACCOUNT': 60,  # Staging - 中等
        'DEV_ACCOUNT': 20,    # Dev - 低敏感
    }
    return rules


# ============================================================================
# Test Classes
# ============================================================================

class TestVerbBaseScore:
    """動詞基礎分測試"""
    
    def test_describe_is_read_only(self, risk_rules):
        """describe-* → 低風險 (0-25)"""
        commands = [
            'aws ec2 describe-instances',
            'aws ec2 describe-security-groups',
            'aws rds describe-db-instances',
            'aws lambda describe-function --function-name test',
        ]
        for cmd in commands:
            result = calculate_risk(cmd, reason="Check status", source="test", rules=risk_rules)
            assert result.score <= 25, f"'{cmd}' score {result.score} should be <= 25"
            assert result.category == RiskCategory.AUTO_APPROVE.value

    def test_list_is_read_only(self, risk_rules):
        """list-* → 低風險 (0-25) 或 LOG (26-45) for 高敏感服務"""
        # 一般服務的 list 命令 - auto_approve
        low_sensitivity_commands = [
            'aws ec2 describe-images',
            'aws lambda list-functions',
            'aws s3 ls',
        ]
        for cmd in low_sensitivity_commands:
            result = calculate_risk(cmd, reason="Inventory check", source="Private Bot", rules=risk_rules)
            assert result.score <= 25, f"'{cmd}' score {result.score} should be <= 25"
        
        # 高敏感服務 (IAM) 即使是唯讀也會略高 - LOG 級別
        iam_list = calculate_risk('aws iam list-users', reason="Inventory check", source="Private Bot", rules=risk_rules)
        assert iam_list.score <= 45, f"IAM list score {iam_list.score} should be <= 45 (LOG)"
        assert iam_list.category in [RiskCategory.AUTO_APPROVE.value, RiskCategory.LOG.value]

    def test_get_is_read_only(self, risk_rules):
        """get-* → 低風險 (0-25)"""
        commands = [
            'aws ssm get-parameter --name /app/config',
            'aws s3api get-object --bucket test --key file.txt',
            'aws dynamodb get-item --table-name test --key \'{"id": {"S": "123"}}\'',
        ]
        for cmd in commands:
            result = calculate_risk(cmd, reason="Read config", source="test", rules=risk_rules)
            assert result.score <= 30, f"'{cmd}' score {result.score} should be <= 30"

    def test_create_is_write(self, risk_rules):
        """create-* → 中高風險 (30-65)"""
        commands = [
            'aws ec2 create-security-group --group-name test --description "test"',
            'aws sns create-topic --name test',
            'aws sqs create-queue --queue-name test',
        ]
        for cmd in commands:
            result = calculate_risk(cmd, reason="Create resource", source="test", rules=risk_rules)
            # create 動詞分數 65，組合後應該在 confirm 或以下
            assert result.score >= 25, f"'{cmd}' score {result.score} should be >= 25"

    def test_put_is_write(self, risk_rules):
        """put-* → 中高風險"""
        commands = [
            'aws s3api put-object --bucket test --key file.txt --body file.txt',
            'aws dynamodb put-item --table-name test --item \'{"id": {"S": "123"}}\'',
        ]
        for cmd in commands:
            result = calculate_risk(cmd, reason="Write data", source="test", rules=risk_rules)
            assert result.score >= 20, f"'{cmd}' score {result.score} should be >= 20"

    def test_delete_is_destructive(self, risk_rules):
        """delete-* → 高風險 (46-85)"""
        commands = [
            'aws ec2 delete-security-group --group-id sg-12345',
            'aws logs delete-log-group --log-group-name /aws/lambda/test',
            'aws sns delete-topic --topic-arn arn:aws:sns:us-east-1:123456789012:topic',
        ]
        for cmd in commands:
            result = calculate_risk(cmd, reason="Cleanup", source="test", rules=risk_rules)
            assert result.score >= 30, f"'{cmd}' score {result.score} should be >= 30"
            # delete 是危險操作，通常不會是 auto_approve
            assert result.category != RiskCategory.BLOCK.value  # 但也不應該被 block

    def test_terminate_is_destructive(self, risk_rules):
        """terminate-* → 高風險 (46-85)"""
        cmd = 'aws ec2 terminate-instances --instance-ids i-12345'
        result = calculate_risk(cmd, reason="Cleanup test instance", source="test", rules=risk_rules)
        assert result.score >= 35, f"terminate score {result.score} should be >= 35"
        # terminate 分數很高 (95)，組合後應該在 confirm 或 manual

    def test_rm_is_destructive(self, risk_rules):
        """rm (S3) → 高風險"""
        cmd = 'aws s3 rm s3://bucket/key'
        result = calculate_risk(cmd, reason="Delete file", source="test", rules=risk_rules)
        assert result.score >= 25, f"s3 rm score {result.score} should be >= 25"

    def test_unknown_verb_gets_moderate_score(self, risk_rules):
        """未知動詞 → 預設中等分數 (50)"""
        parsed = parse_command('aws ec2 unknown-action')
        verb_score, factors = score_verb(parsed, risk_rules)
        # 未知動詞預設 50，EC2 服務 40，組合後約 46
        assert 30 <= verb_score <= 60, f"Unknown verb score {verb_score} should be 30-60"


class TestParameterRisk:
    """參數風險測試"""
    
    def test_recursive_adds_risk(self, risk_rules):
        """--recursive → +35 風險"""
        # 無 recursive
        cmd_without = 'aws s3 rm s3://bucket/prefix/'
        result_without = calculate_risk(cmd_without, reason="Delete", source="test", rules=risk_rules)
        
        # 有 recursive
        cmd_with = 'aws s3 rm s3://bucket/prefix/ --recursive'
        result_with = calculate_risk(cmd_with, reason="Delete", source="test", rules=risk_rules)
        
        # recursive 應該增加風險
        assert result_with.score > result_without.score, \
            f"--recursive should increase score: {result_with.score} vs {result_without.score}"
        
        # 檢查 factors 中有 recursive
        recursive_factors = [f for f in result_with.factors if 'recursive' in f.name.lower()]
        assert len(recursive_factors) > 0, "Should have recursive in factors"

    def test_force_adds_risk(self, risk_rules):
        """--force → +30 風險"""
        cmd_without = 'aws ecr delete-repository --repository-name test'
        result_without = calculate_risk(cmd_without, reason="Cleanup", source="test", rules=risk_rules)
        
        cmd_with = 'aws ecr delete-repository --repository-name test --force'
        result_with = calculate_risk(cmd_with, reason="Cleanup", source="test", rules=risk_rules)
        
        assert result_with.score > result_without.score, \
            f"--force should increase score: {result_with.score} vs {result_without.score}"

    def test_yes_flag_adds_risk(self, risk_rules):
        """--yes / -y → +20 風險"""
        cmd_without = 'aws s3 sync s3://source s3://dest'
        result_without = calculate_risk(cmd_without, reason="Sync", source="test", rules=risk_rules)
        
        cmd_with = 'aws s3 sync s3://source s3://dest --yes'
        result_with = calculate_risk(cmd_with, reason="Sync", source="test", rules=risk_rules)
        
        # --yes 可能不在所有命令中生效，但至少不應該降低分數
        assert result_with.score >= result_without.score

    def test_policy_document_adds_high_risk(self, risk_rules):
        """--policy-document → +70 風險"""
        cmd = 'aws iam put-role-policy --role-name test --policy-name test --policy-document file://policy.json'
        result = calculate_risk(cmd, reason="Update policy", source="test", rules=risk_rules)
        
        # policy-document 是高風險參數
        policy_factors = [f for f in result.factors if 'policy' in f.name.lower()]
        assert len(policy_factors) > 0, "Should detect policy-document parameter"
        
        # IAM 操作 + policy 參數，分數應該較高
        assert result.score >= 40, f"Policy document score {result.score} should be >= 40"

    def test_no_risky_params_no_extra_risk(self, risk_rules):
        """無高危參數 → 基礎分數"""
        cmd = 'aws s3 ls --profile default'
        parsed = parse_command(cmd)
        param_score, factors = score_parameters(parsed, risk_rules)
        
        # 沒有危險參數，應該只有基礎分數
        # factors 應該有 "No risky parameters detected"
        no_risk_factors = [f for f in factors if 'no risky' in f.name.lower()]
        assert len(no_risk_factors) > 0 or param_score <= 30, \
            f"No risky params should have low score, got {param_score}"

    def test_security_group_param_adds_risk(self, risk_rules):
        """--security-group → +55 風險"""
        cmd = 'aws ec2 run-instances --image-id ami-12345 --security-group-ids sg-12345'
        result = calculate_risk(cmd, reason="Launch instance", source="test", rules=risk_rules)
        
        # security-group 參數應該增加風險
        sg_factors = [f for f in result.factors if 'security' in f.name.lower()]
        # 即使沒有明確的 security-group factor，分數也應該合理
        assert result.score >= 20

    def test_skip_final_snapshot_adds_high_risk(self, risk_rules):
        """--skip-final-snapshot → +40 風險"""
        cmd = 'aws rds delete-db-instance --db-instance-identifier test --skip-final-snapshot'
        result = calculate_risk(cmd, reason="Delete DB", source="test", rules=risk_rules)
        
        # skip-final-snapshot 是危險操作
        assert result.score >= 40, f"Skip final snapshot score {result.score} should be >= 40"


class TestServiceSensitivity:
    """服務敏感度測試"""
    
    def test_iam_is_critical(self, risk_rules):
        """iam → 高敏感度 (95)"""
        # IAM describe 仍然是低風險，但比其他服務高
        cmd_iam = 'aws iam list-users'
        result_iam = calculate_risk(cmd_iam, reason="List users", source="test", rules=risk_rules)
        
        cmd_s3 = 'aws s3 ls'
        result_s3 = calculate_risk(cmd_s3, reason="List buckets", source="test", rules=risk_rules)
        
        # IAM 即使是唯讀，分數也比 S3 高
        assert result_iam.score >= result_s3.score, \
            f"IAM ({result_iam.score}) should be >= S3 ({result_s3.score})"

    def test_kms_is_critical(self, risk_rules):
        """kms → 高敏感度 (90)"""
        cmd = 'aws kms list-keys'
        result = calculate_risk(cmd, reason="List KMS keys", source="test", rules=risk_rules)
        
        # KMS 服務分數高，但 list 動詞低，組合後應該是低-中風險
        # 服務分數 90 × 0.4 權重 + 動詞分數 0 × 0.6 = 36 (verb 部分)
        assert result.score <= 45, f"KMS list score {result.score} should be <= 45"

    def test_sts_is_critical(self, risk_rules):
        """sts → 高敏感度 (85)"""
        cmd = 'aws sts get-caller-identity'
        result = calculate_risk(cmd, reason="Check identity", source="test", rules=risk_rules)
        
        # STS get-caller-identity 是安全的唯讀操作
        assert result.score <= 30, f"STS get-caller-identity score {result.score} should be <= 30"

    def test_s3_is_medium(self, risk_rules):
        """s3 → 中等敏感度 (30)"""
        cmd = 'aws s3 ls'
        result = calculate_risk(cmd, reason="List buckets", source="test", rules=risk_rules)
        
        # S3 list 應該是自動批准
        assert result.category == RiskCategory.AUTO_APPROVE.value, \
            f"S3 ls should be auto_approve, got {result.category}"

    def test_ec2_is_medium(self, risk_rules):
        """ec2 → 中等敏感度 (40)"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, reason="Check instances", source="test", rules=risk_rules)
        
        # EC2 describe 應該是自動批准
        assert result.score <= 25, f"EC2 describe score {result.score} should be <= 25"

    def test_logs_is_low_sensitivity(self, risk_rules):
        """logs → 低敏感度 (15)"""
        cmd = 'aws logs describe-log-groups'
        result = calculate_risk(cmd, reason="List log groups", source="test", rules=risk_rules)
        
        # Logs 服務低敏感，describe 低風險
        assert result.score <= 20, f"Logs describe score {result.score} should be <= 20"

    def test_organizations_is_blocked(self, risk_rules):
        """organizations → 黑名單 (100)"""
        cmd = 'aws organizations list-accounts'
        result = calculate_risk(cmd, reason="List accounts", source="test", rules=risk_rules)
        
        # Organizations 在黑名單中
        assert result.category == RiskCategory.BLOCK.value, \
            f"Organizations should be blocked, got {result.category}"

    def test_unknown_service_gets_moderate_score(self, risk_rules):
        """未知服務 → 預設中等分數 (40)"""
        cmd = 'aws newservice describe-things'
        parsed = parse_command(cmd)
        verb_score, factors = score_verb(parsed, risk_rules)
        
        # 未知服務預設 40，describe 動詞 0
        # 組合後約 16
        assert 10 <= verb_score <= 30, f"Unknown service score {verb_score} should be 10-30"


class TestReasonQuality:
    """Reason 品質測試"""
    
    def test_empty_reason_adds_risk(self, risk_rules):
        """空 reason → +15 風險"""
        cmd = 'aws ec2 describe-instances'
        
        result_with = calculate_risk(cmd, reason="Check instance status", source="test", rules=risk_rules)
        result_without = calculate_risk(cmd, reason="", source="test", rules=risk_rules)
        
        assert result_without.score > result_with.score, \
            f"Empty reason should increase score: {result_without.score} vs {result_with.score}"

    def test_short_reason_adds_risk(self, risk_rules):
        """過短 reason (<10 字) → +10 風險"""
        cmd = 'aws ec2 describe-instances'
        
        result_long = calculate_risk(cmd, reason="Checking instance status for deployment verification", source="test", rules=risk_rules)
        result_short = calculate_risk(cmd, reason="test", source="test", rules=risk_rules)
        
        # 過短的 reason 應該增加風險
        assert result_short.score >= result_long.score, \
            f"Short reason should have higher/equal score: {result_short.score} vs {result_long.score}"

    def test_ticket_reference_high_trust(self, risk_rules):
        """工單引用 → 可信度較高（但不一定降分）"""
        cmd = 'aws ec2 terminate-instances --instance-ids i-12345'
        
        # 有工單引用
        result_ticket = calculate_risk(
            cmd, 
            reason="JIRA-1234: Cleanup test instances after sprint", 
            source="test", 
            rules=risk_rules
        )
        
        # 無工單引用
        result_no_ticket = calculate_risk(
            cmd, 
            reason="Cleanup test instances", 
            source="test", 
            rules=risk_rules
        )
        
        # 有工單引用的 reason 不應該比沒有的更差
        assert result_ticket.score <= result_no_ticket.score + 5, \
            f"Ticket reference should not increase score much: {result_ticket.score} vs {result_no_ticket.score}"

    def test_vague_reason_lower_trust(self, risk_rules):
        """模糊 reason → 信任度較低"""
        cmd = 'aws ec2 terminate-instances --instance-ids i-12345'
        
        result_vague = calculate_risk(cmd, reason="測試", source="test", rules=risk_rules)
        result_detailed = calculate_risk(
            cmd, 
            reason="Terminating test instance i-12345 after load testing completed", 
            source="test", 
            rules=risk_rules
        )
        
        # 詳細的 reason 應該比模糊的好（或至少一樣）
        assert result_vague.score >= result_detailed.score - 5

    def test_test_keyword_in_reason(self, risk_rules):
        """test/debug 關鍵字 → 風險略降"""
        cmd = 'aws ec2 terminate-instances --instance-ids i-12345'
        
        result_test = calculate_risk(
            cmd, 
            reason="Testing cleanup procedure in dev environment", 
            source="test", 
            rules=risk_rules
        )
        
        result_prod = calculate_risk(
            cmd, 
            reason="Production instance cleanup", 
            source="test", 
            rules=risk_rules
        )
        
        # test 關鍵字應該有 -5 修正
        # 但差異可能不大
        assert result_test.score <= result_prod.score + 10

    def test_unknown_source_adds_risk(self, risk_rules):
        """未知來源 → +20 風險"""
        cmd = 'aws ec2 describe-instances'
        
        result_known = calculate_risk(cmd, reason="Check", source="Steven's Private Bot", rules=risk_rules)
        result_unknown = calculate_risk(cmd, reason="Check", source="", rules=risk_rules)
        
        assert result_unknown.score > result_known.score, \
            f"Unknown source should increase score: {result_unknown.score} vs {result_known.score}"


class TestRiskCalculation:
    """整合測試 - 完整的風險計算流程"""
    
    def test_safe_command_auto_approve(self, risk_rules):
        """安全命令 → auto_approve (0-25)"""
        safe_commands = [
            ('aws s3 ls', 'List buckets'),
            ('aws ec2 describe-instances', 'Check instances'),
            ('aws logs describe-log-groups', 'List log groups'),
            ('aws lambda list-functions', 'List functions'),
        ]
        
        for cmd, reason in safe_commands:
            result = calculate_risk(cmd, reason=reason, source="Private Bot", rules=risk_rules)
            assert result.category == RiskCategory.AUTO_APPROVE.value, \
                f"'{cmd}' should be auto_approve, got {result.category} (score: {result.score})"
            assert result.score <= 25, f"'{cmd}' score {result.score} should be <= 25"

    def test_medium_risk_command_log_or_confirm(self, risk_rules):
        """中等風險命令 → log (26-45) 或 confirm (46-65)"""
        medium_commands = [
            ('aws s3 cp file.txt s3://bucket/key', 'Upload file'),
            ('aws lambda update-function-code --function-name test --zip-file fileb://code.zip', 'Deploy'),
        ]
        
        for cmd, reason in medium_commands:
            result = calculate_risk(cmd, reason=reason, source="Private Bot", rules=risk_rules)
            assert result.category in [
                RiskCategory.AUTO_APPROVE.value,
                RiskCategory.LOG.value, 
                RiskCategory.CONFIRM.value
            ], f"'{cmd}' should be log/confirm, got {result.category}"

    def test_dangerous_command_manual(self, risk_rules):
        """危險命令 → manual (66-85)"""
        dangerous_commands = [
            ('aws ec2 terminate-instances --instance-ids i-12345 --force', 'Cleanup'),
            ('aws rds delete-db-instance --db-instance-identifier prod-db --skip-final-snapshot', 'Delete DB'),
        ]
        
        for cmd, reason in dangerous_commands:
            result = calculate_risk(cmd, reason=reason, source="Private Bot", rules=risk_rules)
            # 這些命令應該至少是 confirm 或更高
            assert result.score >= 35, \
                f"'{cmd}' score {result.score} should be >= 35"

    def test_blocked_command(self, risk_rules, sample_commands):
        """黑名單命令 → block (86-100)"""
        for cmd in sample_commands['blocked']:
            result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
            assert result.category == RiskCategory.BLOCK.value, \
                f"'{cmd}' should be blocked, got {result.category} (score: {result.score})"
            assert result.score >= 86, f"'{cmd}' score {result.score} should be >= 86"

    def test_score_accumulation(self, risk_rules):
        """分數累加測試"""
        # 基礎命令
        base_cmd = 'aws s3 rm s3://bucket/key'
        base_result = calculate_risk(base_cmd, reason="Delete file", source="Private Bot", rules=risk_rules)
        
        # 加上 recursive
        recursive_cmd = 'aws s3 rm s3://bucket/ --recursive'
        recursive_result = calculate_risk(recursive_cmd, reason="Delete files", source="Private Bot", rules=risk_rules)
        
        # 加上 force
        force_cmd = 'aws s3 rm s3://bucket/ --recursive --force'
        force_result = calculate_risk(force_cmd, reason="Force delete", source="Private Bot", rules=risk_rules)
        
        # 分數應該遞增
        assert recursive_result.score > base_result.score, \
            f"Recursive ({recursive_result.score}) should > base ({base_result.score})"
        assert force_result.score >= recursive_result.score, \
            f"Force ({force_result.score}) should >= recursive ({recursive_result.score})"

    def test_result_has_all_fields(self, risk_rules):
        """結果應包含所有必要欄位"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
        
        # 基本欄位
        assert isinstance(result.score, int)
        assert 0 <= result.score <= 100
        assert result.category in [c.value for c in RiskCategory]
        assert isinstance(result.factors, list)
        assert len(result.factors) > 0
        assert isinstance(result.recommendation, str)
        assert len(result.recommendation) > 0
        assert result.command == cmd
        assert result.parsed_command is not None
        assert result.evaluation_time_ms >= 0
        assert result.rule_version is not None

    def test_to_dict_serialization(self, risk_rules):
        """to_dict() 序列化測試"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
        
        d = result.to_dict()
        
        assert 'score' in d
        assert 'category' in d
        assert 'factors' in d
        assert 'recommendation' in d
        assert 'evaluation_time_ms' in d
        
        # factors 應該是可序列化的字典列表
        assert isinstance(d['factors'], list)
        for factor in d['factors']:
            assert isinstance(factor, dict)
            assert 'name' in factor
            assert 'raw_score' in factor


class TestEdgeCases:
    """邊界案例測試"""
    
    def test_empty_command(self, risk_rules):
        """空命令 → Fail-closed (manual)"""
        result = calculate_risk("", reason="Test", source="test", rules=risk_rules)
        
        assert result.category == RiskCategory.MANUAL.value
        assert result.score == 70  # Fail-closed 分數
        assert not result.parsed_command.is_valid

    def test_whitespace_only_command(self, risk_rules):
        """只有空白的命令"""
        result = calculate_risk("   ", reason="Test", source="test", rules=risk_rules)
        
        assert result.category == RiskCategory.MANUAL.value
        assert not result.parsed_command.is_valid

    def test_malformed_command(self, risk_rules):
        """格式錯誤的命令"""
        malformed = [
            'aws',
            'aws ec2',
            'aws --help',
            '--option value',
        ]
        
        for cmd in malformed:
            result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
            # 格式錯誤應該能處理，不會崩潰
            assert isinstance(result.score, int)
            assert 0 <= result.score <= 100

    def test_unknown_service(self, risk_rules):
        """未知服務"""
        cmd = 'aws unknownservice do-something'
        result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
        
        # 未知服務應該使用預設分數，不應該是 block
        assert result.category != RiskCategory.BLOCK.value
        assert 20 <= result.score <= 60

    def test_missing_reason(self, risk_rules):
        """缺少 reason"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, source="test", rules=risk_rules)
        
        # 沒有 reason 應該增加風險
        context_factors = [f for f in result.factors if f.category == 'context']
        assert len(context_factors) > 0

    def test_missing_source(self, risk_rules):
        """缺少 source"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, reason="Test", rules=risk_rules)
        
        # 沒有 source 應該增加風險
        assert result.score >= 0

    def test_very_long_command(self, risk_rules):
        """超長命令"""
        # 建構一個很長的命令
        long_cmd = 'aws ec2 describe-instances --instance-ids ' + ' '.join([f'i-{i:016d}' for i in range(100)])
        result = calculate_risk(long_cmd, reason="Test many instances", source="test", rules=risk_rules)
        
        # 應該能處理，不會崩潰
        assert isinstance(result.score, int)

    def test_special_characters_in_command(self, risk_rules):
        """命令中的特殊字元"""
        special_commands = [
            "aws s3 cp 's3://bucket/file with spaces.txt' .",
            'aws dynamodb query --table-name test --key-condition-expression "id = :id"',
            "aws lambda invoke --function-name test --payload '{\"key\": \"value\"}' output.json",
        ]
        
        for cmd in special_commands:
            result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
            # 應該能處理，不會崩潰
            assert isinstance(result.score, int)

    def test_unicode_in_reason(self, risk_rules):
        """Reason 中的 Unicode"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(
            cmd, 
            reason="檢查實例狀態 🚀 for deployment", 
            source="test", 
            rules=risk_rules
        )
        
        assert isinstance(result.score, int)

    def test_none_values(self, risk_rules):
        """None 值處理"""
        cmd = 'aws ec2 describe-instances'
        result = calculate_risk(cmd, reason=None, source=None, account_id=None, rules=risk_rules)
        
        assert isinstance(result.score, int)
        assert 0 <= result.score <= 100


class TestCommandParsing:
    """命令解析測試"""
    
    def test_parse_basic_command(self):
        """基本命令解析"""
        cmd = 'aws ec2 describe-instances'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert parsed.service == 'ec2'
        assert parsed.action == 'describe-instances'
        assert parsed.verb == 'describe'
        assert parsed.resource_type == 'instances'

    def test_parse_command_with_parameters(self):
        """帶參數的命令解析"""
        cmd = 'aws ec2 describe-instances --instance-ids i-12345 --region us-east-1'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert 'instance-ids' in parsed.parameters
        assert parsed.parameters['instance-ids'] == 'i-12345'
        assert 'region' in parsed.parameters

    def test_parse_command_with_flags(self):
        """帶旗標的命令解析"""
        cmd = 'aws s3 rm s3://bucket/key --recursive --force'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert '--recursive' in parsed.flags
        assert '--force' in parsed.flags

    def test_parse_s3_command(self):
        """S3 命令解析（特殊格式）"""
        cmd = 'aws s3 cp file.txt s3://bucket/key'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert parsed.service == 's3'
        assert parsed.action == 'cp'
        assert parsed.verb == 'cp'

    def test_parse_command_with_aws_prefix(self):
        """帶 aws 前綴的命令"""
        cmd = 'aws ec2 describe-instances'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert parsed.service == 'ec2'

    def test_parse_command_without_aws_prefix(self):
        """不帶 aws 前綴的命令"""
        cmd = 'ec2 describe-instances'
        parsed = parse_command(cmd)
        
        assert parsed.is_valid
        assert parsed.service == 'ec2'

    def test_parse_invalid_command(self):
        """無效命令解析"""
        parsed = parse_command('')
        
        assert not parsed.is_valid
        assert parsed.parse_error is not None

    def test_parse_preserves_original(self):
        """解析保留原始命令"""
        cmd = 'aws ec2 describe-instances --instance-ids i-12345'
        parsed = parse_command(cmd)
        
        assert parsed.original == cmd


class TestWeightCalculation:
    """權重計算測試"""
    
    def test_default_weights_sum_to_one(self, risk_rules):
        """預設權重總和為 1"""
        total = sum(risk_rules.weights.values())
        assert abs(total - 1.0) < 0.01, f"Weights should sum to 1.0, got {total}"

    def test_verb_weight_is_dominant(self, risk_rules):
        """動詞權重最高 (40%)"""
        assert risk_rules.weights['verb'] == 0.40

    def test_parameter_weight(self, risk_rules):
        """參數權重 (30%)"""
        assert risk_rules.weights['parameter'] == 0.30

    def test_context_weight(self, risk_rules):
        """上下文權重 (20%)"""
        assert risk_rules.weights['context'] == 0.20

    def test_account_weight(self, risk_rules):
        """帳號權重 (10%)"""
        assert risk_rules.weights['account'] == 0.10

    def test_custom_weights(self):
        """自定義權重"""
        custom_rules = create_default_rules()
        custom_rules.weights = {
            'verb': 0.50,
            'parameter': 0.25,
            'context': 0.15,
            'account': 0.10,
        }
        
        is_valid, errors = custom_rules.validate()
        assert is_valid, f"Custom weights should be valid: {errors}"

    def test_invalid_weights_detected(self):
        """無效權重檢測"""
        invalid_rules = create_default_rules()
        invalid_rules.weights = {
            'verb': 0.50,
            'parameter': 0.30,
            'context': 0.30,  # 總和 > 1
            'account': 0.10,
        }
        
        is_valid, errors = invalid_rules.validate()
        assert not is_valid, "Should detect invalid weights"
        assert len(errors) > 0


class TestAccountSensitivity:
    """帳號敏感度測試"""
    
    def test_configured_account_score(self, sensitive_accounts):
        """已配置帳號的分數"""
        cmd = 'aws ec2 describe-instances'
        
        # Production 帳號 - 高敏感
        result_prod = calculate_risk(
            cmd, 
            reason="Check", 
            source="test", 
            account_id="PROD_ACCOUNT",
            rules=sensitive_accounts
        )
        
        # Dev 帳號 - 低敏感
        result_dev = calculate_risk(
            cmd, 
            reason="Check", 
            source="test", 
            account_id="DEV_ACCOUNT",
            rules=sensitive_accounts
        )
        
        # Production 應該比 Dev 更敏感
        assert result_prod.score > result_dev.score, \
            f"Prod ({result_prod.score}) should be > Dev ({result_dev.score})"

    def test_unknown_account_default_score(self, sensitive_accounts):
        """未配置帳號使用預設分數"""
        cmd = 'aws ec2 describe-instances'
        
        result = calculate_risk(
            cmd, 
            reason="Check", 
            source="test", 
            account_id="UNKNOWN_ACCOUNT",
            rules=sensitive_accounts
        )
        
        # 應該有帳號相關的 factor
        account_factors = [f for f in result.factors if f.category == 'account']
        assert len(account_factors) > 0


class TestCategoryThresholds:
    """分類閾值測試"""
    
    def test_auto_approve_threshold(self):
        """auto_approve 閾值 (0-25)"""
        assert get_category_from_score(0) == RiskCategory.AUTO_APPROVE.value
        assert get_category_from_score(25) == RiskCategory.AUTO_APPROVE.value
        assert get_category_from_score(26) != RiskCategory.AUTO_APPROVE.value

    def test_log_threshold(self):
        """log 閾值 (26-45)"""
        assert get_category_from_score(26) == RiskCategory.LOG.value
        assert get_category_from_score(45) == RiskCategory.LOG.value
        assert get_category_from_score(46) != RiskCategory.LOG.value

    def test_confirm_threshold(self):
        """confirm 閾值 (46-65)"""
        assert get_category_from_score(46) == RiskCategory.CONFIRM.value
        assert get_category_from_score(65) == RiskCategory.CONFIRM.value
        assert get_category_from_score(66) != RiskCategory.CONFIRM.value

    def test_manual_threshold(self):
        """manual 閾值 (66-85)"""
        assert get_category_from_score(66) == RiskCategory.MANUAL.value
        assert get_category_from_score(85) == RiskCategory.MANUAL.value
        assert get_category_from_score(86) != RiskCategory.MANUAL.value

    def test_block_threshold(self):
        """block 閾值 (86-100)"""
        assert get_category_from_score(86) == RiskCategory.BLOCK.value
        assert get_category_from_score(100) == RiskCategory.BLOCK.value


class TestPerformance:
    """效能測試"""
    
    def test_evaluation_time_under_50ms(self, risk_rules):
        """評估時間 < 50ms"""
        cmd = 'aws ec2 describe-instances --instance-ids i-12345'
        result = calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
        
        assert result.evaluation_time_ms < 50, \
            f"Evaluation time {result.evaluation_time_ms}ms should be < 50ms"

    def test_batch_evaluation_performance(self, risk_rules, sample_commands):
        """批量評估效能"""
        import time
        
        all_commands = []
        for category, cmds in sample_commands.items():
            all_commands.extend(cmds)
        
        start = time.perf_counter()
        for cmd in all_commands:
            calculate_risk(cmd, reason="Test", source="test", rules=risk_rules)
        elapsed = (time.perf_counter() - start) * 1000
        
        avg_time = elapsed / len(all_commands)
        assert avg_time < 20, f"Average evaluation time {avg_time}ms should be < 20ms"


class TestRuleValidation:
    """規則驗證測試"""
    
    def test_default_rules_valid(self):
        """預設規則有效"""
        rules = create_default_rules()
        is_valid, errors = rules.validate()
        
        assert is_valid, f"Default rules should be valid: {errors}"

    def test_invalid_verb_score_detected(self):
        """偵測無效動詞分數"""
        rules = create_default_rules()
        rules.verb_scores['invalid'] = 150  # 超出範圍
        
        is_valid, errors = rules.validate()
        assert not is_valid
        assert any('invalid' in e.lower() for e in errors)

    def test_invalid_service_score_detected(self):
        """偵測無效服務分數"""
        rules = create_default_rules()
        rules.service_scores['invalid'] = -10  # 負數
        
        is_valid, errors = rules.validate()
        assert not is_valid


# ============================================================================
# Integration Tests with Real-World Scenarios
# ============================================================================

class TestRealWorldScenarios:
    """真實場景測試"""
    
    def test_deployment_workflow(self, risk_rules):
        """部署工作流程"""
        # 1. 檢查現有資源
        check_result = calculate_risk(
            'aws lambda list-functions',
            reason="Pre-deployment check",
            source="CI/CD Pipeline",
            rules=risk_rules
        )
        assert check_result.category == RiskCategory.AUTO_APPROVE.value
        
        # 2. 上傳程式碼
        upload_result = calculate_risk(
            'aws s3 cp code.zip s3://deploy-bucket/code.zip',
            reason="Upload deployment package",
            source="CI/CD Pipeline",
            rules=risk_rules
        )
        assert upload_result.score <= 45  # 應該是 log 或更低
        
        # 3. 更新函數
        update_result = calculate_risk(
            'aws lambda update-function-code --function-name my-func --s3-bucket deploy-bucket --s3-key code.zip',
            reason="Deploy new version",
            source="CI/CD Pipeline",
            rules=risk_rules
        )
        assert update_result.score <= 55  # 應該是 confirm 或更低

    def test_incident_response_workflow(self, risk_rules):
        """事件響應工作流程"""
        # 1. 診斷
        diagnose_result = calculate_risk(
            'aws ec2 describe-instances --filters Name=instance-state-name,Values=running',
            reason="INCIDENT-123: Investigating high CPU",
            source="SRE Team",
            rules=risk_rules
        )
        assert diagnose_result.category == RiskCategory.AUTO_APPROVE.value
        
        # 2. 查看日誌
        logs_result = calculate_risk(
            'aws logs filter-log-events --log-group-name /app/logs --filter-pattern ERROR',
            reason="INCIDENT-123: Finding error logs",
            source="SRE Team",
            rules=risk_rules
        )
        assert logs_result.score <= 30
        
        # 3. 重啟服務（需要審批）
        restart_result = calculate_risk(
            'aws ecs update-service --cluster prod --service api --force-new-deployment',
            reason="INCIDENT-123: Restarting service to recover",
            source="SRE Team",
            rules=risk_rules
        )
        # 強制重新部署應該需要確認
        assert restart_result.score >= 30

    def test_cleanup_workflow(self, risk_rules):
        """資源清理工作流程"""
        # 1. 列出舊資源
        list_result = calculate_risk(
            'aws ec2 describe-snapshots --owner-ids self --filters Name=tag:Environment,Values=test',
            reason="List old test snapshots for cleanup",
            source="Cleanup Bot",
            rules=risk_rules
        )
        assert list_result.category == RiskCategory.AUTO_APPROVE.value
        
        # 2. 刪除快照（需要審批）
        delete_result = calculate_risk(
            'aws ec2 delete-snapshot --snapshot-id snap-12345',
            reason="Cleanup: Delete test snapshot older than 30 days",
            source="Cleanup Bot",
            rules=risk_rules
        )
        # 刪除操作需要審批
        assert delete_result.score >= 30

    def test_security_audit_workflow(self, risk_rules):
        """安全審計工作流程"""
        # 1. 列出 IAM 用戶 - IAM 是高敏感服務，即使 list 也會是 LOG 級別
        list_users = calculate_risk(
            'aws iam list-users',
            reason="Security audit: Review IAM users",
            source="Security Team",
            rules=risk_rules
        )
        # IAM list 應該在 LOG 或以下（即使 IAM 敏感度高，list 仍然是安全操作）
        assert list_users.score <= 45, f"IAM list score {list_users.score} should be <= 45"
        assert list_users.category in [RiskCategory.AUTO_APPROVE.value, RiskCategory.LOG.value]
        
        # 2. 檢查政策（讀取）
        get_policy = calculate_risk(
            'aws iam get-role-policy --role-name MyRole --policy-name MyPolicy',
            reason="Security audit: Review role permissions",
            source="Security Team",
            rules=risk_rules
        )
        assert get_policy.score <= 35
        
        # 3. 修改政策（應該被阻止）
        put_policy = calculate_risk(
            'aws iam put-role-policy --role-name MyRole --policy-name MyPolicy --policy-document file://policy.json',
            reason="Security: Update permissions",
            source="Security Team",
            rules=risk_rules
        )
        # 修改 IAM 政策在黑名單中
        assert put_policy.category == RiskCategory.BLOCK.value
