"""
Tests for sprint13-001 Phase 2: entities migration for 3 core notification functions.

Verifies that send_approval_request, send_blocked_notification, and
send_account_approval_request:
  - Call send_message_with_entities (not _send_message / MarkdownV2)
  - Pass a non-empty entities list
  - Do NOT include parse_mode in the call arguments
  - Produce correct text content (key fields present)
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch


os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('APPROVED_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')


def _make_notifications():
    """Reload notifications module with clean state."""
    for mod in ['notifications', 'telegram', 'commands', 'constants', 'utils',
                'risk_scorer', 'template_scanner', 'scheduler_service',
                'telegram_entities']:
        sys.modules.pop(mod, None)
        sys.modules.pop(f'src.{mod}', None)
    import notifications as _n
    return _n


def _patch_entities(notif_module):
    """Return (context_manager, mock) that patches _telegram.send_message_with_entities."""
    mock_fn = MagicMock(return_value={'ok': True, 'result': {'message_id': 42}})
    ctx = patch.object(notif_module._telegram, 'send_message_with_entities', mock_fn)
    return ctx, mock_fn


# ============================================================================
# send_approval_request — entities mode
# ============================================================================

class TestSendApprovalRequestEntities:
    """send_approval_request must use entities mode (no parse_mode)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.notif = _make_notifications()

    def test_uses_send_message_with_entities(self):
        """Must call _telegram.send_message_with_entities (not _send_message)."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-001',
                command='aws s3 ls',
                reason='phase2 test',
            )
        mock_fn.assert_called_once()

    def test_entities_list_is_non_empty(self):
        """entities arg must be a non-empty list (has at least bold/code entities)."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-002',
                command='aws ec2 describe-instances',
                reason='check instances',
                source='TestBot',
            )
        call_args = mock_fn.call_args[0]
        entities = call_args[1]
        assert isinstance(entities, list), "entities must be a list"
        assert len(entities) > 0, "entities list must not be empty"

    def test_no_parse_mode_in_call(self):
        """send_message_with_entities must not receive a parse_mode argument."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-003',
                command='aws s3 ls',
                reason='no parse_mode test',
            )
        _, call_kwargs = mock_fn.call_args
        assert 'parse_mode' not in call_kwargs, \
            "parse_mode must not be passed to send_message_with_entities"

    def test_text_contains_request_id(self):
        """Plain text must contain the request_id."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-004',
                command='aws s3 ls',
                reason='contain id test',
            )
        text = mock_fn.call_args[0][0]
        assert 'req-p2-004' in text

    def test_text_contains_command(self):
        """Plain text must contain the command preview."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-005',
                command='aws iam list-users',
                reason='iam check',
            )
        text = mock_fn.call_args[0][0]
        assert 'aws iam list-users' in text

    def test_text_contains_reason(self):
        """Plain text must contain the reason."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-006',
                command='aws s3 ls',
                reason='unique_reason_xyz',
            )
        text = mock_fn.call_args[0][0]
        assert 'unique_reason_xyz' in text

    def test_entities_contain_bold_type(self):
        """Entities list must include at least one bold entity."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-007',
                command='aws s3 ls',
                reason='bold entity test',
            )
        entities = mock_fn.call_args[0][1]
        bold_entities = [e for e in entities if e.get('type') == 'bold']
        assert len(bold_entities) > 0, "Must have at least one bold entity"

    def test_entities_contain_code_type(self):
        """Entities list must include at least one code entity (command is code-formatted)."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_approval_request(
                request_id='req-p2-008',
                command='aws s3 ls',
                reason='code entity test',
            )
        entities = mock_fn.call_args[0][1]
        code_entities = [e for e in entities if e.get('type') == 'code']
        assert len(code_entities) > 0, "Must have at least one code entity (command)"

    def test_returns_notification_result(self):
        """Must return a NotificationResult namedtuple with ok field."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            result = self.notif.send_approval_request(
                request_id='req-p2-009',
                command='aws s3 ls',
                reason='return type test',
            )
        assert hasattr(result, 'ok')
        assert result.ok is True


# ============================================================================
# send_blocked_notification — entities mode
# ============================================================================

class TestSendBlockedNotificationEntities:
    """send_blocked_notification must use entities mode (no parse_mode)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.notif = _make_notifications()

    def test_uses_send_message_with_entities(self):
        """Must call _telegram.send_message_with_entities."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws iam delete-role',
                block_reason='blocked by policy',
                source='TestBot',
            )
        mock_fn.assert_called_once()

    def test_entities_list_is_non_empty(self):
        """entities arg must be a non-empty list."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws iam delete-role',
                block_reason='blocked by safelist',
            )
        entities = mock_fn.call_args[0][1]
        assert isinstance(entities, list)
        assert len(entities) > 0

    def test_no_parse_mode_in_call(self):
        """parse_mode must not be passed to send_message_with_entities."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws ec2 terminate-instances',
                block_reason='test block',
            )
        _, call_kwargs = mock_fn.call_args
        assert 'parse_mode' not in call_kwargs

    def test_text_contains_command(self):
        """Plain text must contain command preview."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws s3 rm s3://bucket',
                block_reason='blocked',
            )
        text = mock_fn.call_args[0][0]
        assert 'aws s3 rm s3://bucket' in text

    def test_text_contains_block_reason(self):
        """Plain text must contain block_reason."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws iam delete-role',
                block_reason='explicitly_blocked_reason',
            )
        text = mock_fn.call_args[0][0]
        assert 'explicitly_blocked_reason' in text

    def test_entities_contain_bold_type(self):
        """Must have at least one bold entity."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_blocked_notification(
                command='aws s3 rm',
                block_reason='policy block',
            )
        entities = mock_fn.call_args[0][1]
        assert any(e.get('type') == 'bold' for e in entities)


