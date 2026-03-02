"""
Sprint 9-002: upload_batch 大檔案靜默失敗根因修復
Tests for early payload size validation in mcp_tool_upload_batch.

Scenarios:
  1. Total base64 payload exceeds UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT -> error + suggestion
  2. Single file base64 size exceeds UPLOAD_BATCH_PER_FILE_B64_LIMIT -> error + suggestion
  3. Payload within limits -> passes validation (no early rejection)
  4. Multiple files all within per-file limit but total exceeds -> total check fires first
  5. Error response shape matches spec (status, error, suggestion, payload_size, safe_limit)
"""

import base64
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DEFAULT_ACCOUNT_ID', '111111111111')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(size_bytes: int) -> str:
    """Return a base64 string representing `size_bytes` bytes of zero data."""
    return base64.b64encode(b'\x00' * size_bytes).decode()


def _make_file(filename: str, raw_size: int, content_type: str = 'application/octet-stream') -> dict:
    return {
        'filename': filename,
        'content': _b64(raw_size),
        'content_type': content_type,
    }


def _parse_result(result: dict) -> dict:
    """Extract the JSON payload from an MCP JSON-RPC result envelope."""
    body = result.get('body') or result
    if isinstance(body, str):
        body = json.loads(body)
    # JSON-RPC: {"jsonrpc":"2.0","id":...,"result":{"content":[{"type":"text","text":"..."}],...}}
    if 'result' in body:
        text = body['result']['content'][0]['text']
    elif 'content' in body:
        text = body['content'][0]['text']
    else:
        raise ValueError(f"Unexpected result structure: {body!r}")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Constants (imported directly to verify values are sensible)
# ---------------------------------------------------------------------------

from constants import (
    UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT,
    UPLOAD_BATCH_PER_FILE_B64_LIMIT,
)


class TestUploadBatchConstants(unittest.TestCase):
    """Verify constant values match spec requirements."""

    def test_payload_safe_limit_is_3_5mb(self):
        self.assertEqual(UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT, 3_500_000)

    def test_per_file_b64_limit_is_3_5mb(self):
        self.assertEqual(UPLOAD_BATCH_PER_FILE_B64_LIMIT, 3_500_000)

    def test_payload_safe_limit_less_than_lambda_hard_limit(self):
        """3.5MB base64 < 6MB Lambda limit -> provides safety margin."""
        lambda_hard_limit = 6 * 1024 * 1024
        self.assertLess(UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT, lambda_hard_limit)


# ---------------------------------------------------------------------------
# Unit tests -- mcp_tool_upload_batch early validation
# ---------------------------------------------------------------------------

PATCH_TARGETS = [
    'mcp_upload.init_default_account',
    'mcp_upload.get_account',
    'mcp_upload.list_accounts',
    'mcp_upload.validate_account_id',
    'mcp_upload.check_rate_limit',
    'mcp_upload.table',
    'mcp_upload.boto3',
    'mcp_upload.send_batch_upload_notification',
    'mcp_upload._telegram',
]


