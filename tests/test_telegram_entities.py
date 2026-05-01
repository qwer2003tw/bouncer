"""Tests for telegram_entities module."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestUtf16Len:
    """Tests for _utf16_len helper function."""

    def test_ascii_characters(self):
        """Test UTF-16 length for ASCII characters (1 unit each)."""
        from telegram_entities import _utf16_len

        assert _utf16_len("hello") == 5
        assert _utf16_len("AWS CLI") == 7
        assert _utf16_len("") == 0

    def test_bmp_characters(self):
        """Test UTF-16 length for BMP characters (1 unit each)."""
        from telegram_entities import _utf16_len

        # Chinese characters in BMP
        assert _utf16_len("你好") == 2
        # European characters
        assert _utf16_len("café") == 4

    def test_emoji_supplementary_plane(self):
        """Test UTF-16 length for emoji (2 units each, surrogate pairs)."""
        from telegram_entities import _utf16_len

        # Single emoji (U+1F600, requires surrogate pair)
        assert _utf16_len("😀") == 2
        # Multiple emoji
        assert _utf16_len("😀😁") == 4
        # Mixed text and emoji
        assert _utf16_len("Hi 😀") == 5  # H(1) + i(1) + space(1) + 😀(2)

    def test_mixed_content(self):
        """Test UTF-16 length for mixed ASCII, BMP, and supplementary plane."""
        from telegram_entities import _utf16_len

        # "Hello 世界 🌍"
        text = "Hello 世界 🌍"
        # H(1) + e(1) + l(1) + l(1) + o(1) + space(1) + 世(1) + 界(1) + space(1) + 🌍(2)
        assert _utf16_len(text) == 11


class TestMessageBuilder:
    """Tests for MessageBuilder class."""

    def test_empty_builder(self):
        """Test building empty message."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        text, entities = builder.build()

        assert text == ""
        assert entities == []

    def test_plain_text_only(self):
        """Test builder with only plain text."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.text("Hello World")
        text, entities = builder.build()

        assert text == "Hello World"
        assert entities == []

    def test_bold_text(self):
        """Test bold entity."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.bold("Important")
        text, entities = builder.build()

        assert text == "Important"
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 0, 'length': 9}

    def test_multiple_entities(self):
        """Test multiple different entities."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.text("Status: ").bold("SUCCESS").text(" - ").code("exit 0")
        text, entities = builder.build()

        assert text == "Status: SUCCESS - exit 0"
        assert len(entities) == 2
        assert entities[0] == {'type': 'bold', 'offset': 8, 'length': 7}  # SUCCESS
        assert entities[1] == {'type': 'code', 'offset': 18, 'length': 6}  # exit 0

    def test_all_entity_types(self):
        """Test all supported entity types."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.bold("bold").newline()
        builder.italic("italic").newline()
        builder.code("code").newline()
        builder.pre("pre").newline()
        builder.underline("underline").newline()
        builder.strikethrough("strike").newline()
        builder.spoiler("spoiler").newline()
        builder.expandable_blockquote("blockquote")

        text, entities = builder.build()

        expected_types = ['bold', 'italic', 'code', 'pre', 'underline', 'strikethrough', 'spoiler', 'expandable_blockquote']
        assert len(entities) == len(expected_types)
        for i, expected_type in enumerate(expected_types):
            assert entities[i]['type'] == expected_type

    def test_newline(self):
        """Test newline handling."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.text("Line 1").newline().text("Line 2").newline(2).text("Line 3")
        text, entities = builder.build()

        assert text == "Line 1\nLine 2\n\nLine 3"
        assert entities == []

    def test_emoji_offset_calculation(self):
        """Test correct offset calculation with emoji (UTF-16 surrogate pairs)."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.text("Hi 😀 ").bold("World")
        text, entities = builder.build()

        # "Hi 😀 " = H(1) + i(1) + space(1) + 😀(2) + space(1) = 6 UTF-16 units
        assert text == "Hi 😀 World"
        assert len(entities) == 1
        assert entities[0] == {'type': 'bold', 'offset': 6, 'length': 5}

    def test_date_time_entity(self):
        """Test date_time entity with unix timestamp."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.date_time("2024-03-16 10:00 UTC", unix_timestamp=1710586800)
        text, entities = builder.build()

        assert text == "2024-03-16 10:00 UTC"
        assert len(entities) == 1
        assert entities[0] == {
            'type': 'date_time',
            'offset': 0,
            'length': 20,
            'unix_time': 1710586800
        }

    def test_date_time_default_timestamp(self):
        """Test date_time entity with default timestamp (0)."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.date_time("Some date")
        text, entities = builder.build()

        assert entities[0]['unix_time'] == 0

    def test_empty_content_no_entity(self):
        """Test that empty formatted content produces no entity."""
        from telegram_entities import MessageBuilder

        builder = MessageBuilder()
        builder.bold("")
        text, entities = builder.build()

        assert text == ""
        assert entities == []

    def test_fluent_api_chaining(self):
        """Test that all methods return self for chaining."""
        from telegram_entities import MessageBuilder

        result = MessageBuilder().text("a").bold("b").code("c").newline()
        assert isinstance(result, MessageBuilder)

    def test_from_parts_class_method(self):
        """Test from_parts factory method."""
        from telegram_entities import MessageBuilder

        parts = [
            ("Header", "bold"),
            ("\n", None),
            ("Body text", None),
            ("\n", None),
            ("footer", "italic")
        ]

        builder = MessageBuilder.from_parts(parts)
        text, entities = builder.build()

        assert text == "Header\nBody text\nfooter"
        assert len(entities) == 2
        assert entities[0]['type'] == 'bold'
        assert entities[1]['type'] == 'italic'


