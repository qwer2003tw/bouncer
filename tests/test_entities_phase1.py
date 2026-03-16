"""
Tests for sprint12-007 Phase 1: build_entities_message + send_message_with_entities

Covers:
  - MessageBuilder: basic bold/code/text/newline
  - ASCII text: offset/length correctness
  - CJK characters (3 bytes UTF-8 but 1 UTF-16 unit): offset correct
  - Emoji (4 bytes UTF-8, 2 UTF-16 surrogate pair units): offset correct
  - Mixed text + bold + code entities
  - date_time entity
  - Empty input -> ("", [])
  - send_telegram_message with entities -> no parse_mode in payload
  - send_telegram_message without entities -> has parse_mode (backward compat)
  - send_message_with_entities: correct API payload shape
  - _utf16_len: ASCII, CJK, emoji
  - build_entities_message functional wrapper
"""
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest


os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('APPROVED_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


# ---------------------------------------------------------------------------
# _utf16_len unit tests
# ---------------------------------------------------------------------------

class TestUtf16Len:
    """Tests for _utf16_len() helper function."""

    def test_empty_string(self):
        from telegram_entities import _utf16_len
        assert _utf16_len('') == 0

    def test_ascii_single_char(self):
        from telegram_entities import _utf16_len
        assert _utf16_len('A') == 1

    def test_ascii_word(self):
        from telegram_entities import _utf16_len
        assert _utf16_len('hello') == 5

    def test_ascii_with_space(self):
        from telegram_entities import _utf16_len
        assert _utf16_len('hello world') == 11

    def test_cjk_single_char(self):
        """CJK character: U+4E2D (中) is in BMP -> 1 UTF-16 unit."""
        from telegram_entities import _utf16_len
        assert _utf16_len('中') == 1

    def test_cjk_three_chars(self):
        """Three CJK characters -> 3 UTF-16 units."""
        from telegram_entities import _utf16_len
        assert _utf16_len('中文字') == 3

    def test_emoji_single(self):
        """Emoji U+1F512 (🔐) is above BMP -> 2 UTF-16 surrogate pair units."""
        from telegram_entities import _utf16_len
        assert _utf16_len('🔐') == 2

    def test_emoji_fire(self):
        """🔥 U+1F525 -> 2 UTF-16 units."""
        from telegram_entities import _utf16_len
        assert _utf16_len('🔥') == 2

    def test_mixed_ascii_and_emoji(self):
        """'Hi 🔐' -> 3 (Hi + space) + 2 (emoji) = 5 UTF-16 units."""
        from telegram_entities import _utf16_len
        assert _utf16_len('Hi 🔐') == 5  # H(1) + i(1) + space(1) + emoji(2)

    def test_mixed_cjk_and_emoji(self):
        """'中🔐' -> 1 (CJK) + 2 (emoji) = 3."""
        from telegram_entities import _utf16_len
        assert _utf16_len('中🔐') == 3

    def test_newline(self):
        """Newline is 1 UTF-16 unit."""
        from telegram_entities import _utf16_len
        assert _utf16_len('\n') == 1

    def test_multiple_emoji(self):
        """Two emoji -> 4 UTF-16 units."""
        from telegram_entities import _utf16_len
        assert _utf16_len('🔐🔥') == 4


# ---------------------------------------------------------------------------
# MessageBuilder: build() output correctness
# ---------------------------------------------------------------------------

class TestMessageBuilderBuild:
    """Tests for MessageBuilder.build() text and entity output."""

    def test_empty_builder_returns_empty(self):
        """Empty builder -> ('', [])."""
        from telegram_entities import MessageBuilder
        builder = MessageBuilder()
        text, entities = builder.build()
        assert text == ''
        assert entities == []

    def test_plain_text_only(self):
        """Plain text only -> no entities."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().text('hello world').build()
        assert text == 'hello world'
        assert entities == []

    def test_single_bold(self):
        """Single bold segment -> correct entity at offset=0."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().bold('hello').build()
        assert text == 'hello'
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 0, 'length': 5}

    def test_single_code(self):
        """Single code segment -> entity at offset=0."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().code('aws s3 ls').build()
        assert text == 'aws s3 ls'
        assert len(entities) == 1
        assert entities[0] == {'type': 'code', 'offset': 0, 'length': 9}

    def test_text_then_bold(self):
        """Plain text 'a' then bold 'b' -> bold offset=1."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().text('a').bold('b').build()
        assert text == 'ab'
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 1, 'length': 1}

    def test_bold_then_text(self):
        """Bold 'hello' then plain ' world' -> bold at offset=0."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().bold('hello').text(' world').build()
        assert text == 'hello world'
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 0, 'length': 5}

    def test_multiple_entities(self):
        """'Label: ' + bold('value') + ' more' -> 1 entity."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .text('Label: ')
            .bold('value')
            .text(' more')
            .build()
        )
        assert text == 'Label: value more'
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 7, 'length': 5}

    def test_bold_and_code_separate(self):
        """Bold + text + code -> 2 entities with correct offsets."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .bold('Title')    # offset=0, len=5
            .text('\n')       # offset=5, len=1 (no entity)
            .code('cmd')      # offset=6, len=3
            .build()
        )
        assert text == 'Title\ncmd'
        assert len(entities) == 2
        bold_ent = next(e for e in entities if e['type'] == 'bold')
        code_ent = next(e for e in entities if e['type'] == 'code')
        assert bold_ent == {'type': 'bold', 'offset': 0, 'length': 5}
        assert code_ent == {'type': 'code', 'offset': 6, 'length': 3}

    def test_newline_counts_in_offset(self):
        """Newline advances offset by 1."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .text('line1')   # len=5
            .newline()       # len=1
            .bold('line2')   # offset=6
            .build()
        )
        assert text == 'line1\nline2'
        assert entities[0] == {'type': 'bold', 'offset': 6, 'length': 5}

    def test_date_time_entity(self):
        """date_time entity is included with correct type."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().date_time('2026-03-05 14:00').build()
        assert text == '2026-03-05 14:00'
        assert len(entities) == 1
        assert entities[0]['type'] == 'date_time'
        assert entities[0]['offset'] == 0
        assert entities[0]['length'] == 16

    def test_italic_entity(self):
        """Italic entity has correct type."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().italic('note').build()
        assert len(entities) == 1
        assert entities[0]['type'] == 'italic'

    def test_pre_entity(self):
        """Pre entity has correct type."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().pre('block\ntext').build()
        assert len(entities) == 1
        assert entities[0]['type'] == 'pre'
        assert entities[0]['length'] == 10


# ---------------------------------------------------------------------------
# CJK offset tests
# ---------------------------------------------------------------------------

class TestCJKOffsets:
    """CJK characters are 1 UTF-16 unit; ensure offsets are calculated correctly."""

    def test_cjk_prefix_offsets_bold(self):
        """'中文字' (3 UTF-16 units) + bold('X') -> bold at offset=3."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().text('中文字').bold('X').build()
        assert text == '中文字X'
        assert entities[0] == {'type': 'bold', 'offset': 3, 'length': 1}

    def test_cjk_in_bold(self):
        """Bold CJK text: length should be the UTF-16 count."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().bold('中文').build()
        assert text == '中文'
        assert entities[0]['length'] == 2  # 2 CJK chars = 2 UTF-16 units

    def test_mixed_ascii_cjk_offset(self):
        """'abc' (3) + '中文' (2) + bold('end') -> bold at offset=5."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .text('abc')   # 3 units
            .text('中文')  # 2 units -> cumulative 5
            .bold('end')   # offset=5
            .build()
        )
        assert text == 'abc中文end'
        assert entities[0] == {'type': 'bold', 'offset': 5, 'length': 3}


