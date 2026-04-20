"""
test_notifications_entities_phase2.py — Sprint 13-001 Phase 2
Tests for entities-mode migration of 3 core notification functions:
  - send_approval_request
  - send_blocked_notification
  - send_account_approval_request

Verifies:
  - No parse_mode in Telegram API calls
  - entities list is passed (not empty)
  - Content correctness (request_id, reason, command, etc.)
  - inline keyboard (reply_markup) still passes through correctly
  - Special characters (Markdown-unsafe) don't break messages
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, call

# Ensure src is on path
_SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '99999')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')


def _make_notifications_module():
    """Reload notifications and its dependencies with mocked env."""
    for mod in ['notifications', 'notifications_core', 'notifications_execute', 'notifications_grant',
                'telegram', 'commands', 'constants', 'utils',
                'risk_scorer', 'template_scanner', 'scheduler_service', 'telegram_entities']:
        sys.modules.pop(mod, None)
        sys.modules.pop(f'src.{mod}', None)

    import notifications as _n
    return _n


def _capture_send_with_entities(notif_module):
    """Return (captured_calls, context_manager) for send_message_with_entities."""
    captured = []

    def fake_send(text, entities, reply_markup=None, silent=False):
        captured.append({
            'text': text,
            'entities': entities,
            'reply_markup': reply_markup,
            'silent': silent,
        })
        return {'ok': True, 'result': {'message_id': 42}}

    ctx = patch.object(notif_module._telegram, 'send_message_with_entities', side_effect=fake_send)
    return captured, ctx


# ===========================================================================
# send_approval_request — entities mode
# ===========================================================================

class TestSendApprovalRequestEntities:
    """Verify send_approval_request uses entities mode (no parse_mode)."""

    @pytest.fixture(autouse=True)
    def _reload(self):
        self.notif = _make_notifications_module()

    def _call(self, **kwargs):
        """Call send_approval_request and return (result, captured)."""
        captured, ctx = _capture_send_with_entities(self.notif)
        defaults = dict(
            request_id='req-e1',
            command='aws s3 ls',
            reason='list buckets',
            source='TestBot',
        )
        defaults.update(kwargs)
        with ctx:
            result = self.notif.send_approval_request(**defaults)
        return result, captured

    # --- no parse_mode ---

    def test_no_parse_mode_in_payload(self):
        """send_approval_request must NOT use parse_mode."""
        captured_payload = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured_payload.update(data)
            return {'ok': True, 'result': {'message_id': 1}}

        with patch('telegram._telegram_request', side_effect=fake_request):
            self.notif.send_approval_request(
                request_id='req-no-pm',
                command='aws s3 ls',
                reason='test',
            )

        assert 'parse_mode' not in captured_payload

    # --- entities list ---

    def test_entities_list_is_non_empty(self):
        """Entities list must be non-empty (we format with bold/code)."""
        _, captured = self._call()
        assert len(captured) == 1
        assert len(captured[0]['entities']) > 0

    def test_entities_list_contains_bold(self):
        """At least one bold entity is present."""
        _, captured = self._call()
        types = [e['type'] for e in captured[0]['entities']]
        assert 'bold' in types

    def test_entities_list_contains_code(self):
        """At least one code entity is present (command and request_id)."""
        _, captured = self._call()
        types = [e['type'] for e in captured[0]['entities']]
        assert 'code' in types

    # --- content correctness ---

    def test_text_contains_request_id(self):
        """Text must contain request_id."""
        _, captured = self._call(request_id='req-check-1')
        assert 'req-check-1' in captured[0]['text']

    def test_text_contains_reason(self):
        """Text must contain reason verbatim (no escaping)."""
        _, captured = self._call(reason='test reason xyz')
        assert 'test reason xyz' in captured[0]['text']

    def test_text_contains_command(self):
        """Text must contain command."""
        _, captured = self._call(command='aws sts get-caller-identity')
        assert 'aws sts get-caller-identity' in captured[0]['text']

    def test_text_contains_source(self):
        """Text must contain source."""
        _, captured = self._call(source='SpecialBot')
        assert 'SpecialBot' in captured[0]['text']

    def test_text_contains_account_id_and_name(self):
        """Account ID and name appear in text."""
        _, captured = self._call(account_id='123456789012', account_name='DevAccount')
        assert '123456789012' in captured[0]['text']
        assert 'DevAccount' in captured[0]['text']

    def test_text_contains_assume_role_account_id(self):
        """assume_role ARN's account ID appears in text."""
        _, captured = self._call(assume_role='arn:aws:iam::999888777666:role/BouncerRole')
        assert '999888777666' in captured[0]['text']

    # --- special characters don't break ---

    def test_markdown_special_chars_in_reason_ok(self):
        """Reason with Markdown-unsafe chars (_*[]`~) works without crash."""
        result, _ = self._call(reason='fix: [critical] *bug* in `lambda_handler` (>95%)')
        assert result.ok is True

    def test_markdown_special_chars_in_source_ok(self):
        """Source with special chars works."""
        result, _ = self._call(source='Bot_v2.0 [prod]')
        assert result.ok is True

    # --- reply_markup (keyboard) ---

    def test_keyboard_passed_to_send(self):
        """reply_markup is passed through to send_message_with_entities."""
        _, captured = self._call()
        assert captured[0]['reply_markup'] is not None
        assert 'inline_keyboard' in captured[0]['reply_markup']

    def test_normal_keyboard_has_approve_reject(self):
        """Normal (non-dangerous) command has Approve + Reject buttons."""
        _, captured = self._call()
        buttons_flat = [
            btn['text']
            for row in captured[0]['reply_markup']['inline_keyboard']
            for btn in row
        ]
        assert any('Approve' in b for b in buttons_flat)
        assert any('Reject' in b for b in buttons_flat)

    def test_dangerous_keyboard_has_confirm_reject(self):
        """Dangerous command has Confirm (not Approve) + Reject buttons."""
        import notifications_execute as _ne
        with patch.object(_ne, 'is_dangerous', return_value=True):
            _, captured = self._call(command='aws s3 rb s3://bucket --force')
        buttons_flat = [
            btn['text']
            for row in captured[0]['reply_markup']['inline_keyboard']
            for btn in row
        ]
        assert any('Confirm' in b for b in buttons_flat)
        assert any('Reject' in b for b in buttons_flat)

    # --- dangerous message content ---

    def test_dangerous_message_shows_warning(self):
        """Dangerous command text contains warning phrase."""
        import notifications_execute as _ne
        with patch.object(_ne, 'is_dangerous', return_value=True):
            _, captured = self._call(command='aws s3 rb s3://bucket --force')
        text = captured[0]['text']
        assert '高危' in text or '⚠️' in text

    # --- timeout formatting ---

    def test_timeout_seconds_in_text(self):
        """Short timeout (<60s) shows 秒 unit."""
        _, captured = self._call(request_id='t1', reason='r', timeout=30)
        assert '秒' in captured[0]['text']

    def test_timeout_minutes_in_text(self):
        """Timeout >=60s shows 分鐘."""
        _, captured = self._call(request_id='t2', reason='r', timeout=300)
        assert '分鐘' in captured[0]['text']

    def test_timeout_hours_in_text(self):
        """Timeout >=3600s shows 小時."""
        _, captured = self._call(request_id='t3', reason='r', timeout=7200)
        assert '小時' in captured[0]['text']

    # --- NotificationResult ---

    def test_returns_notification_result_ok_true(self):
        """Returns NotificationResult(ok=True) on success."""
        result, _ = self._call()
        assert result.ok is True
        assert result.message_id == 42

    def test_returns_notification_result_ok_false_on_failure(self):
        """Returns NotificationResult(ok=False) when API fails."""
        def fake_send(text, entities, reply_markup=None, silent=False):
            return {'ok': False}

        with patch.object(self.notif._telegram, 'send_message_with_entities', side_effect=fake_send):
            result = self.notif.send_approval_request(
                request_id='fail-req',
                command='aws s3 ls',
                reason='test',
            )
        assert result.ok is False
        assert result.message_id is None

    # --- long command truncation ---

    def test_long_command_truncated_in_text(self):
        """Commands > 500 chars are truncated."""
        long_cmd = 'aws s3 cp ' + 'x' * 600
        _, captured = self._call(command=long_cmd)
        assert '...' in captured[0]['text']

    # --- template scan ---

    def test_template_scan_hit_in_text(self):
        """Template scan hits appear in the text."""
        scan = {
            'hit_count': 2,
            'severity': 'high',
            'max_score': 80,
            'escalate': False,
            'factors': [{'details': 'risky_param'}],
        }
        _, captured = self._call(template_scan_result=scan)
        assert 'Template Scan' in captured[0]['text'] or 'HIGH' in captured[0]['text']

    def test_template_scan_many_factors_truncated(self):
        """Template scan with >3 factors shows truncation."""
        scan = {
            'hit_count': 5,
            'severity': 'critical',
            'max_score': 95,
            'escalate': True,
            'factors': [{'details': f'factor_{i}'} for i in range(6)],
        }
        _, captured = self._call(template_scan_result=scan)
        assert '及其他' in captured[0]['text']

    # --- lambda env warning ---

    def test_lambda_env_dangerous_warning_in_text(self):
        """DANGEROUS lambda env update shows warning text."""
        import notifications_execute as _ne
        with patch.object(_ne, 'check_lambda_env_update', return_value=('DANGEROUS', 'Risky env update')), \
             patch.object(_ne, 'is_dangerous', return_value=False):
            _, captured = self._call(command='aws lambda update-function-configuration --environment ...')
        assert 'Risky env update' in captured[0]['text'] or '🔴' in captured[0]['text']

    # --- entity offset sanity ---

    def test_entity_offsets_are_non_negative(self):
        """All entity offsets are >= 0."""
        _, captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['offset'] >= 0

    def test_entity_lengths_are_positive(self):
        """All entity lengths are > 0."""
        _, captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['length'] > 0

    def test_entities_do_not_overlap(self):
        """No two entities overlap (basic sanity)."""
        _, captured = self._call()
        entities = sorted(captured[0]['entities'], key=lambda e: e['offset'])
        for i in range(len(entities) - 1):
            e1, e2 = entities[i], entities[i + 1]
            assert e1['offset'] + e1['length'] <= e2['offset'], \
                f"Entities overlap: {e1} and {e2}"


