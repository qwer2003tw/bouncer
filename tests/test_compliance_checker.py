"""
Tests for compliance_checker.py
合規檢查模組測試
"""
import os
import importlib
import pytest

# Set TRUSTED_ACCOUNT_IDS before importing compliance_checker so the regex
# for P-S3 (external account detection) is built correctly.
if not os.environ.get('TRUSTED_ACCOUNT_IDS'):
    os.environ['TRUSTED_ACCOUNT_IDS'] = '111111111111,222222222222,333333333333'

import src.compliance_checker as _cc_mod
# Reload constants first (TRUSTED_ACCOUNT_IDS loaded from env at import time),
# then reload compliance_checker to pick up the new TRUSTED_ACCOUNT_IDS
import src.constants as _const_mod
importlib.reload(_const_mod)
importlib.reload(_cc_mod)

from src.compliance_checker import (
    check_compliance,
    format_violation_message,
    get_all_rules,
    ComplianceViolation,
)


class TestLambdaRules:
    """Lambda 安全規則測試 (L1-L2)"""

    def test_lambda_principal_star_blocked(self):
        """L1: Lambda Principal:* 禁止"""
        cmd = "aws lambda add-permission --function-name test --principal '*' --statement-id s1 --action lambda:InvokeFunction"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "L1"
        assert "Principal" in violation.rule_name

    def test_lambda_principal_star_without_quotes(self):
        """L1: Principal * 不帶引號"""
        cmd = "aws lambda add-permission --function-name test --principal * --statement-id s1"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "L1"

    def test_lambda_principal_specific_account_ok(self):
        """L1: 指定具體帳號應該通過"""
        cmd = "aws lambda add-permission --function-name test --principal 123456789012 --statement-id s1"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant
        assert violation is None

    def test_lambda_url_auth_none_blocked(self):
        """L2: Lambda URL AuthType NONE 禁止"""
        cmd = "aws lambda create-function-url-config --function-name test --auth-type NONE"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "L2"

    def test_lambda_url_update_auth_none_blocked(self):
        """L2: 更新 Lambda URL 也禁止 NONE"""
        cmd = "aws lambda update-function-url-config --function-name test --auth-type NONE"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "L2"

    def test_lambda_url_auth_iam_ok(self):
        """L2: AWS_IAM 認證應該通過"""
        cmd = "aws lambda create-function-url-config --function-name test --auth-type AWS_IAM"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant


class TestS3Rules:
    """S3 公開存取規則測試 (P-S2)"""

    def test_s3_public_read_acl_blocked(self):
        """P-S2: S3 public-read ACL 禁止"""
        cmd = "aws s3api put-bucket-acl --bucket test --acl public-read"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"

    def test_s3_public_read_write_acl_blocked(self):
        """P-S2: S3 public-read-write ACL 禁止"""
        cmd = "aws s3 cp test.txt s3://bucket/ --acl public-read-write"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"

    def test_s3_private_acl_ok(self):
        """P-S2: private ACL 應該通過"""
        cmd = "aws s3api put-bucket-acl --bucket test --acl private"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_s3_block_public_access_false_blocked(self):
        """P-S2: Block Public Access false 禁止"""
        cmd = 'aws s3api put-public-access-block --bucket test --public-access-block-configuration \'{"BlockPublicAcls": false}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"


class TestSnapshotRules:
    """快照公開存取規則測試 (P-S2)"""

    def test_ebs_snapshot_public_blocked(self):
        """P-S2: EBS Snapshot 公開禁止"""
        cmd = "aws ec2 modify-snapshot-attribute --snapshot-id snap-123 --attribute createVolumePermission --group-names all"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"
        assert "EBS Snapshot" in violation.rule_name

    def test_ami_public_blocked(self):
        """P-S2: AMI 公開禁止"""
        cmd = 'aws ec2 modify-image-attribute --image-id ami-123 --launch-permission \'{"Add": [{"Group": "all"}]}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "AMI" in violation.rule_name

    def test_rds_snapshot_public_blocked(self):
        """P-S2: RDS Snapshot 公開禁止"""
        cmd = "aws rds modify-db-snapshot-attribute --db-snapshot-identifier snap-123 --attribute-name restore --values-to-add all"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "RDS Snapshot" in violation.rule_name

    def test_rds_cluster_snapshot_public_blocked(self):
        """P-S2: RDS Cluster Snapshot 公開禁止"""
        cmd = "aws rds modify-db-cluster-snapshot-attribute --db-cluster-snapshot-identifier snap-123 --attribute-name restore --values-to-add all"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant


