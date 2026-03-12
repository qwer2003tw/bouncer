"""
Bouncer - Telegram Entities Builder

Provides MessageBuilder for constructing Telegram messages with entities
(offset/length-based formatting) instead of Markdown parse_mode.

Telegram uses UTF-16 code units for offset/length calculation:
  - Most characters: 1 UTF-16 unit
  - Emoji / supplementary plane (U+10000+): 2 UTF-16 units (surrogate pair)

This avoids all Markdown escape issues; text and formatting are fully separated.

Usage:
    from telegram_entities import MessageBuilder

    builder = MessageBuilder()
    builder.bold("Request Approval").newline()
    builder.text("Command: ").code("aws s3 ls").newline()
    builder.text("Reason: ").text(reason)
    text, entities = builder.build()
    send_message_with_entities(text, entities)
"""

from typing import Optional


def _utf16_len(text: str) -> int:
    """Return the number of UTF-16 code units for a string.

    Most Unicode characters occupy 1 code unit.
    Characters outside the BMP (code point > U+FFFF, e.g. most emoji)
    require 2 code units (surrogate pair) in UTF-16.
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


class MessageBuilder:
    """Fluent builder for Telegram messages with entities.

    Accumulates (text, entity_type) segments and computes correct
    UTF-16 offsets and lengths for each formatted span.

    Supported entity types (Telegram Bot API):
      'bold', 'italic', 'underline', 'strikethrough', 'spoiler',
      'code', 'pre', 'expandable_blockquote', 'text_link', 'text_mention',
      'custom_emoji', 'date_time' (display hint only — not a standard
      Telegram entity, but stored as a custom entity for downstream use)

    Plain text segments (entity_type=None) contribute to offset but
    produce no entity in the output list.
    """

    def __init__(self):
        self._parts: list[tuple[str, Optional[str]]] = []

    # ------------------------------------------------------------------
    # Fluent API
    # ------------------------------------------------------------------

    def text(self, content) -> "MessageBuilder":
        """Add plain (unformatted) text."""
        self._parts.append((str(content), None))
        return self

    def bold(self, content) -> "MessageBuilder":
        """Add bold text."""
        self._parts.append((str(content), 'bold'))
        return self

    def italic(self, content) -> "MessageBuilder":
        """Add italic text."""
        self._parts.append((str(content), 'italic'))
        return self

    def code(self, content) -> "MessageBuilder":
        """Add inline code text."""
        self._parts.append((str(content), 'code'))
        return self

    def pre(self, content) -> "MessageBuilder":
        """Add pre-formatted block text."""
        self._parts.append((str(content), 'pre'))
        return self

    def expandable_blockquote(self, content) -> "MessageBuilder":
        """Add expandable blockquote text (collapsible block)."""
        self._parts.append((str(content), 'expandable_blockquote'))
        return self

    def underline(self, content) -> "MessageBuilder":
        """Add underlined text."""
        self._parts.append((str(content), 'underline'))
        return self

    def strikethrough(self, content) -> "MessageBuilder":
        """Add strikethrough text."""
        self._parts.append((str(content), 'strikethrough'))
        return self

    def spoiler(self, content) -> "MessageBuilder":
        """Add spoiler text."""
        self._parts.append((str(content), 'spoiler'))
        return self

    def date_time(self, content) -> "MessageBuilder":
        """Add date/time text with date_time entity (custom hint)."""
        self._parts.append((str(content), 'date_time'))
        return self

    def newline(self, count: int = 1) -> "MessageBuilder":
        """Add one or more newline characters (plain text)."""
        self._parts.append(('\n' * count, None))
        return self

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> tuple[str, list]:
        """Build the final (text, entities) pair.

        Returns:
            text:     The concatenated plain text string.
            entities: List of Telegram entity dicts with
                      {'type': ..., 'offset': ..., 'length': ...}.
        """
        text_parts = []
        entities = []
        offset = 0  # UTF-16 code unit offset

        for content, entity_type in self._parts:
            content_str = str(content)
            utf16_length = _utf16_len(content_str)

            if entity_type and utf16_length > 0:
                entities.append({
                    'type': entity_type,
                    'offset': offset,
                    'length': utf16_length,
                })

            text_parts.append(content_str)
            offset += utf16_length

        return ''.join(text_parts), entities

    # ------------------------------------------------------------------
    # Convenience class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_parts(cls, parts: list) -> "MessageBuilder":
        """Create a MessageBuilder from a list of (content, entity_type_or_None) tuples.

        This is a convenience factory that matches the functional
        build_entities_message() signature for callers that prefer
        the tuple-list style over the fluent API.
        """
        builder = cls()
        for content, entity_type in parts:
            builder._parts.append((str(content), entity_type))
        return builder


def build_entities_message(parts: list) -> tuple[str, list]:
    """Build a Telegram message with entities from a list of (text, entity_type) parts.

    parts: [(text, entity_type_or_None), ...]
    entity_type: 'bold', 'code', 'pre', 'italic', 'date_time', None

    Returns: (text, entities_list)
    Note: Telegram uses UTF-16 code units for offset calculation.

    This is a functional convenience wrapper around MessageBuilder.
    """
    builder = MessageBuilder.from_parts(parts)
    return builder.build()


def format_command_output(result: str, threshold: int = 50) -> tuple[list, str]:
    """Format command output as plain text + entities.

    Long output (>threshold lines) → expandable_blockquote entity
    Short output → pre entity (code block)
    Empty output → "(no output)" plain text

    Args:
        result: Command output string
        threshold: Line count threshold for expandable blockquote (default: 50)

    Returns:
        Tuple of (entities_list, text_for_api)
        - entities_list: List of entity dicts for Telegram API
        - text_for_api: Plain text content

    Note:
        expandable_blockquote is supported since Telegram Bot API 7.0 (2024-03-31)
    """
    if not result or not result.strip():
        return [], "(no output)"

    lines = result.strip().splitlines()
    text = result.strip()

    if len(lines) > threshold:
        # Long output: use expandable_blockquote
        entity = {
            "type": "expandable_blockquote",
            "offset": 0,
            "length": _utf16_len(text)
        }
        return [entity], text
    else:
        # Short output: use pre entity (existing behavior)
        entity = {
            "type": "pre",
            "offset": 0,
            "length": _utf16_len(text)
        }
        return [entity], text