# ============================================================================
# send_account_approval_request — entities mode
# ============================================================================

class TestSendAccountApprovalRequestEntities:
    """send_account_approval_request must use entities mode (no parse_mode)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.notif = _make_notifications()

    def test_uses_send_message_with_entities_add(self):
        """add action must call send_message_with_entities."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-001',
                action='add',
                account_id='111122223333',
                name='StagingAccount',
                role_arn='arn:aws:iam::111122223333:role/BouncerRole',
                source='TestBot',
            )
        mock_fn.assert_called_once()

    def test_uses_send_message_with_entities_remove(self):
        """remove action must also call send_message_with_entities."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-002',
                action='remove',
                account_id='111122223333',
                name='OldAccount',
                role_arn='',
                source='TestBot',
            )
        mock_fn.assert_called_once()

    def test_entities_list_is_non_empty(self):
        """entities arg must be a non-empty list."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-003',
                action='add',
                account_id='222233334444',
                name='ProdAccount',
                role_arn='arn:aws:iam::222233334444:role/BouncerRole',
                source='TestBot',
            )
        entities = mock_fn.call_args[0][1]
        assert isinstance(entities, list)
        assert len(entities) > 0

    def test_no_parse_mode_in_call(self):
        """parse_mode must not be passed."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-004',
                action='add',
                account_id='333344445555',
                name='DevAccount',
                role_arn='arn:aws:iam::333344445555:role/BouncerRole',
                source='TestBot',
            )
        _, call_kwargs = mock_fn.call_args
        assert 'parse_mode' not in call_kwargs

    def test_text_contains_account_id(self):
        """Plain text must contain account_id."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-005',
                action='add',
                account_id='999988887777',
                name='TestAccount',
                role_arn='arn:aws:iam::999988887777:role/BouncerRole',
                source='TestBot',
            )
        text = mock_fn.call_args[0][0]
        assert '999988887777' in text

    def test_text_contains_request_id(self):
        """Plain text must contain the request_id."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-unique-id',
                action='remove',
                account_id='123456789012',
                name='OldAcct',
                role_arn='',
                source='TestBot',
            )
        text = mock_fn.call_args[0][0]
        assert 'acct-p2-unique-id' in text

    def test_entities_contain_bold_type(self):
        """Must have at least one bold entity."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-007',
                action='add',
                account_id='111100001111',
                name='BoldTest',
                role_arn='arn:aws:iam::111100001111:role/TestRole',
                source='TestBot',
            )
        entities = mock_fn.call_args[0][1]
        assert any(e.get('type') == 'bold' for e in entities)

    def test_entities_contain_code_type(self):
        """Must have at least one code entity (account_id is code-formatted)."""
        ctx, mock_fn = _patch_entities(self.notif)
        with ctx:
            self.notif.send_account_approval_request(
                request_id='acct-p2-008',
                action='add',
                account_id='555566667777',
                name='CodeTest',
                role_arn='arn:aws:iam::555566667777:role/TestRole',
                source='TestBot',
            )
        entities = mock_fn.call_args[0][1]
        assert any(e.get('type') == 'code' for e in entities)