class TestIAMKMSRules:
    """IAM/KMS 安全規則測試 (P-S2, P-S3)"""

    def test_iam_trust_policy_star_blocked(self):
        """P-S2: IAM Role Trust Policy Principal:* 禁止"""
        cmd = 'aws iam update-assume-role-policy --role-name test --policy-document \'{"Principal": "*"}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S2"

    def test_iam_create_role_star_blocked(self):
        """P-S2: 建立 IAM Role 時 Principal:* 禁止"""
        cmd = 'aws iam create-role --role-name test --assume-role-policy-document \'{"Principal": "*"}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant

    def test_kms_key_policy_star_blocked(self):
        """P-S2: KMS Key Policy Principal:* 禁止"""
        cmd = 'aws kms put-key-policy --key-id 123 --policy-name default --policy \'{"Principal": "*"}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "KMS" in violation.rule_name

    def test_iam_external_account_blocked(self):
        """P-S3: IAM Role 信任外部帳號禁止"""
        cmd = 'aws iam update-assume-role-policy --role-name test --policy-document \'{"Principal": {"AWS": "arn:aws:iam::999999999999:root"}}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "P-S3"

    def test_iam_internal_account_ok(self):
        """P-S3: 組織內帳號應該通過"""
        cmd = 'aws iam update-assume-role-policy --role-name test --policy-document \'{"Principal": {"AWS": "arn:aws:iam::111111111111:root"}}\''
        is_compliant, violation = check_compliance(cmd)
        # 這個不會被 P-S3 攔截（是內部帳號），但可能被其他規則攔截
        # 主要測試 P-S3 的外部帳號檢測
        # 注意：這個命令沒有 Principal: *，所以應該通過 P-S2
        assert violation is None or violation.rule_id != "P-S3"


class TestSNSSQSRules:
    """SNS/SQS 公開存取規則測試 (P-S2)"""

    def test_sns_public_permission_blocked(self):
        """P-S2: SNS 公開存取禁止"""
        cmd = "aws sns add-permission --topic-arn arn:aws:sns:us-east-1:123:test --label pub --aws-account-id '*' --action-name Publish"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "SNS" in violation.rule_name

    def test_sqs_public_permission_blocked(self):
        """P-S2: SQS 公開存取禁止"""
        cmd = "aws sqs add-permission --queue-url https://sqs.us-east-1.amazonaws.com/123/test --label pub --aws-account-ids '*' --actions SendMessage"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "SQS" in violation.rule_name

    def test_sqs_policy_star_blocked(self):
        """P-S2: SQS Policy Principal:* 禁止"""
        cmd = 'aws sqs set-queue-attributes --queue-url https://sqs.us-east-1.amazonaws.com/123/test --attributes \'{"Policy": {"Principal": "*"}}\''
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant


class TestHardcodedCredentials:
    """硬編碼憑證檢測測試 (CS)"""

    def test_access_key_in_command_blocked(self):
        """CS-HC001: Access Key 硬編碼禁止"""
        cmd = "aws s3 ls --access-key-id AKIAIOSFODNN7EXAMPLE"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "CS-HC001"

    def test_secret_key_in_command_blocked(self):
        """CS-HC002: Secret Key 硬編碼禁止"""
        cmd = "aws configure set aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "CS-HC002"

    def test_private_key_in_command_blocked(self):
        """CS-HC003: 私鑰硬編碼禁止"""
        cmd = "echo '-----BEGIN RSA PRIVATE KEY-----' | aws secretsmanager put-secret-value"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "CS-HC003"


class TestSecurityGroupRules:
    """Security Group 規則測試 (P-S2)"""

    def test_sg_all_traffic_public_blocked(self):
        """P-S2: SG 全開禁止"""
        cmd = "aws ec2 authorize-security-group-ingress --group-id sg-123 --cidr 0.0.0.0/0 --protocol -1"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "Security Group" in violation.rule_name

    def test_sg_ssh_public_blocked(self):
        """P-S2: SSH 公開禁止"""
        cmd = "aws ec2 authorize-security-group-ingress --group-id sg-123 --cidr 0.0.0.0/0 --protocol tcp --port 22"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert "敏感端口" in violation.rule_name

    def test_sg_rdp_public_blocked(self):
        """P-S2: RDP 公開禁止"""
        cmd = "aws ec2 authorize-security-group-ingress --group-id sg-123 --cidr 0.0.0.0/0 --protocol tcp --port 3389"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant

    def test_sg_mysql_public_blocked(self):
        """P-S2: MySQL 端口公開禁止"""
        cmd = "aws ec2 authorize-security-group-ingress --group-id sg-123 --cidr 0.0.0.0/0 --protocol tcp --port 3306"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant

    def test_sg_specific_cidr_ok(self):
        """P-S2: 指定 CIDR 應該通過"""
        cmd = "aws ec2 authorize-security-group-ingress --group-id sg-123 --cidr 10.0.0.0/8 --protocol tcp --port 22"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant


