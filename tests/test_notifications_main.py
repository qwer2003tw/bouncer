"""
test_notifications_main.py — Notifications 與 display summary 測試
Extracted from test_bouncer.py batch-b
"""

import json
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from moto import mock_aws
import boto3


# ============================================================================
# Generate Display Summary 測試
# ============================================================================

class TestGenerateDisplaySummary:
    """Tests for generate_display_summary() helper function in utils.py"""

    def test_execute_command(self, app_module):
        """Execute action uses command[:100]"""
        from utils import generate_display_summary
        result = generate_display_summary('execute', command='aws s3 ls --region us-east-1')
        assert result == 'aws s3 ls --region us-east-1'

    def test_execute_command_truncation(self, app_module):
        """Execute command truncated to 100 chars"""
        from utils import generate_display_summary
        long_cmd = 'aws s3 cp ' + 'x' * 200
        result = generate_display_summary('execute', command=long_cmd)
        assert len(result) == 100
        assert result == long_cmd[:100]

    def test_execute_empty_command(self, app_module):
        """Execute with empty command shows fallback"""
        from utils import generate_display_summary
        result = generate_display_summary('execute', command='')
        assert result == '(empty command)'

    def test_execute_no_action(self, app_module):
        """No action defaults to execute behavior"""
        from utils import generate_display_summary
        result = generate_display_summary('', command='aws sts get-caller-identity')
        assert result == 'aws sts get-caller-identity'

    def test_upload_with_size(self, app_module):
        """Upload shows filename and size"""
        from utils import generate_display_summary
        result = generate_display_summary('upload', filename='index.html', content_size=12288)
        assert result == 'upload: index.html (12.00 KB)'

    def test_upload_without_size(self, app_module):
        """Upload without size shows just filename"""
        from utils import generate_display_summary
        result = generate_display_summary('upload', filename='index.html')
        assert result == 'upload: index.html'

    def test_upload_batch_with_size(self, app_module):
        """Upload batch shows count and total size"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch', file_count=9, total_size=250880)
        assert result == 'upload_batch (9 個檔案, 245.00 KB)'

    def test_upload_batch_without_size(self, app_module):
        """Upload batch without total_size shows just count"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch', file_count=5)
        assert result == 'upload_batch (5 個檔案)'

    def test_upload_batch_missing_count(self, app_module):
        """Upload batch with missing file_count shows 'unknown'"""
        from utils import generate_display_summary
        result = generate_display_summary('upload_batch')
        assert 'unknown' in result

    def test_add_account(self, app_module):
        """Add account shows name and ID"""
        from utils import generate_display_summary
        result = generate_display_summary('add_account', account_name='Dev', account_id='992382394211')
        assert result == 'add_account: Dev (992382394211)'

    def test_remove_account(self, app_module):
        """Remove account shows name and ID"""
        from utils import generate_display_summary
        result = generate_display_summary('remove_account', account_name='Dev', account_id='992382394211')
        assert result == 'remove_account: Dev (992382394211)'

    def test_deploy(self, app_module):
        """Deploy shows project_id"""
        from utils import generate_display_summary
        result = generate_display_summary('deploy', project_id='bouncer')
        assert result == 'deploy: bouncer'

    def test_deploy_missing_project(self, app_module):
        """Deploy with missing project_id shows fallback"""
        from utils import generate_display_summary
        result = generate_display_summary('deploy')
        assert result == 'deploy: unknown project'

    def test_unknown_action(self, app_module):
        """Unknown action returns action name"""
        from utils import generate_display_summary
        result = generate_display_summary('some_future_action')
        assert result == 'some_future_action'


# ============================================================================
# Display Summary In Items 測試
# ============================================================================

class TestDisplaySummaryInItems:
    """Tests that display_summary is written to DynamoDB items"""

    @patch('mcp_execute.send_approval_request')
    @patch('mcp_execute.send_blocked_notification')
    def test_execute_item_has_display_summary(self, mock_blocked, mock_approval, app_module):
        """Execute approval item has display_summary field"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-exec-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_execute', 'arguments': {
                    'command': 'aws s3 cp local.txt s3://my-bucket/file.txt',
                    'trust_scope': 'test-session',
                    'reason': 'test display summary',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'] == 'aws s3 cp local.txt s3://my-bucket/file.txt'

    @patch('telegram.send_telegram_message')
    def test_upload_item_has_display_summary(self, mock_telegram, app_module):
        """Upload approval item has display_summary field"""
        import base64
        content_b64 = base64.b64encode(b'test content').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-upload-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_upload', 'arguments': {
                    'filename': 'test.js',
                    'content': content_b64,
                    'content_type': 'application/javascript',
                    'reason': 'test display summary',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'].startswith('upload: test.js')

    @patch('mcp_upload.send_batch_upload_notification')
    def test_upload_batch_item_has_display_summary(self, mock_notification, app_module):
        """Upload batch approval item has display_summary field"""
        import base64
        content_b64 = base64.b64encode(b'test content').decode()

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-batch-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_upload_batch', 'arguments': {
                    'files': [
                        {'filename': 'a.js', 'content': content_b64, 'content_type': 'application/javascript'},
                        {'filename': 'b.js', 'content': content_b64, 'content_type': 'application/javascript'},
                    ],
                    'reason': 'test display summary',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert 'upload_batch' in item['display_summary']
        assert '2 個檔案' in item['display_summary']

    @patch('mcp_admin.send_account_approval_request')
    def test_add_account_item_has_display_summary(self, mock_approval, app_module):
        """Add account approval item has display_summary field"""
        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-add-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_add_account', 'arguments': {
                    'account_id': '222222222222',
                    'name': 'TestAccount',
                    'role_arn': 'arn:aws:iam::222222222222:role/BouncerExecutionRole',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert item['display_summary'] == 'add_account: TestAccount (222222222222)'

    @patch('mcp_admin.send_account_approval_request')
    def test_remove_account_item_has_display_summary(self, mock_approval, app_module):
        """Remove account approval item has display_summary field"""
        # First add the account so it exists for removal
        import accounts
        import db
        db.accounts_table.put_item(Item={
            'account_id': '333333333333',
            'name': 'RemoveMe',
            'role_arn': 'arn:aws:iam::333333333333:role/BouncerExecutionRole',
            'enabled': True,
        })
        # Clear cache
        if hasattr(accounts, '_accounts_cache'):
            accounts._accounts_cache = {}

        event = {
            'rawPath': '/mcp',
            'headers': {'x-approval-secret': os.environ.get('REQUEST_SECRET', 'test-secret')},
            'body': json.dumps({
                'jsonrpc': '2.0', 'id': 'ds-remove-1', 'method': 'tools/call',
                'params': {'name': 'bouncer_remove_account', 'arguments': {
                    'account_id': '333333333333',
                    'source': 'test-bot',
                }}
            })
        }
        result = app_module.lambda_handler(event, None)
        body = json.loads(result['body'])
        content = json.loads(body['result']['content'][0]['text'])
        request_id = content.get('request_id')
        assert request_id

        # Check DynamoDB item
        item = app_module.table.get_item(Key={'request_id': request_id}).get('Item')
        assert item is not None
        assert 'display_summary' in item
        assert 'remove_account' in item['display_summary']
        assert '333333333333' in item['display_summary']
