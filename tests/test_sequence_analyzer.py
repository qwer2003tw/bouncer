"""
Tests for sequence_analyzer module
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestExtractResourceIds:
    """extract_resource_ids 測試"""

    def test_ec2_instance_id(self):
        """提取 EC2 instance ID"""
        from sequence_analyzer import extract_resource_ids
        ids = extract_resource_ids('aws ec2 terminate-instances --instance-ids i-1234567890abcdef0')
        assert 'i-1234567890abcdef0' in ids

    def test_multiple_instance_ids(self):
        """提取多個 instance ID"""
        from sequence_analyzer import extract_resource_ids
        ids = extract_resource_ids('aws ec2 describe-instances --instance-ids i-abc123 i-def456')
        assert 'i-abc123' in ids
        assert 'i-def456' in ids

    def test_s3_bucket_name(self):
        """提取 S3 bucket 名稱"""
        from sequence_analyzer import extract_resource_ids
        ids = extract_resource_ids('aws s3 rm s3://my-bucket/key --recursive')
        assert 'my-bucket' in ids

    def test_lambda_function_name(self):
        """提取 Lambda function 名稱"""
        from sequence_analyzer import extract_resource_ids
        ids = extract_resource_ids('aws lambda delete-function --function-name my-function')
        assert 'my-function' in ids

    def test_no_resource_ids(self):
        """沒有資源 ID"""
        from sequence_analyzer import extract_resource_ids
        ids = extract_resource_ids('aws sts get-caller-identity')
        assert ids == []


class TestParseActionFromCommand:
    """parse_action_from_command 測試"""

    def test_basic_command(self):
        """基本命令解析"""
        from sequence_analyzer import parse_action_from_command
        service, action = parse_action_from_command('aws ec2 terminate-instances --instance-ids i-123')
        assert service == 'ec2'
        assert action == 'terminate-instances'

    def test_s3_command(self):
        """S3 命令解析"""
        from sequence_analyzer import parse_action_from_command
        service, action = parse_action_from_command('aws s3 rm s3://bucket/key')
        assert service == 's3'
        assert action == 'rm'

    def test_service_alias(self):
        """服務別名解析（s3api → s3）"""
        from sequence_analyzer import parse_action_from_command
        service, action = parse_action_from_command('aws s3api delete-bucket --bucket my-bucket')
        assert service == 's3'
        assert action == 'delete-bucket'

    def test_short_command(self):
        """太短的命令"""
        from sequence_analyzer import parse_action_from_command
        service, action = parse_action_from_command('aws')
        assert service == ''
        assert action == ''

    def test_empty_command(self):
        """空命令"""
        from sequence_analyzer import parse_action_from_command
        service, action = parse_action_from_command('')
        assert service == ''
        assert action == ''


class TestAnalyzeSequence:
    """analyze_sequence 測試（DynamoDB 操作 mock）"""

    @patch('sequence_analyzer.get_recent_commands')
    def test_non_dangerous_action(self, mock_history):
        """非危險操作不需前置查詢"""
        from sequence_analyzer import analyze_sequence
        mock_history.return_value = []
        result = analyze_sequence('test-source', 'aws ec2 describe-instances')
        assert result.has_prior_query is True  # 非危險操作視為已有
        assert result.risk_modifier == 0.0

    @patch('sequence_analyzer.get_recent_commands')
    def test_dangerous_no_history(self, mock_history):
        """危險操作 + 無歷史 → 高風險"""
        from sequence_analyzer import analyze_sequence
        mock_history.return_value = []
        result = analyze_sequence('test-source', 'aws ec2 terminate-instances --instance-ids i-123')
        assert result.has_prior_query is False
        assert result.risk_modifier > 0  # 正值表示增加風險

    @patch('sequence_analyzer.get_recent_commands')
    def test_dangerous_with_prior_query(self, mock_history):
        """危險操作 + 有相關前置查詢 → 降低風險"""
        from sequence_analyzer import analyze_sequence, CommandRecord
        mock_history.return_value = [
            CommandRecord(
                source='test-source',
                timestamp='2026-01-01T00:00:00Z',
                command='aws ec2 describe-instances --instance-ids i-123',
                service='ec2',
                action='describe-instances',
                resource_ids=['i-123'],
                account_id='111111111111',
            )
        ]
        result = analyze_sequence('test-source', 'aws ec2 terminate-instances --instance-ids i-123')
        assert result.has_prior_query is True
        assert result.risk_modifier < 0  # 負值表示降低風險
        assert result.resource_match is True
        assert 'i-123' in result.matched_resources


class TestCommandRecord:
    """CommandRecord 資料類別測試"""

    def test_to_dict_and_from_dict(self):
        """to_dict 和 from_dict 往返"""
        from sequence_analyzer import CommandRecord
        record = CommandRecord(
            source='test',
            timestamp='2026-01-01T00:00:00Z',
            command='aws ec2 describe-instances',
            service='ec2',
            action='describe-instances',
            resource_ids=['i-123'],
            account_id='111111111111',
        )
        d = record.to_dict()
        restored = CommandRecord.from_dict(d)
        assert restored.source == record.source
        assert restored.command == record.command
        assert restored.resource_ids == record.resource_ids


class TestSequenceAnalysisData:
    """SequenceAnalysis 資料類別測試"""

    def test_to_dict(self):
        """SequenceAnalysis to_dict"""
        from sequence_analyzer import SequenceAnalysis
        analysis = SequenceAnalysis(
            has_prior_query=True,
            related_commands=['ec2 describe-instances'],
            risk_modifier=-0.2,
            reason='test reason',
            resource_match=True,
            matched_resources=['i-123'],
        )
        d = analysis.to_dict()
        assert d['has_prior_query'] is True
        assert d['risk_modifier'] == -0.2
        assert 'i-123' in d['matched_resources']
