"""Regression tests for deploy progress notifications (bouncer-s41-001)"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

from unittest.mock import patch, MagicMock
import sam_deploy


def test_notify_progress_invokes_lambda():
    """_notify_progress calls Lambda invoke with correct payload"""
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {'StatusCode': 202}

    with patch.dict(os.environ, {'NOTIFIER_LAMBDA_ARN': 'arn:aws:lambda:us-east-1:123:function:notifier'}):
        with patch('boto3.client', return_value=mock_lambda):
            sam_deploy._notify_progress('deploy-abc123', 'ztp-files', 'BUILDING')

    mock_lambda.invoke.assert_called_once()
    call_kwargs = mock_lambda.invoke.call_args[1]
    assert call_kwargs['FunctionName'] == 'arn:aws:lambda:us-east-1:123:function:notifier'
    assert call_kwargs['InvocationType'] == 'Event'
    import json
    payload = json.loads(call_kwargs['Payload'])
    assert payload['action'] == 'progress'
    assert payload['phase'] == 'BUILDING'
    assert payload['deploy_id'] == 'deploy-abc123'
    assert payload['project_id'] == 'ztp-files'


def test_notify_progress_no_arn_skips():
    """_notify_progress skips silently when NOTIFIER_LAMBDA_ARN not set"""
    with patch.dict(os.environ, {}, clear=True):
        # Should not raise
        sam_deploy._notify_progress('deploy-abc', 'proj', 'SCANNING')


def test_notify_progress_lambda_error_non_fatal():
    """_notify_progress swallows Lambda errors (non-fatal)"""
    mock_lambda = MagicMock()
    mock_lambda.invoke.side_effect = Exception('Lambda error')

    with patch.dict(os.environ, {'NOTIFIER_LAMBDA_ARN': 'arn:aws:lambda:us-east-1:123:function:notifier'}):
        with patch('boto3.client', return_value=mock_lambda):
            # Should not raise
            sam_deploy._notify_progress('deploy-abc', 'proj', 'SCANNING')