# ===========================================================================
# send_blocked_notification — entities mode
# ===========================================================================

class TestSendBlockedNotificationEntities:
    """Verify send_blocked_notification uses entities mode."""

    @pytest.fixture(autouse=True)
    def _reload(self):
        self.notif = _make_notifications_module()

    def _call(self, **kwargs):
        captured, ctx = _capture_send_with_entities(self.notif)
        defaults = dict(
            command='aws iam delete-account',
            block_reason='Blocked: dangerous operation',
            source='TestBot',
        )
        defaults.update(kwargs)
        with ctx:
            self.notif.send_blocked_notification(**defaults)
        return captured

    # --- no parse_mode ---

    def test_no_parse_mode_in_payload(self):
        """send_blocked_notification must NOT use parse_mode."""
        captured_payload = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured_payload.update(data)
            return {'ok': True}

        with patch('telegram._telegram_request', side_effect=fake_request):
            self.notif.send_blocked_notification(
                command='aws s3 ls',
                block_reason='blocked',
            )

        assert 'parse_mode' not in captured_payload

    # --- entities present ---

    def test_entities_list_is_non_empty(self):
        """Entities list is non-empty."""
        captured = self._call()
        assert len(captured) == 1
        assert len(captured[0]['entities']) > 0

    def test_entities_contains_bold(self):
        """At least one bold entity."""
        captured = self._call()
        types = [e['type'] for e in captured[0]['entities']]
        assert 'bold' in types

    # --- content ---

    def test_text_contains_command(self):
        """Text contains command."""
        captured = self._call(command='aws iam delete-user --user-name admin')
        assert 'aws iam delete-user' in captured[0]['text']

    def test_text_contains_block_reason(self):
        """Text contains block_reason verbatim."""
        captured = self._call(block_reason='Rate limit exceeded')
        assert 'Rate limit exceeded' in captured[0]['text']

    def test_text_contains_source(self):
        """Text contains source."""
        captured = self._call(source='MyBot')
        assert 'MyBot' in captured[0]['text']

    def test_no_source_shows_unknown(self):
        """Empty source defaults to Unknown."""
        captured = self._call(source='')
        assert 'Unknown' in captured[0]['text']

    def test_long_command_truncated(self):
        """Long command truncated to 100 chars."""
        long_cmd = 'aws s3 cp ' + 'x' * 200
        captured = self._call(command=long_cmd)
        assert '...' in captured[0]['text']

    # --- special characters don't break ---

    def test_markdown_chars_in_reason_ok(self):
        """Reason with Markdown-unsafe chars works."""
        captured = self._call(block_reason='blocked: user_name*123 [admin]')
        assert 'blocked: user_name*123 [admin]' in captured[0]['text']

    def test_markdown_chars_in_source_ok(self):
        """Source with special chars works."""
        captured = self._call(source='Bot_v2.0 [prod]')
        assert 'Bot_v2.0 [prod]' in captured[0]['text']

    # --- sent silently ---

    def test_sent_silently(self):
        """send_blocked_notification is sent silently."""
        captured = self._call()
        assert captured[0]['silent'] is True

    # --- no keyboard ---

    def test_no_keyboard(self):
        """Blocked notification has no inline keyboard."""
        captured = self._call()
        assert captured[0]['reply_markup'] is None

    # --- error handling ---

    def test_exception_is_caught(self):
        """Exception in send is caught — no crash."""
        with patch.object(self.notif._telegram, 'send_message_with_entities',
                          side_effect=RuntimeError('network fail')):
            self.notif.send_blocked_notification(
                command='aws s3 ls',
                block_reason='rate limit',
                source='Bot',
            )

    # --- entity offset sanity ---

    def test_entity_offsets_non_negative(self):
        captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['offset'] >= 0

    def test_entity_lengths_positive(self):
        captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['length'] > 0


