"""
Tests for S34-004: format_command_output() with expandable_blockquote

Covers:
  - Short output (< threshold lines) -> pre entity
  - Long output (> threshold lines) -> expandable_blockquote entity
  - Empty/whitespace output -> "(no output)" with no entity
  - Custom threshold parameter
  - UTF-16 length calculation correctness
  - MessageBuilder.expandable_blockquote() method
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '-1234567890')
os.environ.setdefault('APPROVED_CHAT_ID', '-1234567890')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


class TestFormatCommandOutput:
    """Tests for format_command_output() function."""

    def test_empty_output(self):
        """Empty output should return no entities and '(no output)' text."""
        from telegram_entities import format_command_output

        entities, text = format_command_output('')
        assert entities == []
        assert text == "(no output)"

    def test_whitespace_only_output(self):
        """Whitespace-only output should be treated as empty."""
        from telegram_entities import format_command_output

        entities, text = format_command_output('   \n\n  ')
        assert entities == []
        assert text == "(no output)"

    def test_short_output_uses_pre(self):
        """Output with fewer lines than threshold should use 'pre' entity."""
        from telegram_entities import format_command_output

        # 10 lines (well below default threshold of 50)
        result = '\n'.join([f'Line {i}' for i in range(10)])
        entities, text = format_command_output(result)

        assert len(entities) == 1
        assert entities[0]['type'] == 'pre'
        assert entities[0]['offset'] == 0
        assert text == result

    def test_long_output_uses_expandable_blockquote(self):
        """Output with more lines than threshold should use 'expandable_blockquote' entity."""
        from telegram_entities import format_command_output

        # 60 lines (above default threshold of 50)
        result = '\n'.join([f'Line {i}' for i in range(60)])
        entities, text = format_command_output(result)

        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'
        assert entities[0]['offset'] == 0
        assert text == result

    def test_exactly_at_threshold(self):
        """Output exactly at threshold should use 'pre' entity (not exceeding)."""
        from telegram_entities import format_command_output

        # Exactly 50 lines (at default threshold)
        result = '\n'.join([f'Line {i}' for i in range(50)])
        entities, text = format_command_output(result)

        assert len(entities) == 1
        assert entities[0]['type'] == 'pre'
        assert entities[0]['offset'] == 0

    def test_one_over_threshold(self):
        """Output one line over threshold should use 'expandable_blockquote' entity."""
        from telegram_entities import format_command_output

        # 51 lines (one over default threshold)
        result = '\n'.join([f'Line {i}' for i in range(51)])
        entities, text = format_command_output(result)

        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'

    def test_custom_threshold(self):
        """Custom threshold parameter should be respected."""
        from telegram_entities import format_command_output

        # 15 lines with threshold=10
        result = '\n'.join([f'Line {i}' for i in range(15)])
        entities, text = format_command_output(result, threshold=10)

        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'

        # Same input with threshold=20 should use pre
        entities2, text2 = format_command_output(result, threshold=20)
        assert entities2[0]['type'] == 'pre'

    def test_utf16_length_ascii(self):
        """UTF-16 length should be correct for ASCII text."""
        from telegram_entities import format_command_output, _utf16_len

        result = 'Hello World'
        entities, text = format_command_output(result)

        assert entities[0]['length'] == len(result)
        assert entities[0]['length'] == _utf16_len(result)

    def test_utf16_length_with_emoji(self):
        """UTF-16 length should be correct for text with emoji (surrogate pairs)."""
        from telegram_entities import format_command_output, _utf16_len

        # Emoji characters count as 2 UTF-16 units each
        result = 'Success! 🔥🚀'
        entities, text = format_command_output(result)

        # 'Success! ' = 9, '🔥' = 2, '🚀' = 2 -> total = 13 UTF-16 units
        expected_len = 9 + 2 + 2
        assert entities[0]['length'] == expected_len
        assert entities[0]['length'] == _utf16_len(result)

    def test_utf16_length_with_cjk(self):
        """UTF-16 length should be correct for CJK characters."""
        from telegram_entities import format_command_output, _utf16_len

        # CJK characters are 1 UTF-16 unit each
        result = '執行成功'
        entities, text = format_command_output(result)

        assert entities[0]['length'] == 4
        assert entities[0]['length'] == _utf16_len(result)

    def test_multiline_output_with_special_chars(self):
        """Long output with special characters should be handled correctly."""
        from telegram_entities import format_command_output

        lines = [
            'aws s3 ls',
            '2024-01-01 bucket-name',
            '🔐 Secret: hidden',
            '中文輸出',
            'Error: ❌ Failed',
        ] * 15  # 75 lines total

        result = '\n'.join(lines)
        entities, text = format_command_output(result)

        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'
        assert text == result

    def test_strips_leading_trailing_whitespace(self):
        """Leading/trailing whitespace should be stripped before processing."""
        from telegram_entities import format_command_output

        result = '\n\n  Line 1\nLine 2  \n\n'
        entities, text = format_command_output(result)

        assert text == 'Line 1\nLine 2'
        assert not text.startswith('\n')
        assert not text.endswith('\n')


class TestMessageBuilderExpandableBlockquote:
    """Tests for MessageBuilder.expandable_blockquote() method."""

    def test_expandable_blockquote_method(self):
        """MessageBuilder should support expandable_blockquote() method."""
        from telegram_entities import MessageBuilder

        mb = MessageBuilder()
        mb.text("Result:").newline()
        mb.expandable_blockquote("Long output here...")
        text, entities = mb.build()

        assert "Long output here..." in text
        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'

    def test_expandable_blockquote_offset_calculation(self):
        """Expandable blockquote offset should be calculated correctly."""
        from telegram_entities import MessageBuilder

        mb = MessageBuilder()
        mb.text("Header: ")
        mb.expandable_blockquote("Content")
        text, entities = mb.build()

        assert entities[0]['type'] == 'expandable_blockquote'
        assert entities[0]['offset'] == len("Header: ")
        assert entities[0]['length'] == len("Content")

    def test_expandable_blockquote_with_emoji(self):
        """Expandable blockquote with emoji should calculate UTF-16 offsets correctly."""
        from telegram_entities import MessageBuilder, _utf16_len

        mb = MessageBuilder()
        mb.text("🔥 ")  # 2 UTF-16 units + space
        mb.expandable_blockquote("Output")
        text, entities = mb.build()

        assert entities[0]['type'] == 'expandable_blockquote'
        assert entities[0]['offset'] == _utf16_len("🔥 ")  # Should be 3
        assert entities[0]['length'] == 6  # "Output"

    def test_multiple_entities_with_expandable_blockquote(self):
        """Multiple entities including expandable_blockquote should work together."""
        from telegram_entities import MessageBuilder

        mb = MessageBuilder()
        mb.bold("Command:").newline()
        mb.code("aws s3 ls").newline()
        mb.bold("Result:").newline()
        mb.expandable_blockquote("Long output...")
        text, entities = mb.build()

        assert len(entities) == 4
        assert entities[0]['type'] == 'bold'
        assert entities[1]['type'] == 'code'
        assert entities[2]['type'] == 'bold'
        assert entities[3]['type'] == 'expandable_blockquote'