# ---------------------------------------------------------------------------
# Emoji offset tests
# ---------------------------------------------------------------------------

class TestEmojiOffsets:
    """Emoji above U+FFFF take 2 UTF-16 units (surrogate pair)."""

    def test_emoji_prefix_offsets_bold(self):
        """'🔐 ' (emoji=2 + space=1 = 3 units) + bold('secure') -> bold at offset=3."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().text('🔐 ').bold('secure').build()
        assert text == '🔐 secure'
        assert entities[0] == {'type': 'bold', 'offset': 3, 'length': 6}

    def test_emoji_in_bold_length(self):
        """Bold '🔐🔥': two emoji = 4 UTF-16 units."""
        from telegram_entities import MessageBuilder
        text, entities = MessageBuilder().bold('🔐🔥').build()
        assert entities[0] == {'type': 'bold', 'offset': 0, 'length': 4}

    def test_emoji_after_ascii_and_cjk(self):
        """'ab' (2) + '中' (1) + '🔐' (2) + bold('!') -> bold at offset=5."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .text('ab')   # 2
            .text('中')   # 1 -> 3
            .text('🔐')  # 2 -> 5
            .bold('!')    # offset=5, len=1
            .build()
        )
        assert entities[0] == {'type': 'bold', 'offset': 5, 'length': 1}

    def test_multiple_emoji_offset(self):
        """Three emoji before bold: 3*2=6 units offset."""
        from telegram_entities import MessageBuilder
        text, entities = (
            MessageBuilder()
            .text('🔐🔥✅')  # 3 emoji * 2 = 6 units
            .bold('after')
            .build()
        )
        # ✅ U+2705 is in BMP -> 1 unit; 🔐 and 🔥 are 2 units each
        # Total: 2 + 2 + 1 = 5
        assert entities[0]['offset'] == 5


# ---------------------------------------------------------------------------
# build_entities_message functional wrapper
# ---------------------------------------------------------------------------