class TestUploadBatchEarlyValidation(unittest.TestCase):

    def setUp(self):
        self.patches = []
        for target in PATCH_TARGETS:
            try:
                p = patch(target)
                mock = p.start()
                if 'get_account' in target:
                    mock.return_value = None
                self.patches.append(p)
            except Exception:
                pass

        # Also patch trust helpers used inside the function
        for trust_fn in [
            'trust._is_upload_extension_blocked',
            'trust._is_upload_filename_safe',
            'trust.get_trust_session',
            'trust.should_trust_approve_upload',
            'trust.increment_trust_upload_count',
        ]:
            try:
                p = patch(trust_fn)
                mock = p.start()
                if '_is_upload_filename_safe' in trust_fn:
                    mock.return_value = True
                if '_is_upload_extension_blocked' in trust_fn:
                    mock.return_value = False
                if 'get_trust_session' in trust_fn:
                    mock.return_value = None
                if 'should_trust_approve_upload' in trust_fn:
                    mock.return_value = (False, None, 'no trust')
                self.patches.append(p)
            except Exception:
                pass

    def tearDown(self):
        for p in self.patches:
            try:
                p.stop()
            except Exception:
                pass

    def _call(self, files, **kwargs):
        from mcp_upload import mcp_tool_upload_batch
        arguments = {'files': files, 'reason': 'test', **kwargs}
        return mcp_tool_upload_batch('req-test-001', arguments)

    # ------------------------------------------------------------------
    # Scenario 1: total payload exceeds safe limit
    # ------------------------------------------------------------------

    def test_total_payload_too_large_returns_error(self):
        """3 x ~1.5MB base64 ~= 4.5MB > 3.5MB limit -> error."""
        # Each file ~1.1MB raw -> ~1.5MB base64; 3 files total ~4.5MB base64
        files = [_make_file(f'file{i}.bin', 1_100_000) for i in range(3)]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertEqual(payload['status'], 'error')
        self.assertIn('too large', payload['error'].lower())
        self.assertIn('bouncer_request_presigned_batch', payload['error'])
        self.assertEqual(payload['suggestion'], 'bouncer_request_presigned_batch')

    def test_total_payload_too_large_includes_size_fields(self):
        """Error response includes payload_size and safe_limit fields per spec."""
        files = [_make_file(f'big{i}.bin', 1_100_000) for i in range(3)]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertIn('payload_size', payload)
        self.assertIn('safe_limit', payload)
        self.assertEqual(payload['safe_limit'], UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT)
        self.assertGreater(payload['payload_size'], UPLOAD_BATCH_PAYLOAD_SAFE_LIMIT)

    def test_total_payload_too_large_is_error(self):
        """MCP result body must contain isError=True for oversized payload."""
        files = [_make_file(f'f{i}.bin', 1_100_000) for i in range(3)]
        result = self._call(files)
        body = result.get('body') or result
        if isinstance(body, str):
            body = json.loads(body)
        rpc_result = body.get('result', {})
        self.assertTrue(rpc_result.get('isError'))

    # ------------------------------------------------------------------
    # Scenario 2: single file exceeds per-file base64 limit
    # ------------------------------------------------------------------

    def test_single_file_too_large_returns_error(self):
        """A single file whose base64 size > UPLOAD_BATCH_PER_FILE_B64_LIMIT.
        
        base64(2_625_001 bytes) = 3_500_004 chars > 3_500_000 limit.
        """
        # 2,625,001 raw bytes -> 3,500,004 base64 chars (just over limit)
        files = [_make_file('huge.bin', 2_625_001)]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertEqual(payload['status'], 'error')
        self.assertIn('bouncer_request_presigned_batch', payload['error'])
        self.assertEqual(payload['suggestion'], 'bouncer_request_presigned_batch')

    def test_single_file_too_large_includes_file_fields(self):
        """Per-file error includes file_index, filename, file_b64_size, per_file_limit."""
        files = [_make_file('huge.bin', 2_625_001)]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertIn('file_index', payload)
        self.assertEqual(payload['file_index'], 1)
        self.assertEqual(payload['filename'], 'huge.bin')
        self.assertIn('file_b64_size', payload)
        self.assertIn('per_file_limit', payload)
        self.assertEqual(payload['per_file_limit'], UPLOAD_BATCH_PER_FILE_B64_LIMIT)

    def test_second_file_too_large_reports_correct_index(self):
        """When the second file is oversized, file_index should be 2."""
        files = [
            _make_file('small.txt', 100),
            _make_file('huge.bin', 2_625_001),
        ]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertIn('file_index', payload)
        self.assertEqual(payload['file_index'], 2)
        self.assertEqual(payload['filename'], 'huge.bin')

    # ------------------------------------------------------------------
    # Scenario 3: payload within limits -> no early rejection
    # ------------------------------------------------------------------

    def test_small_payload_passes_size_validation(self):
        """5 x 100KB raw ~= 680KB base64 -> well within limit, no size error."""
        files = [_make_file(f'ok{i}.js', 100_000) for i in range(5)]
        result = self._call(files)
        payload = _parse_result(result)

        # Should not be a size validation error
        self.assertNotIn('bouncer_request_presigned_batch', payload.get('error', ''))

    def test_single_small_file_passes_per_file_check(self):
        """Single 500KB raw file ~= 685KB base64 -- well within 3.5MB per-file limit."""
        files = [_make_file('asset.wasm', 500_000)]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertNotEqual(payload.get('suggestion'), 'bouncer_request_presigned_batch')

    # ------------------------------------------------------------------
    # Scenario 4: multiple files pass per-file but total exceeds
    # ------------------------------------------------------------------

    def test_total_check_fires_before_per_file_check(self):
        """3 x 1.1MB raw each passes per-file check individually,
        but total ~= 4.5MB base64 triggers the total check first.
        
        Total error has payload_size (no file_index); per-file error has file_index.
        """
        files = [_make_file(f'chunk{i}.bin', 1_100_000) for i in range(3)]
        result = self._call(files)
        payload = _parse_result(result)

        # Should get total payload error (has payload_size, not file_index)
        self.assertIn('payload_size', payload)
        self.assertNotIn('file_index', payload)

    # ------------------------------------------------------------------
    # Scenario 5: real base64 content triggers per-file check
    # ------------------------------------------------------------------

    def test_validation_fires_before_base64_decode(self):
        """Oversized but valid base64 string triggers per-file check.
        
        raw=2_625_001 -> b64=3_500_004 chars > 3_500_000 limit.
        """
        raw = b'\x00' * 2_625_001  # 2,625,001 raw -> 3,500,004 base64 chars
        b64_str = base64.b64encode(raw).decode()
        self.assertGreater(len(b64_str), UPLOAD_BATCH_PER_FILE_B64_LIMIT)

        files = [{'filename': 'real_big.bin', 'content': b64_str, 'content_type': 'application/octet-stream'}]
        result = self._call(files)
        payload = _parse_result(result)

        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['suggestion'], 'bouncer_request_presigned_batch')

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_files_array_still_rejected(self):
        """Empty files array is rejected before size validation."""
        result = self._call([])
        payload = _parse_result(result)
        self.assertEqual(payload['status'], 'error')
        self.assertIn('files', payload['error'].lower())

    def test_files_with_empty_content_pass_size_check(self):
        """Files with empty content strings (size=0) pass size validation."""
        files = [{'filename': 'empty.txt', 'content': '', 'content_type': 'text/plain'}]
        result = self._call(files)
        payload = _parse_result(result)
        # Should NOT get a payload-size / presigned suggestion error
        self.assertNotIn('bouncer_request_presigned_batch', payload.get('error', ''))


if __name__ == '__main__':
    unittest.main()