class TestEC2InstanceAttribute:
    """EC2 modify-instance-attribute 細粒度控制測試 (B-EC2)"""

    def test_user_data_blocked(self):
        """B-EC2-01: 禁止修改 User Data"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --user-data file://script.sh"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-EC2-01"

    def test_iam_instance_profile_blocked(self):
        """B-EC2-02: 禁止直接修改 Instance Profile"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --iam-instance-profile Name=AdminRole"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-EC2-02"

    def test_source_dest_check_false_blocked(self):
        """B-EC2-03: 禁止關閉 Source/Dest Check"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --source-dest-check false"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-EC2-03"

    def test_kernel_blocked(self):
        """B-EC2-04: 禁止修改 Kernel"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --kernel aki-12345"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-EC2-04"

    def test_ramdisk_blocked(self):
        """B-EC2-05: 禁止修改 Ramdisk"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --ramdisk ari-12345"
        is_compliant, violation = check_compliance(cmd)
        assert not is_compliant
        assert violation.rule_id == "B-EC2-05"

    def test_instance_type_ok(self):
        """允許修改 Instance Type"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --instance-type m8i.xlarge"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_cpu_options_ok(self):
        """允許修改 CPU Options (nested virtualization)"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --cpu-options AmdSevSnp=enabled"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_disable_api_termination_ok(self):
        """允許修改 Disable API Termination"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --disable-api-termination"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_ebs_optimized_ok(self):
        """允許修改 EBS Optimized"""
        cmd = "aws ec2 modify-instance-attribute --instance-id i-123 --ebs-optimized"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant


class TestSafeCommands:
    """安全命令測試 - 應該通過"""

    def test_s3_ls_ok(self):
        """s3 ls 應該通過"""
        cmd = "aws s3 ls s3://my-bucket/"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_ec2_describe_ok(self):
        """ec2 describe 應該通過"""
        cmd = "aws ec2 describe-instances"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_lambda_list_ok(self):
        """lambda list 應該通過"""
        cmd = "aws lambda list-functions"
        is_compliant, violation = check_compliance(cmd)
        assert is_compliant

    def test_empty_command_ok(self):
        """空命令應該通過"""
        is_compliant, violation = check_compliance("")
        assert is_compliant
        assert violation is None

    def test_none_command_ok(self):
        """None 命令應該通過"""
        is_compliant, violation = check_compliance(None)
        assert is_compliant


class TestFormatViolationMessage:
    """違規訊息格式化測試"""

    def test_format_message_structure(self):
        """測試訊息結構"""
        violation = ComplianceViolation(
            rule_id="L1",
            rule_name="Lambda Principal:* 禁止",
            description="Lambda 資源政策不可使用 Principal: *",
            remediation="指定具體的 AWS 帳號或服務",
        )
        msg = format_violation_message(violation)
        assert "🚫" in msg
        assert "L1" in msg
        assert "Lambda" in msg
        assert "修正建議" in msg

    def test_format_escapes_markdown(self):
        """測試 Markdown 轉義"""
        violation = ComplianceViolation(
            rule_id="TEST",
            rule_name="Test_Rule",
            description="This has * and _ special chars",
            remediation="Use [brackets] and (parens)",
        )
        msg = format_violation_message(violation)
        # 應該轉義特殊字元
        assert "\\*" in msg or "*" not in msg.replace("*合規違規*", "")


class TestGetAllRules:
    """取得所有規則測試"""

    def test_get_all_rules_not_empty(self):
        """應該有規則"""
        rules = get_all_rules()
        assert len(rules) > 0

    def test_get_all_rules_structure(self):
        """規則結構正確"""
        rules = get_all_rules()
        for rule in rules:
            assert 'rule_id' in rule
            assert 'rule_name' in rule
            assert 'description' in rule
            assert 'remediation' in rule

    def test_get_all_rules_has_lambda_rules(self):
        """包含 Lambda 規則"""
        rules = get_all_rules()
        lambda_rules = [r for r in rules if r['rule_id'].startswith('L')]
        assert len(lambda_rules) >= 2

    def test_get_all_rules_has_palisade_rules(self):
        """包含 Palisade 規則"""
        rules = get_all_rules()
        palisade_rules = [r for r in rules if r['rule_id'].startswith('P-')]
        assert len(palisade_rules) >= 5

    def test_get_all_rules_has_code_scanning_rules(self):
        """包含 Code Scanning 規則"""
        rules = get_all_rules()
        cs_rules = [r for r in rules if r['rule_id'].startswith('CS-')]
        assert len(cs_rules) >= 3
