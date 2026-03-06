"""
Sprint 10-004: Button UX regression tests
Validates: English text, style field, valid style values, backward-compatible callback_data
"""
import re
import importlib
import pytest
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

VALID_STYLES = {'success', 'primary', 'danger'}
CHINESE = re.compile(r'[\u4e00-\u9fff]')

@pytest.fixture(autouse=True)
def fresh_notifications():
    """Reload notifications before each test to prevent mock pollution from other test modules."""
    import notifications as notif_mod
    importlib.reload(notif_mod)
    # Re-inject into sys.modules and local scope
    sys.modules['notifications'] = notif_mod
    yield notif_mod

def extract_buttons(keyboard):
    if not keyboard:
        return []
    return [btn for row in keyboard.get('inline_keyboard', []) for btn in row]

def get_keyboard(mock_send):
    if not mock_send.call_args:
        return None
    kwargs = mock_send.call_args[1] if mock_send.call_args[1] else {}
    # send_message_with_entities uses reply_markup kwarg
    if 'reply_markup' in kwargs:
        return kwargs['reply_markup']
    args = mock_send.call_args[0]
    return args[1] if len(args) >= 2 else kwargs.get('keyboard')

def check_buttons(buttons):
    assert buttons, "No buttons found"
    for btn in buttons:
        assert 'style' in btn, f"Missing style: {btn}"
        assert btn['style'] in VALID_STYLES, f"Invalid style '{btn['style']}': {btn}"
        assert not CHINESE.search(btn.get('text', '')), f"Chinese in button: {btn['text']}"


@pytest.fixture(autouse=True)
def _mock_entities_send():
    """Ensure send_message_with_entities is mocked for pre-entities tests."""
    import sys, importlib
    import telegram as _tg
    from unittest.mock import MagicMock

    mock_msg_id = 99999
    mock_response = {'ok': True, 'result': {'message_id': mock_msg_id}}

    # Save originals
    orig_entities = getattr(_tg, 'send_message_with_entities', None)

    # Replace only send_message_with_entities (entities Phase 2 migration)
    mock_entities = MagicMock(return_value=mock_response)
    _tg.send_message_with_entities = mock_entities

    # Reload notifications so it picks up the mocks
    if 'notifications' in sys.modules:
        importlib.reload(sys.modules['notifications'])

    yield mock_entities

    # Restore
    if orig_entities is not None:
        _tg.send_message_with_entities = orig_entities
    elif hasattr(_tg, 'send_message_with_entities'):
        delattr(_tg, 'send_message_with_entities')


class TestApprovalRequestButtons:
    def test_normal_approve_buttons(self, fresh_notifications):
        with patch('telegram.send_message_with_entities') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_approval_request('req-001', 'aws s3 ls', 'test', source='test')
            kb = get_keyboard(m)
            check_buttons(extract_buttons(kb))

    def test_approve_button_style_success(self, fresh_notifications):
        with patch('telegram.send_message_with_entities') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_approval_request('req-002', 'aws s3 ls', 'test', source='test')
            kb = get_keyboard(m)
            btns = extract_buttons(kb)
            approve = [b for b in btns if 'Approve' in b.get('text', '')]
            assert approve, f"No Approve button, got: {[b['text'] for b in btns]}"
            assert all(b['style'] == 'success' for b in approve)

    def test_reject_button_style_danger(self, fresh_notifications):
        with patch('telegram.send_message_with_entities') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_approval_request('req-003', 'aws s3 ls', 'test', source='test')
            kb = get_keyboard(m)
            btns = extract_buttons(kb)
            reject = [b for b in btns if 'Reject' in b.get('text', '')]
            assert reject, f"No Reject button, got: {[b['text'] for b in btns]}"
            assert all(b['style'] == 'danger' for b in reject)

    def test_callback_data_contains_request_id(self, fresh_notifications):
        with patch('telegram.send_message_with_entities') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_approval_request('req-abc-123', 'aws s3 ls', 'test', source='test')
            kb = get_keyboard(m)
            cds = [b.get('callback_data','') for b in extract_buttons(kb)]
            assert any('req-abc-123' in cd for cd in cds), f"request_id not in callbacks: {cds}"

class TestGrantButtons:
    def test_grant_buttons(self, fresh_notifications):
        with patch.object(fresh_notifications, '_send_message') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_grant_request_notification(
                grant_id='g-001',
                commands_detail=[{'command': 'aws s3 ls', 'risk': 'low', 'blocked': False}],
                reason='test', source='test', account_id='123',
                ttl_minutes=30, allow_repeat=True
            )
            kb = get_keyboard(m)
            check_buttons(extract_buttons(kb))

class TestStyleValues:
    def test_no_invalid_style_values(self, fresh_notifications):
        with patch.object(fresh_notifications, '_send_message') as m:
            m.return_value = {'ok': True, 'result': {'message_id': 1}}
            fresh_notifications.send_approval_request('req-x', 'aws ec2 describe-instances', 'test', source='test')
            kb = get_keyboard(m)
            for btn in extract_buttons(kb):
                if 'style' in btn:
                    assert btn['style'] in VALID_STYLES, f"Invalid style: {btn}"
