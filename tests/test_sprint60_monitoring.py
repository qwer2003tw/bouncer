"""
Tests for Sprint 60 Monitoring - Smart Approval metrics emission
"""
import os
import sys
from unittest.mock import patch, MagicMock

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestSmartApprovalMetrics:
    """Tests for smart_approval.py metric emission"""

    @patch('smart_approval.emit_metric')
    @patch('smart_approval.get_sequence_risk_modifier')
    @patch('smart_approval.calculate_risk')
    def test_smart_approval_emits_decision_metric(self, mock_calc_risk, mock_seq_modifier, mock_emit):
        """evaluate_command success → SmartApprovalDecision metric emitted"""
        from smart_approval import evaluate_command, ApprovalDecision
        from risk_scorer import RiskResult, RiskCategory

        # Mock calculate_risk to return low-risk result
        mock_calc_risk.return_value = RiskResult(
            score=20,
            category=RiskCategory.SAFE,
            factors=[],
            recommendation="Low risk",
            parsed_command={'service': 's3', 'action': 'ls'}
        )
        mock_seq_modifier.return_value = (0.0, "No sequence risk")

        # Call evaluate_command
        result = evaluate_command(
            command='aws s3 ls',
            user='test-user',
            account='123456789012',
            role='TestRole'
        )

        # Verify decision metric was emitted
        decision_calls = [c for c in mock_emit.call_args_list
                         if c[0][1] == 'SmartApprovalDecision']
        assert len(decision_calls) == 1
        assert decision_calls[0][0][0] == 'Bouncer'
        assert decision_calls[0][0][2] == 1
        assert decision_calls[0][1]['dimensions'][0]['Name'] == 'Decision'
        assert result.decision == ApprovalDecision.AUTO_APPROVE

    @patch('smart_approval.emit_metric')
    @patch('smart_approval.get_sequence_risk_modifier')
    @patch('smart_approval.calculate_risk')
    def test_smart_approval_emits_score_metric(self, mock_calc_risk, mock_seq_modifier, mock_emit):
        """evaluate_command success → SmartApprovalScore metric emitted"""
        from smart_approval import evaluate_command
        from risk_scorer import RiskResult, RiskCategory

        # Mock calculate_risk to return medium-risk result
        mock_calc_risk.return_value = RiskResult(
            score=50,
            category=RiskCategory.MANUAL,
            factors=[],
            recommendation="Medium risk",
            parsed_command={'service': 'ec2', 'action': 'stop-instances'}
        )
        mock_seq_modifier.return_value = (0.0, "No sequence risk")

        # Call evaluate_command
        result = evaluate_command(
            command='aws ec2 stop-instances --instance-ids i-123',
            user='test-user',
            account='123456789012',
            role='TestRole'
        )

        # Verify score metric was emitted
        score_calls = [c for c in mock_emit.call_args_list
                      if c[0][1] == 'SmartApprovalScore']
        assert len(score_calls) == 1
        assert score_calls[0][0][0] == 'Bouncer'
        assert score_calls[0][0][2] == result.final_score

    @patch('smart_approval.emit_metric')
    @patch('smart_approval.get_sequence_risk_modifier')
    @patch('smart_approval.calculate_risk')
    def test_smart_approval_error_emits_metric(self, mock_calc_risk, mock_seq_modifier, mock_emit):
        """evaluate_command exception → SmartApprovalError metric emitted"""
        from smart_approval import evaluate_command

        # Mock calculate_risk to raise exception
        mock_calc_risk.side_effect = Exception("Test error")
        mock_seq_modifier.return_value = (0.0, "No sequence risk")

        # Call evaluate_command (should handle exception gracefully)
        _result = evaluate_command(
            command='aws s3 ls',
            user='test-user',
            account='123456789012',
            role='TestRole'
        )

        # Verify error metric was emitted
        error_calls = [c for c in mock_emit.call_args_list
                      if c[0][1] == 'SmartApprovalError']
        assert len(error_calls) == 1
        assert error_calls[0][0][0] == 'Bouncer'
        assert error_calls[0][0][2] == 1


class TestSequenceAnalyzerMetrics:
    """Tests for sequence_analyzer.py metric emission"""

    @patch('sequence_analyzer.emit_metric')
    @patch('sequence_analyzer.get_recent_commands')
    def test_sequence_analyzer_emits_risk_modifier(self, mock_get_recent, mock_emit):
        """analyze_sequence → SequenceRiskModifier metric emitted"""
        from sequence_analyzer import analyze_sequence

        # Mock get_recent_commands to return some history
        mock_get_recent.return_value = [
            MagicMock(
                command='aws ec2 describe-instances',
                timestamp=1234567890,
                action='describe-instances',
                resource_ids=[]
            )
        ]

        # Call analyze_sequence
        result = analyze_sequence(
            current_command='aws ec2 terminate-instances --instance-ids i-123',
            user='test-user',
            account='123456789012',
            role='TestRole'
        )

        # Verify risk modifier metric was emitted
        modifier_calls = [c for c in mock_emit.call_args_list
                         if c[0][1] == 'SequenceRiskModifier']
        assert len(modifier_calls) == 1
        assert modifier_calls[0][0][0] == 'Bouncer'
        assert modifier_calls[0][0][2] == result.risk_modifier

    @patch('sequence_analyzer.emit_metric')
    @patch('sequence_analyzer.get_recent_commands')
    def test_sequence_analyzer_emits_positive_modifier(self, mock_get_recent, mock_emit):
        """analyze_sequence without prior query → positive risk_modifier emitted"""
        from sequence_analyzer import analyze_sequence

        # Mock get_recent_commands to return no relevant history
        mock_get_recent.return_value = []

        # Call analyze_sequence with destructive command
        _result = analyze_sequence(
            current_command='aws ec2 terminate-instances --instance-ids i-123',
            user='test-user',
            account='123456789012',
            role='TestRole'
        )

        # Verify positive risk modifier was emitted (no prior query)
        modifier_calls = [c for c in mock_emit.call_args_list
                         if c[0][1] == 'SequenceRiskModifier']
        assert len(modifier_calls) == 1
        assert modifier_calls[0][0][2] > 0  # Positive modifier (increased risk)
