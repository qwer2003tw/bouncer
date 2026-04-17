"""
Tests for utils.sanitize_filename() — verifies the function works correctly
in its new canonical location (moved from mcp_presigned/_sanitize_filename
and mcp_upload/_sanitize_filename).
"""

import sys
import os


import pytest
from unittest.mock import patch

# Patch constants before importing utils, then remove stub to avoid polluting
# other test modules that need the real constants (e.g. TELEGRAM_TOKEN).
import types as _types
_constants_already_present = 'constants' in sys.modules
if not _constants_already_present:
    _constants_stub = _types.ModuleType('constants')
    _constants_stub.AUDIT_TTL_SHORT = 86400 * 30
    _constants_stub.AUDIT_TTL_LONG = 86400 * 90
    sys.modules['constants'] = _constants_stub

from utils import sanitize_filename  # noqa: E402

# Remove stub immediately after import so later test modules get the real one.
if not _constants_already_present:
    del sys.modules['constants']


class TestSanitizeFilenameBasename:
    """keep_path=False (default) — basename-only mode used by mcp_upload"""

    def test_simple_filename(self):
        assert sanitize_filename('hello.txt') == 'hello.txt'

    def test_strips_directory(self):
        assert sanitize_filename('some/path/file.txt') == 'file.txt'

    def test_strips_backslash_directory(self):
        assert sanitize_filename('some\\path\\file.txt') == 'file.txt'

    def test_removes_null_bytes(self):
        assert sanitize_filename('file\x00name.txt') == 'filename.txt'

    def test_removes_path_traversal(self):
        assert sanitize_filename('../../../etc/passwd') == 'passwd'

    def test_strips_leading_dots(self):
        assert sanitize_filename('.hidden') == 'hidden'

    def test_strips_leading_spaces(self):
        assert sanitize_filename('  file.txt') == 'file.txt'

    def test_replaces_special_chars(self):
        result = sanitize_filename('file name!@#.txt')
        assert result == 'file_name___.txt'

    def test_empty_result_returns_unnamed(self):
        assert sanitize_filename('...') == 'unnamed'

    def test_empty_string_returns_unnamed(self):
        assert sanitize_filename('') == 'unnamed'

    def test_null_only_returns_unnamed(self):
        assert sanitize_filename('\x00') == 'unnamed'

    def test_preserves_hyphens_and_underscores(self):
        assert sanitize_filename('my-file_v2.tar.gz') == 'my-file_v2.tar.gz'


class TestSanitizeFilenameKeepPath:
    """keep_path=True — subdirectory-preserving mode used by mcp_presigned"""

    def test_simple_filename(self):
        assert sanitize_filename('hello.txt', keep_path=True) == 'hello.txt'

    def test_preserves_subdir_structure(self):
        assert sanitize_filename('assets/foo.js', keep_path=True) == 'assets/foo.js'

    def test_preserves_nested_subdir(self):
        assert sanitize_filename('static/js/app.chunk.js', keep_path=True) == 'static/js/app.chunk.js'

    def test_removes_path_traversal_segments(self):
        result = sanitize_filename('../../etc/passwd', keep_path=True)
        # traversal segments should be stripped
        assert '..' not in result
        assert 'passwd' in result

    def test_removes_null_bytes(self):
        assert sanitize_filename('dir/\x00file.txt', keep_path=True) == 'dir/file.txt'

    def test_normalises_backslash(self):
        result = sanitize_filename('assets\\foo.js', keep_path=True)
        assert result == 'assets/foo.js'

    def test_empty_result_returns_unnamed(self):
        assert sanitize_filename('...', keep_path=True) == 'unnamed'

    def test_empty_string_returns_unnamed(self):
        assert sanitize_filename('', keep_path=True) == 'unnamed'

    def test_replaces_special_chars_in_segment(self):
        result = sanitize_filename('assets/my file!.js', keep_path=True)
        assert result == 'assets/my_file_.js'

    def test_preserves_hyphens_and_underscores(self):
        assert sanitize_filename('dist/my-file_v2.js', keep_path=True) == 'dist/my-file_v2.js'
