"""
Tests for sprint11-008: telegram.py json_body=True + style field strip

Verifies:
1. _strip_unsupported_button_fields removes 'style' from inline_keyboard buttons
2. send_telegram_message uses json_body=True (no double-encode of reply_markup)
3. send_telegram_message_silent uses json_body=True
4. update_message uses json_body=True
"""
import sys
import os
import unittest
from unittest.mock import patch

# Set up env + path before importing telegram
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import telegram as _telegram_module  # noqa: E402


class TestStripUnsupportedButtonFields(unittest.TestCase):
    """Test _strip_unsupported_button_fields helper."""

    def test_removes_style_field(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': 'Approve', 'callback_data': 'approve:1', 'style': 'primary'},
                    {'text': 'Deny', 'callback_data': 'deny:1', 'style': 'danger'},
                ]
            ]
        }
        result = strip(keyboard)
        btn1 = result['inline_keyboard'][0][0]
        btn2 = result['inline_keyboard'][0][1]
        self.assertNotIn('style', btn1)
        self.assertNotIn('style', btn2)
        self.assertEqual(btn1['text'], 'Approve')
        self.assertEqual(btn1['callback_data'], 'approve:1')

    def test_preserves_other_fields(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {
            'inline_keyboard': [
                [{'text': 'Click', 'callback_data': 'foo', 'url': 'https://x.com', 'style': 'info'}]
            ]
        }
        result = strip(keyboard)
        btn = result['inline_keyboard'][0][0]
        self.assertNotIn('style', btn)
        self.assertEqual(btn['url'], 'https://x.com')
        self.assertEqual(btn['callback_data'], 'foo')

    def test_no_style_field_unchanged(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {
            'inline_keyboard': [
                [{'text': 'OK', 'callback_data': 'ok'}]
            ]
        }
        result = strip(keyboard)
        self.assertEqual(result['inline_keyboard'][0][0], {'text': 'OK', 'callback_data': 'ok'})

    def test_none_keyboard_returns_none(self):
        strip = _telegram_module._strip_unsupported_button_fields
        self.assertIsNone(strip(None))

    def test_empty_dict_returns_empty(self):
        strip = _telegram_module._strip_unsupported_button_fields
        result = strip({})
        self.assertEqual(result, {})

    def test_no_inline_keyboard_key(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {'force_reply': True}
        result = strip(keyboard)
        self.assertEqual(result, {'force_reply': True})

    def test_multiple_rows(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {
            'inline_keyboard': [
                [{'text': 'A', 'callback_data': 'a', 'style': 'x'}],
                [{'text': 'B', 'callback_data': 'b', 'style': 'y'},
                 {'text': 'C', 'callback_data': 'c'}],
            ]
        }
        result = strip(keyboard)
        for row in result['inline_keyboard']:
            for btn in row:
                self.assertNotIn('style', btn)

    def test_does_not_mutate_original(self):
        strip = _telegram_module._strip_unsupported_button_fields
        keyboard = {
            'inline_keyboard': [
                [{'text': 'X', 'callback_data': 'x', 'style': 'primary'}]
            ]
        }
        strip(keyboard)
        # Original should be unchanged
        self.assertEqual(keyboard['inline_keyboard'][0][0]['style'], 'primary')


class TestSendTelegramMessageJsonBody(unittest.TestCase):
    """Test that send functions use json_body=True."""

    @patch('telegram._telegram_request')
    def test_send_telegram_message_uses_json_body(self, mock_req):
        mock_req.return_value = {'ok': True}
        _telegram_module.send_telegram_message('Hello')
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        # Signature: _telegram_request(method, data, timeout=5, json_body=False)
        if len(args) >= 4:
            self.assertTrue(args[3], "json_body should be True")
        else:
            self.assertTrue(kwargs.get('json_body', False), "json_body should be True")

    @patch('telegram._telegram_request')
    def test_send_telegram_message_reply_markup_not_json_string(self, mock_req):
        """reply_markup should be dict, not a JSON string."""
        mock_req.return_value = {'ok': True}
        keyboard = {
            'inline_keyboard': [[{'text': 'OK', 'callback_data': 'ok', 'style': 'primary'}]]
        }
        _telegram_module.send_telegram_message('Hello', reply_markup=keyboard)
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        data = args[1]
        self.assertIn('reply_markup', data)
        self.assertIsInstance(data['reply_markup'], dict,
                              "reply_markup should be dict, not JSON string")
        # style should be stripped
        btn = data['reply_markup']['inline_keyboard'][0][0]
        self.assertNotIn('style', btn)

    @patch('telegram._telegram_request')
    def test_send_telegram_message_silent_uses_json_body(self, mock_req):
        _telegram_module.send_telegram_message_silent('Hello silent')
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        if len(args) >= 4:
            self.assertTrue(args[3], "json_body should be True")
        else:
            self.assertTrue(kwargs.get('json_body', False), "json_body should be True")

    @patch('telegram._telegram_request')
    def test_send_telegram_message_silent_reply_markup_is_dict(self, mock_req):
        keyboard = {
            'inline_keyboard': [[{'text': 'X', 'callback_data': 'x', 'style': 'danger'}]]
        }
        _telegram_module.send_telegram_message_silent('Hello', reply_markup=keyboard)
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        data = args[1]
        self.assertIsInstance(data['reply_markup'], dict)
        self.assertNotIn('style', data['reply_markup']['inline_keyboard'][0][0])

    @patch('telegram._telegram_request')
    def test_update_message_uses_json_body(self, mock_req):
        _telegram_module.update_message(999, 'Updated text', remove_buttons=True)
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        if len(args) >= 4:
            self.assertTrue(args[3], "json_body should be True")
        else:
            self.assertTrue(kwargs.get('json_body', False), "json_body should be True")

    @patch('telegram._telegram_request')
    def test_update_message_remove_buttons_reply_markup_is_dict(self, mock_req):
        _telegram_module.update_message(999, 'Updated text', remove_buttons=True)
        self.assertTrue(mock_req.called)
        args, kwargs = mock_req.call_args
        data = args[1]
        self.assertIn('reply_markup', data)
        self.assertIsInstance(data['reply_markup'], dict,
                              "reply_markup should be dict, not JSON string")
        self.assertEqual(data['reply_markup'], {'inline_keyboard': []})


class TestNoDoubleEncode(unittest.TestCase):
    """Regression: ensure reply_markup with style is not double-encoded."""

    @patch('telegram._telegram_request')
    def test_reply_markup_not_double_encoded(self, mock_req):
        """If reply_markup were json.dumps'd, it would be a string. It must be dict."""
        mock_req.return_value = {'ok': True}

        keyboard = {
            'inline_keyboard': [
                [{'text': 'Approve', 'callback_data': 'approve:abc', 'style': 'success'}]
            ]
        }
        _telegram_module.send_telegram_message('Test', reply_markup=keyboard)
        self.assertTrue(mock_req.called)
        args, _ = mock_req.call_args
        data = args[1]

        # Must be dict, not str
        self.assertIsInstance(data['reply_markup'], dict)
        # Must not contain 'style'
        btn = data['reply_markup']['inline_keyboard'][0][0]
        self.assertNotIn('style', btn)
        self.assertEqual(btn['callback_data'], 'approve:abc')


if __name__ == '__main__':
    unittest.main()