class TestBuildEntitiesMessage:
    """Tests for the functional build_entities_message() wrapper."""

    def test_basic_bold(self):
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([('hello', 'bold')])
        assert text == 'hello'
        assert entities == [{'type': 'bold', 'offset': 0, 'length': 5}]

    def test_plain_text(self):
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([('plain', None)])
        assert text == 'plain'
        assert entities == []

    def test_mixed_parts(self):
        """Text + bold -> bold at correct offset."""
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([
            ('Status: ', None),
            ('approved', 'bold'),
        ])
        assert text == 'Status: approved'
        assert entities == [{'type': 'bold', 'offset': 8, 'length': 8}]

    def test_empty_parts(self):
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([])
        assert text == ''
        assert entities == []

    def test_numeric_content_converted_to_str(self):
        """Non-string content is converted to str."""
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([(42, 'code')])
        assert text == '42'
        assert entities == [{'type': 'code', 'offset': 0, 'length': 2}]

    def test_code_entity(self):
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([
            ('Command: ', None),
            ('aws s3 ls', 'code'),
        ])
        assert text == 'Command: aws s3 ls'
        assert len(entities) == 1
        assert entities[0] == {'type': 'code', 'offset': 9, 'length': 9}

    def test_date_time_entity(self):
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([('2026-03-05', 'date_time')])
        assert entities[0]['type'] == 'date_time'
        assert entities[0]['length'] == 10

    def test_cjk_offset(self):
        """CJK prefix (3 units) -> code offset=3."""
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([
            ('中文字', None),
            ('X', 'code'),
        ])
        assert entities[0] == {'type': 'code', 'offset': 3, 'length': 1}

    def test_emoji_offset(self):
        """Emoji prefix (2 units) -> bold offset=2."""
        from telegram_entities import build_entities_message
        text, entities = build_entities_message([
            ('🔐', None),
            ('hi', 'bold'),
        ])
        assert entities[0] == {'type': 'bold', 'offset': 2, 'length': 2}


# ---------------------------------------------------------------------------
# send_message_with_entities API payload tests
# ---------------------------------------------------------------------------

class TestSendMessageWithEntities:
    """Tests for send_message_with_entities() Telegram API call."""

    def _captured_call(self, text, entities, **kwargs):
        """Call send_message_with_entities and capture the data sent to _telegram_request."""
        captured = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured['method'] = method
            captured['data'] = data
            captured['json_body'] = json_body
            return {'ok': True}

        with patch('telegram._telegram_request', side_effect=fake_request):
            from telegram import send_message_with_entities
            send_message_with_entities(text, entities, **kwargs)

        return captured

    def test_entities_payload_no_parse_mode(self):
        """Payload must NOT contain parse_mode when using entities."""
        captured = self._captured_call('Hello', [{'type': 'bold', 'offset': 0, 'length': 5}])
        assert 'parse_mode' not in captured['data']

    def test_entities_payload_has_entities(self):
        """Payload must contain entities list."""
        entities = [{'type': 'bold', 'offset': 0, 'length': 5}]
        captured = self._captured_call('Hello', entities)
        assert captured['data']['entities'] == entities

    def test_entities_payload_has_text(self):
        """Payload must contain the text."""
        captured = self._captured_call('Test message', [])
        assert captured['data']['text'] == 'Test message'

    def test_entities_payload_uses_json_body(self):
        """entities mode must use json_body=True (needed for list values)."""
        captured = self._captured_call('Hello', [])
        assert captured['json_body'] is True

    def test_silent_flag(self):
        """silent=True -> disable_notification=True in payload."""
        captured = self._captured_call('Hello', [], silent=True)
        assert captured['data'].get('disable_notification') is True

    def test_silent_false_not_in_payload(self):
        """silent=False -> disable_notification not in payload."""
        captured = self._captured_call('Hello', [], silent=False)
        assert 'disable_notification' not in captured['data']

    def test_reply_markup_included(self):
        """reply_markup is passed through to payload."""
        markup = {'inline_keyboard': [[{'text': 'OK', 'callback_data': 'ok'}]]}
        captured = self._captured_call('Hello', [], reply_markup=markup)
        assert 'reply_markup' in captured['data']

    def test_empty_entities_list(self):
        """Empty entities list is valid (plain text via entities mode)."""
        captured = self._captured_call('Plain text', [])
        assert captured['data']['entities'] == []
        assert 'parse_mode' not in captured['data']

    def test_method_is_sendmessage(self):
        """Always calls sendMessage method."""
        captured = self._captured_call('Hello', [])
        assert captured['method'] == 'sendMessage'

    def test_chat_id_is_set(self):
        """chat_id is set from APPROVED_CHAT_ID."""
        captured = self._captured_call('Hello', [])
        assert 'chat_id' in captured['data']


# ---------------------------------------------------------------------------
# send_telegram_message backward compatibility
# ---------------------------------------------------------------------------

class TestSendTelegramMessageBackwardCompat:
    """Ensure existing send_telegram_message still uses parse_mode (backward compat)."""

    def test_standard_call_has_parse_mode(self):
        """send_telegram_message without entities -> has parse_mode."""
        captured = {}

        def fake_request(method, data, timeout=5, json_body=False):
            captured['data'] = data
            return {'ok': True}

        with patch('telegram._telegram_request', side_effect=fake_request):
            from telegram import send_telegram_message
            send_telegram_message('Hello *world*')

        assert 'parse_mode' in captured['data']
        assert captured['data']['parse_mode'] == 'Markdown'
