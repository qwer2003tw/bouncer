"""Tests for chain_analyzer module."""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from unittest.mock import patch, MagicMock


def parse_mcp_response(response):
    """Helper to parse MCP response from HTTP response format."""
    if 'body' in response:
        body = json.loads(response['body'])
        return body.get('result', body.get('error'))
    return response


class TestSafeRiskCategory:
    """Tests for _safe_risk_category helper."""

    def test_safe_risk_category_with_enum_value(self):
        """Test extracting risk category from enum."""
        from chain_analyzer import _safe_risk_category

        mock_category = MagicMock()
        mock_category.value = 'high'
        mock_decision = MagicMock()
        mock_decision.risk_result.category = mock_category

        assert _safe_risk_category(mock_decision) == 'high'

    def test_safe_risk_category_with_string(self):
        """Test extracting risk category from plain string."""
        from chain_analyzer import _safe_risk_category

        mock_decision = MagicMock()
        mock_decision.risk_result.category = 'medium'

        assert _safe_risk_category(mock_decision) == 'medium'

    def test_safe_risk_category_none_decision(self):
        """Test handling None decision."""
        from chain_analyzer import _safe_risk_category
        assert _safe_risk_category(None) is None

    def test_safe_risk_category_missing_attribute(self):
        """Test handling missing risk_result attribute."""
        from chain_analyzer import _safe_risk_category
        mock_decision = MagicMock(spec=[])
        assert _safe_risk_category(mock_decision) is None


class TestSafeRiskFactors:
    """Tests for _safe_risk_factors helper."""

    def test_safe_risk_factors_present(self):
        """Test extracting risk factors when present."""
        from chain_analyzer import _safe_risk_factors

        mock_decision = MagicMock()
        mock_decision.risk_factors = ['factor1', 'factor2']

        assert _safe_risk_factors(mock_decision) == ['factor1', 'factor2']

    def test_safe_risk_factors_none_decision(self):
        """Test handling None decision."""
        from chain_analyzer import _safe_risk_factors
        assert _safe_risk_factors(None) is None

    def test_safe_risk_factors_missing_attribute(self):
        """Test handling missing risk_factors attribute."""
        from chain_analyzer import _safe_risk_factors
        mock_decision = MagicMock(spec=[])
        assert _safe_risk_factors(mock_decision) is None