class TestBuildEntitiesMessage:
    """Tests for build_entities_message convenience function."""

    def test_build_entities_message(self):
        """Test functional wrapper around MessageBuilder."""
        from telegram_entities import build_entities_message

        parts = [
            ("Command: ", None),
            ("aws s3 ls", "code"),
            ("\n", None),
            ("Status: ", None),
            ("SUCCESS", "bold")
        ]

        text, entities = build_entities_message(parts)

        assert text == "Command: aws s3 ls\nStatus: SUCCESS"
        assert len(entities) == 2
        assert entities[0] == {'type': 'code', 'offset': 9, 'length': 9}
        assert entities[1] == {'type': 'bold', 'offset': 27, 'length': 7}


class TestFormatCommandOutput:
    """Tests for format_command_output function."""

    def test_empty_output(self):
        """Test formatting empty output."""
        from telegram_entities import format_command_output

        entities, text = format_command_output("")
        assert text == "(no output)"
        assert entities == []

        entities, text = format_command_output("   \n  ")
        assert text == "(no output)"
        assert entities == []

    def test_short_output_uses_pre(self):
        """Test that short output uses pre entity."""
        from telegram_entities import format_command_output

        result = "line1\nline2\nline3"
        entities, text = format_command_output(result, threshold=50)

        assert text == "line1\nline2\nline3"
        assert len(entities) == 1
        assert entities[0]['type'] == 'pre'
        assert entities[0]['offset'] == 0
        assert entities[0]['length'] == len("line1\nline2\nline3")

    def test_long_output_uses_expandable_blockquote(self):
        """Test that long output uses expandable_blockquote entity."""
        from telegram_entities import format_command_output

        # Create output with >50 lines
        result = "\n".join([f"line{i}" for i in range(60)])
        entities, text = format_command_output(result, threshold=50)

        assert len(entities) == 1
        assert entities[0]['type'] == 'expandable_blockquote'
        assert entities[0]['offset'] == 0

    def test_custom_threshold(self):
        """Test custom threshold parameter."""
        from telegram_entities import format_command_output

        result = "\n".join([f"line{i}" for i in range(10)])

        # With threshold=5, should use expandable_blockquote
        entities, text = format_command_output(result, threshold=5)
        assert entities[0]['type'] == 'expandable_blockquote'

        # With threshold=20, should use pre
        entities, text = format_command_output(result, threshold=20)
        assert entities[0]['type'] == 'pre'

    def test_utf16_length_calculation(self):
        """Test that entity length is calculated correctly with UTF-16."""
        from telegram_entities import format_command_output

        result = "Output: 😀 success"
        entities, text = format_command_output(result)

        # "Output: 😀 success" = 7 + 1 + 2 + 1 + 7 = 18 UTF-16 units
        assert entities[0]['length'] == 18

    def test_strips_whitespace(self):
        """Test that output is stripped before formatting."""
        from telegram_entities import format_command_output

        result = "   \n  output  \n   "
        entities, text = format_command_output(result)

        assert text == "output"