# ===========================================================================
# send_account_approval_request — entities mode
# ===========================================================================

class TestSendAccountApprovalRequestEntities:
    """Verify send_account_approval_request uses entities mode."""

    @pytest.fixture(autouse=True)
    def _reload(self):
        self.notif = _make_notifications_module()

    def _call(self, **kwargs):
        captured, ctx = _capture_send_with_entities(self.notif)
        defaults = dict(
            request_id='acc-e1',
            action='add',
            account_id='111222333444',
            name='TestAccount',
            role_arn='arn:aws:iam::111222333444:role/BouncerExecutionRole',
            source='AdminBot',
        )
        defaults.update(kwargs)
        with ctx:
            self.notif.send_account_approval_request(**defaults)
        return captured

    # --- no parse_mode ---

    def test_no_parse_mode_in_payload_add(self):
        """add action must NOT use parse_mode."""
        captured_payload = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured_payload.update(data)
            return {'ok': True}

        with patch('telegram._telegram_request', side_effect=fake_request):
            self.notif.send_account_approval_request(
                request_id='acc-pm-test',
                action='add',
                account_id='111222333444',
                name='Foo',
                role_arn='arn:...',
                source='Bot',
            )

        assert 'parse_mode' not in captured_payload

    def test_no_parse_mode_in_payload_remove(self):
        """remove action must NOT use parse_mode."""
        captured_payload = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured_payload.update(data)
            return {'ok': True}

        with patch('telegram._telegram_request', side_effect=fake_request):
            self.notif.send_account_approval_request(
                request_id='acc-rm-pm',
                action='remove',
                account_id='555666777888',
                name='OldAccount',
                role_arn='',
                source='Bot',
            )

        assert 'parse_mode' not in captured_payload

    # --- entities ---

    def test_entities_non_empty_add(self):
        """entities list non-empty for add action."""
        captured = self._call(action='add')
        assert len(captured[0]['entities']) > 0

    def test_entities_non_empty_remove(self):
        """entities list non-empty for remove action."""
        captured = self._call(action='remove')
        assert len(captured[0]['entities']) > 0

    def test_entities_contains_bold(self):
        """At least one bold entity."""
        captured = self._call()
        types = [e['type'] for e in captured[0]['entities']]
        assert 'bold' in types

    # --- content add ---

    def test_add_text_contains_new_account(self):
        """Add action: text contains '新增'."""
        captured = self._call(action='add')
        assert '新增' in captured[0]['text']

    def test_add_text_contains_account_id(self):
        """Add action: text contains account_id."""
        captured = self._call(action='add', account_id='112233445566')
        assert '112233445566' in captured[0]['text']

    def test_add_text_contains_account_name(self):
        """Add action: text contains account name."""
        captured = self._call(action='add', name='MyNewAccount')
        assert 'MyNewAccount' in captured[0]['text']

    def test_add_text_contains_role_arn(self):
        """Add action: text contains role_arn."""
        captured = self._call(action='add', role_arn='arn:aws:iam::111222333444:role/TestRole')
        assert 'arn:aws:iam::111222333444:role/TestRole' in captured[0]['text']

    def test_add_text_contains_request_id(self):
        """Add action: text contains request_id."""
        captured = self._call(action='add', request_id='acc-req-999')
        assert 'acc-req-999' in captured[0]['text']

    # --- content remove ---

    def test_remove_text_contains_remove(self):
        """Remove action: text contains '移除'."""
        captured = self._call(action='remove')
        assert '移除' in captured[0]['text']

    def test_remove_text_contains_account_id(self):
        """Remove action: text contains account_id."""
        captured = self._call(action='remove', account_id='998877665544')
        assert '998877665544' in captured[0]['text']

    # --- special characters ---

    def test_markdown_chars_in_name_ok(self):
        """Account name with Markdown-unsafe chars works."""
        captured = self._call(name='Account_v2.0 [prod]')
        assert 'Account_v2.0 [prod]' in captured[0]['text']

    def test_markdown_chars_in_source_ok(self):
        """Source with special chars works."""
        captured = self._call(source='AdminBot_v2')
        assert 'AdminBot_v2' in captured[0]['text']

    # --- keyboard ---

    def test_keyboard_has_approve_and_reject(self):
        """Keyboard has Approve and Reject buttons."""
        captured = self._call()
        buttons_flat = [
            btn['text']
            for row in captured[0]['reply_markup']['inline_keyboard']
            for btn in row
        ]
        assert any('Approve' in b for b in buttons_flat)
        assert any('Reject' in b for b in buttons_flat)

    def test_keyboard_callback_has_request_id(self):
        """Keyboard callback_data contains request_id."""
        captured = self._call(request_id='acc-cb-123')
        callbacks = [
            btn['callback_data']
            for row in captured[0]['reply_markup']['inline_keyboard']
            for btn in row
        ]
        assert any('acc-cb-123' in cb for cb in callbacks)

    # --- context ---

    def test_context_included_when_provided(self):
        """Context string appears in the text when provided."""
        captured = self._call(context='sprint5 setup')
        assert 'sprint5 setup' in captured[0]['text']

    # --- entity offset sanity ---

    def test_entity_offsets_non_negative(self):
        captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['offset'] >= 0

    def test_entity_lengths_positive(self):
        captured = self._call()
        for entity in captured[0]['entities']:
            assert entity['length'] > 0
