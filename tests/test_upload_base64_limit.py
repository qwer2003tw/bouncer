"""Tests for base64 truncation detection in upload (sprint9-006, closes #37)."""
import json
import base64
import pytest
from unittest.mock import patch


def _valid_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _truncated_b64(data: bytes) -> str:
    """Return a base64 string with length % 4 != 0 (simulating OS truncation)."""
    full = base64.b64encode(data).decode()
    # Strip chars until length % 4 != 0
    chopped = full[:-1]
    for chop in range(1, 5):
        candidate = full[:-chop]
        if len(candidate) % 4 != 0:
            return candidate
    return full[:-1]  # fallback


def _extract_mcp_error(body: dict) -> str:
    """Extract the error string from an MCP response."""
    # MCP JSON-RPC error
    if 'error' in body:
        return body['error'].get('message', '')
    # MCP result with isError content
    content = body.get('result', {}).get('content', [{}])
    if content:
        try:
            data = json.loads(content[0].get('text', '{}'))
            return data.get('error', '')
        except Exception:
            pass
    return ''


def _call_upload_via_mcp(app_module, arguments: dict) -> dict:
    event = {
        'rawPath': '/mcp',
        'headers': {'x-approval-secret': 'test-secret'},
        'body': json.dumps({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_upload',
                'arguments': arguments,
            },
        }),
        'requestContext': {'http': {'method': 'POST'}},
    }
    return json.loads(app_module.lambda_handler(event, None)['body'])


def _call_upload_batch_via_mcp(app_module, arguments: dict) -> dict:
    event = {
        'rawPath': '/mcp',
        'headers': {'x-approval-secret': 'test-secret'},
        'body': json.dumps({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {
                'name': 'bouncer_upload_batch',
                'arguments': arguments,
            },
        }),
        'requestContext': {'http': {'method': 'POST'}},
    }
    return json.loads(app_module.lambda_handler(event, None)['body'])


class TestSingleUploadBase64Truncation:
    """Test base64 length validation in bouncer_upload."""

    def test_valid_base64_not_rejected_for_truncation(self, app_module):
        """Valid base64 (length % 4 == 0) should NOT trigger truncation error."""
        good_b64 = _valid_b64(b"hello world this is valid content")
        assert len(good_b64) % 4 == 0
        body = _call_upload_via_mcp(app_module, {
            'filename': 'test.txt',
            'content': good_b64,
            'content_type': 'text/plain',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'truncated by OS argument length limits' not in err

    def test_truncated_base64_returns_clear_error(self, app_module):
        """Truncated base64 (length % 4 != 0) must return explicit error."""
        bad_b64 = _truncated_b64(b"hello world this is a longer string for testing purposes")
        assert len(bad_b64) % 4 != 0, "Test setup: must have non-multiple-of-4 length"
        body = _call_upload_via_mcp(app_module, {
            'filename': 'test.txt',
            'content': bad_b64,
            'content_type': 'text/plain',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'not a multiple of 4' in err, f"err={err!r}"
        assert 'truncated by OS argument length limits' in err, f"err={err!r}"

    def test_truncated_base64_suggests_http_or_presigned(self, app_module):
        """Error message should mention HTTP API or presigned URL as alternatives."""
        bad_b64 = _truncated_b64(b"some content that got cut off by the OS argument limit")
        body = _call_upload_via_mcp(app_module, {
            'filename': 'test.txt',
            'content': bad_b64,
            'content_type': 'text/plain',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'HTTP API' in err or 'bouncer_request_presigned' in err, f"err={err!r}"

    def test_length_mod4_eq1_detected(self, app_module):
        """length % 4 == 1 should be caught."""
        crafted = 'A' * (4 * 10 + 1)  # 41 chars, 41 % 4 == 1
        body = _call_upload_via_mcp(app_module, {
            'filename': 'file.bin',
            'content': crafted,
            'content_type': 'application/octet-stream',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'not a multiple of 4' in err, f"err={err!r}"

    def test_length_mod4_eq2_detected(self, app_module):
        """length % 4 == 2 should be caught."""
        crafted = 'A' * (4 * 10 + 2)  # 42 chars, 42 % 4 == 2
        body = _call_upload_via_mcp(app_module, {
            'filename': 'file.bin',
            'content': crafted,
            'content_type': 'application/octet-stream',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'not a multiple of 4' in err, f"err={err!r}"

    def test_length_mod4_eq3_detected(self, app_module):
        """length % 4 == 3 should be caught."""
        crafted = 'A' * (4 * 10 + 3)  # 43 chars, 43 % 4 == 3
        body = _call_upload_via_mcp(app_module, {
            'filename': 'file.bin',
            'content': crafted,
            'content_type': 'application/octet-stream',
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'not a multiple of 4' in err, f"err={err!r}"


class TestBatchUploadBase64Truncation:
    """Test base64 length validation in bouncer_upload_batch."""

    def test_batch_truncated_base64_returns_clear_error(self, app_module):
        """Truncated base64 in a batch file must return explicit error."""
        good_b64 = _valid_b64(b"first file content is fine")
        bad_b64 = _truncated_b64(b"second file content got truncated by shell argument limit")
        body = _call_upload_batch_via_mcp(app_module, {
            'files': [
                {'filename': 'file1.txt', 'content': good_b64, 'content_type': 'text/plain'},
                {'filename': 'file2.txt', 'content': bad_b64, 'content_type': 'text/plain'},
            ],
            'reason': 'test batch',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'not a multiple of 4' in err, f"err={err!r}"
        assert 'truncated by OS argument length limits' in err, f"err={err!r}"

    def test_batch_valid_base64_not_rejected(self, app_module):
        """Valid base64 in batch should not trigger truncation error."""
        good_b64 = _valid_b64(b"perfectly fine content here")
        body = _call_upload_batch_via_mcp(app_module, {
            'files': [
                {'filename': 'ok.txt', 'content': good_b64, 'content_type': 'text/plain'},
            ],
            'reason': 'test',
            'trust_scope': 'test-scope',
        })
        err = _extract_mcp_error(body)
        assert 'truncated by OS argument length limits' not in err, f"err={err!r}"
