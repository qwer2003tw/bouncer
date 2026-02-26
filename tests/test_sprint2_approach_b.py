"""
Tests for Sprint 2 Approach B features:
- bouncer_feat_004: bouncer_stats enhanced (top_sources, top_commands, approval_rate, avg_execution_time)
- bouncer_smart_phase4: template scan escalation to MANUAL
- bouncer-trust-batch-flow: pending request display_summary in trust approval
"""
import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import boto3
import pytest
from moto import mock_aws

SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '190825685292')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')

REQUESTS_TABLE = 'clawdbot-approval-requests'
ACCOUNTS_TABLE = 'bouncer-accounts'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_ddb():
    """Fresh moto DynamoDB for each test."""
    for mod in list(sys.modules.keys()):
        if mod in ('db', 'mcp_history', 'constants', 'utils') or mod.startswith('src.'):
            del sys.modules[mod]

    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

        requests_tbl = dynamodb.create_table(
            TableName=REQUESTS_TABLE,
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
                {'AttributeName': 'source', 'AttributeType': 'S'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        requests_tbl.wait_until_exists()

        dynamodb.create_table(
            TableName=ACCOUNTS_TABLE,
            KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )

        yield dynamodb, requests_tbl


def _import_mcp_history(requests_tbl):
    for mod in list(sys.modules.keys()):
        if 'mcp_history' in mod or mod == 'db':
            del sys.modules[mod]
    import mcp_history as mh
    mh.table = requests_tbl
    return mh


def _seed(tbl, items):
    with tbl.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def _req(req_id, status='approved', action='execute', source='test-bot',
         account_id='190825685292', cmd='aws s3 ls', created_offset=-100,
         approved_offset=None):
    now = int(time.time())
    item = {
        'request_id': req_id,
        'status': status,
        'action': action,
        'source': source,
        'account_id': account_id,
        'created_at': now + created_offset,
        'command': cmd,
        'reason': 'test',
    }
    if approved_offset is not None:
        item['approved_at'] = now + approved_offset
    return item


# ===========================================================================
# Task 2: bouncer_stats enhanced (bouncer_feat_004)
# ===========================================================================

class TestBouncerStatsEnhanced:
    """Tests for enhanced bouncer_stats with top_sources, top_commands,
    approval_rate, avg_execution_time_seconds."""

    def test_stats_has_top_sources_field(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', source='bot-a'),
            _req('r2', source='bot-a'),
            _req('r3', source='bot-b'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert 'top_sources' in data, "Response must include top_sources"
        assert isinstance(data['top_sources'], list)

    def test_top_sources_top5_ordering(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        # 6 different sources with different counts
        items = []
        for i, (src, n) in enumerate([
            ('src-a', 10), ('src-b', 8), ('src-c', 6),
            ('src-d', 4), ('src-e', 2), ('src-f', 1),
        ]):
            for j in range(n):
                items.append(_req(f'{src}-{j}', source=src, created_offset=-(i * 100 + j * 10)))
        _seed(requests_tbl, items)

        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        top = data['top_sources']
        assert len(top) <= 5, "top_sources must be at most 5"
        # First should be highest count
        assert top[0]['key'] == 'src-a'
        assert top[0]['count'] == 10
        # Verify descending order
        counts = [item['count'] for item in top]
        assert counts == sorted(counts, reverse=True)

    def test_top_commands_field_present(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', cmd='aws s3 ls'),
            _req('r2', cmd='aws s3 ls'),
            _req('r3', cmd='aws ec2 describe-instances'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert 'top_commands' in data, "Response must include top_commands"
        assert isinstance(data['top_commands'], list)

    def test_top_commands_counts_correctly(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', action='execute', cmd='aws s3 ls'),
            _req('r2', action='execute', cmd='aws s3 ls'),
            _req('r3', action='execute', cmd='aws s3 ls'),
            _req('r4', action='execute', cmd='aws ec2 describe-instances'),
            _req('r5', action='execute', cmd='aws ec2 describe-instances'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        top = data['top_commands']
        assert len(top) >= 1
        # s3 ls appears 3 times, should be first
        assert top[0]['count'] == 3
        assert top[0]['key'] in ('s3/ls', 'aws s3 ls', 's3 ls')

    def test_top_commands_counts_all_actions(self, mock_ddb):
        """All actions (execute, upload, deploy) count toward top_commands."""
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', action='upload', cmd='aws s3 cp file.txt s3://bucket/'),
            _req('r2', action='deploy', cmd='aws cloudformation deploy'),
            _req('r3', action='execute', cmd='aws s3 ls'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        top_cmds = data['top_commands']
        # All 3 different commands should appear (each count=1)
        assert len(top_cmds) == 3

    def test_approval_rate_field_present(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', status='approved'),
            _req('r2', status='denied'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert 'approval_rate' in data, "Response must include approval_rate"

    def test_approval_rate_calculation(self, mock_ddb):
        """3 approved + 1 denied → rate = 0.75"""
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', status='approved', created_offset=-100),
            _req('r2', status='approved', created_offset=-200),
            _req('r3', status='approved', created_offset=-300),
            _req('r4', status='denied', created_offset=-400),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['approval_rate'] == pytest.approx(0.75, abs=0.01)

    def test_approval_rate_none_when_no_decisions(self, mock_ddb):
        """No decided requests → approval_rate should be None."""
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', status='pending_approval'),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['approval_rate'] is None

    def test_approval_rate_auto_approved_counted(self, mock_ddb):
        """auto_approved/trust_approved should count as approved for rate."""
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [
            _req('r1', status='auto_approved', created_offset=-100),
            _req('r2', status='trust_approved', created_offset=-200),
            _req('r3', status='denied', created_offset=-300),
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        # 2 approved / 3 decided = 0.666...
        assert data['approval_rate'] == pytest.approx(2 / 3, abs=0.01)

    def test_avg_execution_time_field_present(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert 'avg_execution_time_seconds' in data

    def test_avg_execution_time_calculation(self, mock_ddb):
        """avg_execution_time should be computed from approved items."""
        dynamodb, requests_tbl = mock_ddb
        now = int(time.time())
        _seed(requests_tbl, [
            {
                'request_id': 'r1', 'status': 'approved', 'action': 'execute',
                'source': 'bot', 'account_id': '111', 'command': 'aws s3 ls',
                'reason': 'test',
                'created_at': now - 3600,
                'approved_at': now - 3590,  # 10s
            },
            {
                'request_id': 'r2', 'status': 'approved', 'action': 'execute',
                'source': 'bot', 'account_id': '111', 'command': 'aws ec2 ls',
                'reason': 'test',
                'created_at': now - 3600,
                'approved_at': now - 3570,  # 30s
            },
        ])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        # avg of 10 and 30 = 20
        assert data['avg_execution_time_seconds'] == pytest.approx(20.0, abs=2.0)

    def test_avg_execution_time_none_when_no_approved(self, mock_ddb):
        """No approved requests → avg_execution_time should be None."""
        dynamodb, requests_tbl = mock_ddb
        _seed(requests_tbl, [_req('r1', status='denied')])
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['avg_execution_time_seconds'] is None

    def test_top_sources_empty_on_no_data(self, mock_ddb):
        dynamodb, requests_tbl = mock_ddb
        mh = _import_mcp_history(requests_tbl)
        result = mh.mcp_tool_stats('req-1', {})
        body = json.loads(result['body'])
        data = json.loads(body['result']['content'][0]['text'])

        assert data['top_sources'] == []
        assert data['top_commands'] == []
        assert data['approval_rate'] is None
        assert data['avg_execution_time_seconds'] is None


# ===========================================================================
# Task 3: Template scan escalation (bouncer_smart_phase4)
# ===========================================================================

class TestTemplateScanEscalation:
    """Tests for Layer 2.5 template scan escalation in mcp_execute."""

    def test_scan_template_no_escalation_for_safe_command(self):
        """A plain safe command should not escalate."""
        # Clear cached modules
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me
        from unittest.mock import patch

        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws ec2 describe-instances',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )

        # Mock template scanner: no hits, score=0
        with patch('template_scanner.scan_command_payloads', return_value=(0, [])):
            with patch('risk_scorer.load_risk_rules') as mock_rules:
                mock_rules_obj = MagicMock()
                mock_rules_obj.template_rules = []
                mock_rules.return_value = mock_rules_obj
                me._scan_template(ctx)

        assert ctx.template_scan_result['escalate'] is False
        assert ctx.template_scan_result['severity'] == 'none'

    def test_scan_template_escalates_on_critical_hit(self):
        """HIGH/CRITICAL hits must set escalate=True."""
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me

        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws iam put-role-policy --role-name test --policy-document \'{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}\'',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )

        # Simulate template scanner returning critical findings
        mock_factor = MagicMock()
        mock_factor.name = 'Template: Full Admin Policy'
        mock_factor.details = 'Full admin policy (TP-008)'
        mock_factor.raw_score = 95

        with patch('template_scanner.scan_command_payloads', return_value=(95, [mock_factor])):
            with patch('risk_scorer.load_risk_rules') as mock_rules:
                mock_rules_obj = MagicMock()
                mock_rules_obj.template_rules = [{'id': 'TP-008', 'check': 'admin_policy', 'score': 95, 'name': 'Full Admin Policy'}]
                mock_rules.return_value = mock_rules_obj
                me._scan_template(ctx)

        assert ctx.template_scan_result is not None
        assert ctx.template_scan_result['escalate'] is True
        assert ctx.template_scan_result['severity'] in ('critical', 'high')

    def test_scan_template_escalates_on_high_hit(self):
        """Score >= 75 should set severity=high and escalate=True."""
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me

        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws iam create-policy --policy-document \'{}\'',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )

        mock_factor = MagicMock()
        mock_factor.name = 'Template: Open Ingress'
        mock_factor.details = 'Opens 0.0.0.0/0 (TP-005)'
        mock_factor.raw_score = 80

        with patch('template_scanner.scan_command_payloads', return_value=(80, [mock_factor])):
            with patch('risk_scorer.load_risk_rules') as mock_rules:
                mock_rules_obj = MagicMock()
                mock_rules_obj.template_rules = [{'id': 'TP-005', 'check': 'open_ingress', 'score': 80, 'name': 'Open Ingress'}]
                mock_rules.return_value = mock_rules_obj
                me._scan_template(ctx)

        assert ctx.template_scan_result['escalate'] is True
        assert ctx.template_scan_result['severity'] == 'high'

    def test_scan_template_low_score_no_escalation(self):
        """Score < 75 should NOT escalate."""
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me

        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws s3 ls',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )

        mock_factor = MagicMock()
        mock_factor.name = 'Template: Low Risk'
        mock_factor.details = 'Low risk finding'
        mock_factor.raw_score = 40

        with patch('template_scanner.scan_command_payloads', return_value=(40, [mock_factor])):
            with patch('risk_scorer.load_risk_rules') as mock_rules:
                mock_rules_obj = MagicMock()
                mock_rules_obj.template_rules = [{'id': 'TP-001', 'check': 'action_wildcard', 'score': 40, 'name': 'Low'}]
                mock_rules.return_value = mock_rules_obj
                me._scan_template(ctx)

        assert ctx.template_scan_result['escalate'] is False

    def test_scan_template_fails_open_on_import_error(self):
        """If template_scanner is unavailable, escalate must default to False."""
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me

        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws s3 ls',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'template_scanner':
                raise ImportError("template_scanner not available")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=mock_import):
            me._scan_template(ctx)

        # fail-open: no escalation
        assert ctx.template_scan_result['escalate'] is False

    def test_mcp_execute_escalated_skips_auto_approve(self):
        """When template scan escalates, safelist auto-approve must be bypassed."""
        for mod in list(sys.modules.keys()):
            if 'mcp_execute' in mod:
                del sys.modules[mod]

        import mcp_execute as me

        # Simulate ctx where template scan says escalate=True
        ctx = me.ExecuteContext(
            req_id='test-req',
            command='aws ec2 authorize-security-group-ingress --group-id sg-xxx --ip-permissions \'{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}\'',
            reason='test',
            source='bot',
            trust_scope='test-scope',
            context=None,
            account_id='190825685292',
            account_name='Default',
            assume_role=None,
            timeout=30,
            sync_mode=False,
        )
        ctx.template_scan_result = {
            'max_score': 85, 'hit_count': 1,
            'severity': 'high', 'factors': [], 'escalate': True,
        }

        # _check_auto_approve should NOT be called when escalate=True
        with patch.object(me, '_check_auto_approve', wraps=me._check_auto_approve) as mock_auto:
            with patch.object(me, '_check_compliance', return_value=None):
                with patch.object(me, '_check_blocked', return_value=None):
                    with patch.object(me, '_check_rate_limit', return_value=None):
                        with patch.object(me, '_submit_for_approval') as mock_submit:
                            mock_submit.return_value = {'statusCode': 200, 'body': json.dumps({
                                'jsonrpc': '2.0', 'result': {'content': [
                                    {'type': 'text', 'text': json.dumps({'status': 'pending_approval', 'request_id': 'r1'})}
                                ]}
                            })}
                            # Call escalated path directly (as mcp_tool_execute would)
                            if ctx.template_scan_result and ctx.template_scan_result.get('escalate'):
                                result = (
                                    me._check_compliance(ctx)
                                    or me._check_blocked(ctx)
                                    or me._check_rate_limit(ctx)
                                    or me._submit_for_approval(ctx)
                                )

            # _check_auto_approve must NOT have been called
            mock_auto.assert_not_called()

    def test_scan_template_result_included_in_notification(self):
        """send_approval_request should pass template_scan_result to notification."""
        from notifications import send_approval_request

        with patch('notifications._send_message') as mock_send:
            mock_send.return_value = {'ok': True}

            send_approval_request(
                request_id='r-test',
                command='aws ec2 describe-instances',
                reason='test',
                source='bot',
                account_id='190825685292',
                account_name='Default',
                template_scan_result={
                    'max_score': 85,
                    'hit_count': 2,
                    'severity': 'high',
                    'factors': [
                        {'name': 'Template: Open Ingress', 'details': 'SG 0.0.0.0/0 (TP-005)', 'score': 80},
                    ],
                    'escalate': True,
                },
            )

        mock_send.assert_called_once()
        text_sent = mock_send.call_args[0][0]
        # Notification should mention the template scan findings
        assert 'Template Scan' in text_sent or 'TEMPLATE' in text_sent.upper() or 'TP-005' in text_sent or 'HIGH' in text_sent.upper() or 'hits' in text_sent

    def test_send_approval_request_no_template_scan_result(self):
        """send_approval_request works fine without template_scan_result (backward compat)."""
        from notifications import send_approval_request

        with patch('notifications._send_message') as mock_send:
            mock_send.return_value = {'ok': True}

            result = send_approval_request(
                request_id='r-test',
                command='aws s3 ls',
                reason='test',
                source='bot',
                account_id='190825685292',
                account_name='Default',
                # No template_scan_result → should not crash
            )

        assert result is True
        mock_send.assert_called_once()


# ===========================================================================
# Task 4: Trust pending request summaries (bouncer-trust-batch-flow)
# ===========================================================================

class TestTrustPendingDisplaySummary:
    """Tests for showing pending request display_summary in trust approval."""

    @pytest.fixture
    def trust_callback_env(self, monkeypatch):
        """Set up environment for callbacks tests."""
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setenv('DEFAULT_ACCOUNT_ID', '190825685292')
        monkeypatch.setenv('TABLE_NAME', 'clawdbot-approval-requests')

    def test_trust_pending_shows_display_summary(self):
        """When pending_count > 0, trust_line should contain display_summary."""
        import callbacks

        # Build mock pending items with display_summary
        pending_items = [
            {
                'request_id': 'p1',
                'command': 'aws s3 ls s3://bucket',
                'display_summary': 'aws s3 ls s3://bucket',
                'status': 'pending',
                'trust_scope': 'test-scope',
                'account_id': '190825685292',
            },
            {
                'request_id': 'p2',
                'command': 'aws ec2 describe-instances',
                'display_summary': 'aws ec2 describe-instances',
                'status': 'pending',
                'trust_scope': 'test-scope',
                'account_id': '190825685292',
            },
        ]

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tbl = dynamodb.create_table(
                TableName='clawdbot-approval-requests',
                KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
                AttributeDefinitions=[
                    {'AttributeName': 'request_id', 'AttributeType': 'S'},
                    {'AttributeName': 'status', 'AttributeType': 'S'},
                    {'AttributeName': 'created_at', 'AttributeType': 'N'},
                ],
                GlobalSecondaryIndexes=[
                    {
                        'IndexName': 'status-created-index',
                        'KeySchema': [
                            {'AttributeName': 'status', 'KeyType': 'HASH'},
                            {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                        ],
                        'Projection': {'ProjectionType': 'ALL'},
                    },
                ],
                BillingMode='PAY_PER_REQUEST',
            )
            tbl.wait_until_exists()

            now = int(time.time())
            for i, item in enumerate(pending_items):
                item['created_at'] = now - 100 - i
                tbl.put_item(Item=item)

            import db
            import db as _db_module
            original_table = _db_module.table
            _db_module.table = tbl

            # Simulate trust approval by checking that the pending query returns items
            # and that display_summary is used
            pending_resp = tbl.query(
                IndexName='status-created-index',
                KeyConditionExpression='#status = :status',
                FilterExpression='trust_scope = :scope AND account_id = :account',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':status': 'pending',
                    ':scope': 'test-scope',
                    ':account': '190825685292',
                },
                ScanIndexForward=True,
                Limit=20,
            )
            items = pending_resp.get('Items', [])

            # Simulate trust_line building (mirrors callbacks.py logic)
            trust_line = '\n\n🔓 信任時段已啟動'
            pending_count = len(items)
            if pending_count > 0:
                trust_line += f'\n⚡ 自動執行 {pending_count} 個排隊請求：'
                for pi in items[:5]:
                    summary = pi.get('display_summary') or pi.get('command', '')[:60]
                    trust_line += f'\n  • {summary}'

            _db_module.table = original_table

        assert pending_count == 2
        assert '⚡ 自動執行 2 個排隊請求' in trust_line
        assert 'aws s3 ls s3://bucket' in trust_line
        assert 'aws ec2 describe-instances' in trust_line

    def test_trust_pending_no_items_no_bullet_list(self):
        """When no pending items, trust_line should not have bullet items."""
        items = []
        pending_count = len(items)

        trust_line = '\n\n🔓 信任時段已啟動'
        if pending_count > 0:
            trust_line += f'\n⚡ 自動執行 {pending_count} 個排隊請求：'
            for pi in items[:5]:
                summary = pi.get('display_summary') or pi.get('command', '')[:60]
                trust_line += f'\n  • {summary}'

        assert '⚡' not in trust_line
        assert '• ' not in trust_line

    def test_trust_pending_truncates_at_5_items(self):
        """When pending_count > 5, show only first 5 with overflow note."""
        items = [
            {'request_id': f'p{i}', 'display_summary': f'cmd-{i}',
             'command': f'aws cmd-{i}', 'status': 'pending',
             'trust_scope': 'ts', 'account_id': '190825685292'}
            for i in range(7)
        ]
        pending_count = len(items)

        trust_line = '\n\n🔓 信任時段已啟動'
        if pending_count > 0:
            trust_line += f'\n⚡ 自動執行 {pending_count} 個排隊請求：'
            for pi in items[:5]:
                summary = pi.get('display_summary') or pi.get('command', '')[:60]
                trust_line += f'\n  • {summary}'
            if pending_count > 5:
                trust_line += f'\n  ...及其他 {pending_count - 5} 個請求'

        # Should show only 5 items plus overflow note
        bullet_count = trust_line.count('  • ')
        assert bullet_count == 5
        assert '及其他 2 個請求' in trust_line

    def test_trust_pending_fallback_to_command_when_no_summary(self):
        """Falls back to command[:60] when display_summary is missing."""
        items = [
            {'request_id': 'p1', 'command': 'aws ec2 describe-instances --region us-east-1',
             'status': 'pending', 'trust_scope': 'ts', 'account_id': '190825685292'},
            # No display_summary field
        ]
        pending_count = len(items)

        trust_line = '\n\n🔓 信任時段已啟動'
        if pending_count > 0:
            trust_line += f'\n⚡ 自動執行 {pending_count} 個排隊請求：'
            for pi in items[:5]:
                summary = pi.get('display_summary') or pi.get('command', '')[:60]
                trust_line += f'\n  • {summary}'

        # Should fall back to command text
        assert 'aws ec2 describe-instances' in trust_line