class TestCheckChainRisks:
    """Tests for check_chain_risks main function."""

    @patch('chain_analyzer._split_chain')
    def test_single_command_returns_none(self, mock_split):
        """Test that single command (no chain) returns None."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls']

        ctx = MagicMock()
        ctx.command = 'aws s3 ls'

        result = check_chain_risks(ctx)
        assert result is None

    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer.compliance_checker')
    @patch('chain_analyzer._split_chain')
    def test_all_sub_commands_pass(self, mock_split, mock_compliance, mock_block):
        """Test chain where all sub-commands pass checks."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'aws ec2 describe-instances']
        mock_compliance.check_compliance.return_value = (True, None)
        mock_block.return_value = None

        ctx = MagicMock()
        ctx.command = 'aws s3 ls && aws ec2 describe-instances'

        result = check_chain_risks(ctx)
        assert result is None

    @patch('chain_analyzer.emit_metric')
    @patch('chain_analyzer.commands.aws_cli_split')
    @patch('chain_analyzer._split_chain')
    def test_non_aws_command_in_chain(self, mock_split, mock_aws_split, mock_metric):
        """Test that non-AWS command in chain is blocked."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'rm -rf /']
        mock_aws_split.side_effect = [
            ['aws', 's3', 'ls'],
            ['rm', '-rf', '/']
        ]

        ctx = MagicMock()
        ctx.command = 'aws s3 ls && rm -rf /'
        ctx.req_id = 'test-req-123'

        result = check_chain_risks(ctx)

        assert result is not None
        parsed = parse_mcp_response(result)
        assert parsed['isError'] is True
        content = json.loads(parsed['content'][0]['text'])
        assert content['status'] == 'validation_error'
        assert 'rm' in content['error']
        mock_metric.assert_called_once()

    @patch('chain_analyzer.emit_metric')
    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer.compliance_checker')
    @patch('chain_analyzer._split_chain')
    def test_compliance_violation_in_sub_command(
        self, mock_split, mock_compliance, mock_block, mock_metric
    ):
        """Test chain where sub-command violates compliance."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'aws iam create-user --user-name admin']

        # Create a proper violation object with string attributes
        class MockViolation:
            def __init__(self):
                self.rule_id = 'IAM-001'
                self.rule_name = 'No IAM user creation'
                self.description = 'Creating IAM users is restricted'
                self.remediation = 'Use IAM roles instead'

        mock_violation = MockViolation()
        mock_compliance.check_compliance.side_effect = [
            (True, None),
            (False, mock_violation)
        ]
        # Both commands should not be blocked (compliance catches it first)
        mock_block.return_value = None

        ctx = MagicMock()
        ctx.command = 'aws s3 ls && aws iam create-user --user-name admin'
        ctx.req_id = 'test-req-456'

        result = check_chain_risks(ctx)

        assert result is not None
        parsed = parse_mcp_response(result)
        assert parsed['isError'] is True
        content = json.loads(parsed['content'][0]['text'])
        assert content['status'] == 'compliance_violation'
        assert content['rule_id'] == 'IAM-001'
        mock_metric.assert_called_once()

    @patch('chain_analyzer.emit_metric')
    @patch('chain_analyzer.log_decision')
    @patch('chain_analyzer.generate_request_id')
    @patch('chain_analyzer.send_blocked_notification')
    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer.compliance_checker')
    @patch('chain_analyzer.table')
    @patch('chain_analyzer._split_chain')
    def test_blocked_sub_command(
        self, mock_split, mock_table, mock_compliance, mock_block, mock_notify,
        mock_gen_id, mock_log, mock_metric
    ):
        """Test chain where sub-command is blocked."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'aws iam delete-role --role-name admin']
        mock_compliance.check_compliance.return_value = (True, None)
        mock_block.side_effect = [None, 'IAM role deletion is blocked']
        mock_gen_id.return_value = 'gen-req-789'

        ctx = MagicMock()
        ctx.command = 'aws s3 ls && aws iam delete-role --role-name admin'
        ctx.req_id = 'test-req-789'
        ctx.reason = 'testing'
        ctx.source = 'telegram'
        ctx.account_id = '123456789012'
        ctx.smart_decision = None

        result = check_chain_risks(ctx)

        assert result is not None
        parsed = parse_mcp_response(result)
        assert parsed['isError'] is True
        content = json.loads(parsed['content'][0]['text'])
        assert content['status'] == 'blocked'
        mock_notify.assert_called_once()
        mock_metric.assert_called_once()

    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer.compliance_checker')
    @patch('chain_analyzer._split_chain')
    def test_empty_sub_commands_skipped(self, mock_split, mock_compliance, mock_block):
        """Test that empty sub-commands in chain are skipped."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', '', '  ', 'aws ec2 describe-instances']
        mock_compliance.check_compliance.return_value = (True, None)
        mock_block.return_value = None

        ctx = MagicMock()
        ctx.command = 'aws s3 ls &&  && aws ec2 describe-instances'

        result = check_chain_risks(ctx)
        assert result is None

    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer._split_chain')
    def test_compliance_checker_import_error_handled(self, mock_split, mock_block):
        """Test that ImportError from compliance_checker is handled gracefully."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'aws ec2 describe-instances']
        mock_block.return_value = None

        # Patch compliance_checker.check_compliance to raise ImportError
        with patch('chain_analyzer.compliance_checker.check_compliance', side_effect=ImportError):
            ctx = MagicMock()
            ctx.command = 'aws s3 ls && aws ec2 describe-instances'

            result = check_chain_risks(ctx)
            assert result is None  # Should continue after ImportError

    @patch('chain_analyzer.emit_metric')
    @patch('chain_analyzer.log_decision')
    @patch('chain_analyzer.generate_request_id')
    @patch('chain_analyzer.send_blocked_notification')
    @patch('chain_analyzer.get_block_reason')
    @patch('chain_analyzer.compliance_checker')
    @patch('chain_analyzer.table')
    @patch('chain_analyzer._split_chain')
    def test_logs_decision_with_smart_decision_data(
        self, mock_split, mock_table, mock_compliance, mock_block, mock_notify,
        mock_gen_id, mock_log, mock_metric
    ):
        """Test that smart_decision data is properly extracted and logged."""
        from chain_analyzer import check_chain_risks

        mock_split.return_value = ['aws s3 ls', 'aws iam delete-role --role-name admin']
        mock_compliance.check_compliance.return_value = (True, None)
        # First command passes, second is blocked
        mock_block.side_effect = [None, 'Blocked for security']
        mock_gen_id.return_value = 'gen-req-smart'

        # Create smart_decision with enum-style category
        mock_category = MagicMock()
        mock_category.value = 'critical'
        mock_smart_decision = MagicMock()
        mock_smart_decision.final_score = 95
        mock_smart_decision.risk_result.category = mock_category
        mock_smart_decision.risk_factors = ['iam_operation', 'role_deletion']

        ctx = MagicMock()
        ctx.command = 'aws s3 ls && aws iam delete-role --role-name admin'
        ctx.req_id = 'test-req-smart'
        ctx.reason = 'testing'
        ctx.source = 'telegram'
        ctx.account_id = '123456789012'
        ctx.smart_decision = mock_smart_decision

        check_chain_risks(ctx)

        # Verify log_decision was called with extracted smart_decision data
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert call_kwargs['risk_score'] == 95
        assert call_kwargs['risk_category'] == 'critical'
        assert call_kwargs['risk_factors'] == ['iam_operation', 'role_deletion']
