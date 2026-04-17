"""
test_sprint31_004_status_expired.py — bouncer_status expired vs pending (Sprint 31-004)

Tests for mcp_tool_status TTL expiry check in mcp_admin.py:
  1. pending_approval + TTL expired → status: 'expired'
  2. pending_approval + TTL not expired → status: 'pending_approval'
  3. approved + expired TTL → status: 'approved' (TTL check only for pending_approval)
  4. pending_approval + no TTL field → status: 'pending_approval'
  5. pending_approval + TTL=0 → status: 'pending_approval' (TTL=0 treated as no TTL)
"""
import json
import os
import time
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest



def call_mcp_status(request_id: str, db_item: dict | None):
    """Helper: call mcp_tool_status with a mocked DDB item."""
    import mcp_admin

    mock_result = {'Item': db_item} if db_item is not None else {}

    with patch('mcp_admin.table') as mock_table:
        mock_table.get_item.return_value = mock_result
        result = mcp_admin.mcp_tool_status('req-001', {'request_id': request_id})

    body = json.loads(result.get('body', '{}'))
    content_text = body.get('result', {}).get('content', [{}])[0].get('text', '{}')
    return json.loads(content_text)


class TestMcpToolStatusExpired:
    """mcp_tool_status distinguishes expired vs pending_approval."""

    def test_expired_when_pending_and_ttl_passed(self, app_module):
        """pending_approval + TTL in the past → status: 'expired'."""
        item = {
            'request_id': 'req-expired-001',
            'status': 'pending_approval',
            'ttl': int(time.time()) - 100,
        }
        result = call_mcp_status('req-expired-001', item)

        assert result.get('status') == 'expired', (
            f"Expected 'expired' when pending_approval with past TTL, got {result.get('status')!r}"
        )
        assert result.get('request_id') == 'req-expired-001'
        assert '請求已過期' in result.get('message', '')

    def test_pending_when_ttl_not_passed(self, app_module):
        """pending_approval + TTL in the future → status: 'pending_approval' (unchanged)."""
        item = {
            'request_id': 'req-pending-002',
            'status': 'pending_approval',
            'ttl': int(time.time()) + 300,
        }
        result = call_mcp_status('req-pending-002', item)

        assert result.get('status') == 'pending_approval', (
            f"Expected 'pending_approval' when TTL not expired, got {result.get('status')!r}"
        )

    def test_approved_with_expired_ttl_unchanged(self, app_module):
        """approved request with expired TTL → status: 'approved' (TTL check only for pending_approval)."""
        item = {
            'request_id': 'req-approved-003',
            'status': 'approved',
            'ttl': int(time.time()) - 100,
        }
        result = call_mcp_status('req-approved-003', item)

        assert result.get('status') == 'approved', (
            f"Expected 'approved' for non-pending_approval status, got {result.get('status')!r}"
        )

    def test_pending_approval_no_ttl_field_unchanged(self, app_module):
        """pending_approval without ttl field → status: 'pending_approval' (cannot determine expiry)."""
        item = {
            'request_id': 'req-no-ttl-004',
            'status': 'pending_approval',
            # no 'ttl' key
        }
        result = call_mcp_status('req-no-ttl-004', item)

        assert result.get('status') == 'pending_approval', (
            f"Expected 'pending_approval' when no TTL field, got {result.get('status')!r}"
        )

    def test_pending_approval_ttl_zero_unchanged(self, app_module):
        """pending_approval with ttl=0 → status: 'pending_approval' (0 treated as no TTL)."""
        item = {
            'request_id': 'req-ttl-zero-005',
            'status': 'pending_approval',
            'ttl': 0,
        }
        result = call_mcp_status('req-ttl-zero-005', item)

        assert result.get('status') == 'pending_approval', (
            f"Expected 'pending_approval' when ttl=0, got {result.get('status')!r}"
        )

    def test_expired_ttl_stored_as_decimal(self, app_module):
        """DynamoDB stores numbers as Decimal; TTL comparison must still work."""
        item = {
            'request_id': 'req-decimal-006',
            'status': 'pending_approval',
            'ttl': Decimal(str(int(time.time()) - 200)),  # DDB-style Decimal
        }
        result = call_mcp_status('req-decimal-006', item)

        assert result.get('status') == 'expired', (
            f"Expected 'expired' when Decimal TTL is in the past, got {result.get('status')!r}"
        )
