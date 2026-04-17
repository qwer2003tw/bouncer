"""
Sprint 14 #60: Regression test — style field preserved in button whitelist.

Verifies that _strip_unsupported_button_fields() preserves 'style' (supported
since Telegram Bot API 9.4) while still removing unknown fields.
"""
import os
import pytest



@pytest.fixture
def telegram_mod():
    import importlib
    import telegram
    importlib.reload(telegram)
    return telegram


class TestButtonStylePreserved:
    """#60: _strip_unsupported_button_fields preserves 'style' field."""

    def test_style_is_preserved(self, telegram_mod):
        """'style' must NOT be stripped — Telegram Bot API 9.4 supports it."""
        keyboard = {
            'inline_keyboard': [[
                {'text': 'Approve', 'callback_data': 'approve', 'style': 'success'},
                {'text': 'Reject', 'callback_data': 'reject', 'style': 'danger'},
            ]]
        }
        result = telegram_mod._strip_unsupported_button_fields(keyboard)
        btns = result['inline_keyboard'][0]
        assert btns[0]['style'] == 'success', "style should be preserved on Approve button"
        assert btns[1]['style'] == 'danger', "style should be preserved on Reject button"

    def test_known_fields_all_preserved(self, telegram_mod):
        """All known Telegram fields should pass through unmodified."""
        keyboard = {
            'inline_keyboard': [[
                {
                    'text': 'Click',
                    'callback_data': 'cb_data',
                    'url': 'https://example.com',
                    'style': 'primary',
                }
            ]]
        }
        result = telegram_mod._strip_unsupported_button_fields(keyboard)
        btn = result['inline_keyboard'][0][0]
        assert btn['text'] == 'Click'
        assert btn['callback_data'] == 'cb_data'
        assert btn['url'] == 'https://example.com'
        assert btn['style'] == 'primary'

    def test_unknown_fields_are_stripped(self, telegram_mod):
        """Fields NOT in the whitelist should be removed."""
        keyboard = {
            'inline_keyboard': [[
                {
                    'text': 'Btn',
                    'callback_data': 'x',
                    'style': 'success',
                    'unknown_field': 'should_be_removed',
                    'another_unknown': 42,
                }
            ]]
        }
        result = telegram_mod._strip_unsupported_button_fields(keyboard)
        btn = result['inline_keyboard'][0][0]
        assert 'unknown_field' not in btn
        assert 'another_unknown' not in btn
        assert btn['style'] == 'success'
        assert btn['text'] == 'Btn'
        assert btn['callback_data'] == 'x'

    def test_empty_keyboard_returns_unchanged(self, telegram_mod):
        """None / empty keyboard should be returned as-is."""
        assert telegram_mod._strip_unsupported_button_fields(None) is None
        assert telegram_mod._strip_unsupported_button_fields({}) == {}

    def test_multi_row_keyboard(self, telegram_mod):
        """Multi-row keyboards should all be processed correctly."""
        keyboard = {
            'inline_keyboard': [
                [{'text': 'R1B1', 'callback_data': 'r1b1', 'style': 'success'}],
                [{'text': 'R2B1', 'callback_data': 'r2b1', 'style': 'danger'}],
            ]
        }
        result = telegram_mod._strip_unsupported_button_fields(keyboard)
        assert result['inline_keyboard'][0][0]['style'] == 'success'
        assert result['inline_keyboard'][1][0]['style'] == 'danger'

    def test_button_without_style_is_unchanged(self, telegram_mod):
        """Buttons that never had 'style' should still work normally."""
        keyboard = {
            'inline_keyboard': [[
                {'text': 'No Style', 'callback_data': 'ns'}
            ]]
        }
        result = telegram_mod._strip_unsupported_button_fields(keyboard)
        btn = result['inline_keyboard'][0][0]
        assert 'style' not in btn
        assert btn['text'] == 'No Style'
        assert btn['callback_data'] == 'ns'

    def test_known_button_fields_constant_includes_style(self, telegram_mod):
        """KNOWN_BUTTON_FIELDS constant must include 'style'."""
        assert 'style' in telegram_mod.KNOWN_BUTTON_FIELDS
