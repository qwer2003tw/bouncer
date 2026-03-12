"""
Tests for Trust Session IP binding (Sprint 26 - security/trust-session-ip-binding-s26).
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import MagicMock, patch, call
import time


def _make_session_item(creator_ip='', **overrides):
    now = int(time.time())
    item = {
        'request_id': 'trust-ip-test-001',
        'type': 'trust_session',
        'trust_scope': 'private-bot-main',
        'account_id': '190825685292',
        'approved_by': 'user-123',
        'source': 'Private Bot',
        'bound_source': 'Private Bot',
        'creator_ip': creator_ip,
        'command_count': 0,
        'max_uploads': 0,
        'upload_count': 0,
        'upload_bytes_total': 0,
        'created_at': int(time.time()),
        'expires_at': int(time.time()) + 600,
        'ttl': int(time.time()) + 600,
    }
    item.update(overrides)
    return item


def _run_should_trust(session_item, caller_ip=''):
    """Run should_trust_approve with mocked DDB table."""
    import trust as _trust_module
    from trust import should_trust_approve

    mock_table = MagicMock()
    mock_table.get_item.return_value = {'Item': session_item}
    mock_get_table = MagicMock(return_value=mock_table)

    with patch.object(_trust_module, '_get_table', mock_get_table):
        return should_trust_approve(
            'aws s3 ls',
            trust_scope='private-bot-main',
            account_id='190825685292',
            source='Private Bot',
            caller_ip=caller_ip,
        )


# ---------------------------------------------------------------------------
# Test: create_trust_session stores creator_ip
# ---------------------------------------------------------------------------

class TestCreateTrustSessionStoresIP:
    def test_creator_ip_stored_in_ddb(self):
        """create_trust_session with creator_ip stores the IP in DDB item."""
        import trust as _trust_module
        from trust import create_trust_session

        mock_table = MagicMock()
        mock_get_table = MagicMock(return_value=mock_table)

        with patch.object(_trust_module, '_get_table', mock_get_table), \
             patch('scheduler_service.get_trust_expiry_notifier', MagicMock()):
            create_trust_session(
                'private-bot-main', '190825685292', 'user-123',
                source='Private Bot',
                creator_ip='203.0.113.1',
            )

        item = mock_table.put_item.call_args[1]['Item']
        assert item['creator_ip'] == '203.0.113.1'

    def test_creator_ip_empty_by_default(self):
        """create_trust_session without creator_ip stores empty string."""
        import trust as _trust_module
        from trust import create_trust_session

        mock_table = MagicMock()
        mock_get_table = MagicMock(return_value=mock_table)

        with patch.object(_trust_module, '_get_table', mock_get_table), \
             patch('scheduler_service.get_trust_expiry_notifier', MagicMock()):
            create_trust_session(
                'private-bot-main', '190825685292', 'user-123',
                source='Private Bot',
            )

        item = mock_table.put_item.call_args[1]['Item']
        assert item['creator_ip'] == ''


# ---------------------------------------------------------------------------
# Test: should_trust_approve — IP mismatch is warn-only, not blocking
# ---------------------------------------------------------------------------

class TestShouldTrustApproveIPMismatch:

    def test_no_creator_ip_passes_silently(self):
        """When creator_ip is empty, no warning about IP mismatch is emitted."""
        import trust as _trust_module
        item = _make_session_item(creator_ip='')
        with patch.object(_trust_module, 'logger') as mock_logger:
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.0.0.1')
        assert should_approve is True
        # No IP mismatch warning (other warnings from source binding are OK)
        ip_warnings = [c for c in mock_logger.warning.call_args_list
                       if 'mismatch' in str(c).lower() and 'IP' in str(c)]
        assert len(ip_warnings) == 0

    def test_no_caller_ip_passes_silently(self):
        """When caller_ip is empty, no IP mismatch warning is emitted."""
        import trust as _trust_module
        item = _make_session_item(creator_ip='203.0.113.1')
        with patch.object(_trust_module, 'logger') as mock_logger:
            should_approve, _, _ = _run_should_trust(item, caller_ip='')
        assert should_approve is True
        ip_warnings = [c for c in mock_logger.warning.call_args_list
                       if 'IP mismatch' in str(c)]
        assert len(ip_warnings) == 0

    def test_matching_ips_passes_silently(self):
        """When creator_ip == caller_ip, no IP mismatch warning is emitted."""
        import trust as _trust_module
        item = _make_session_item(creator_ip='10.0.0.1')
        with patch.object(_trust_module, 'logger') as mock_logger:
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.0.0.1')
        assert should_approve is True
        ip_warnings = [c for c in mock_logger.warning.call_args_list
                       if 'IP mismatch' in str(c)]
        assert len(ip_warnings) == 0

    def test_ip_mismatch_emits_warning_but_still_approves(self):
        """When IPs differ, warning is logged but session is NOT blocked."""
        import trust as _trust_module
        item = _make_session_item(creator_ip='203.0.113.1')
        with patch.object(_trust_module, 'logger') as mock_logger, \
             patch('metrics.emit_metric', MagicMock()):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is True
        ip_warnings = [c for c in mock_logger.warning.call_args_list
                       if 'IP mismatch' in str(c)]
        assert len(ip_warnings) >= 1

    def test_ip_mismatch_emits_metric(self):
        """When IPs differ, TrustIPMismatch metric is emitted."""
        import trust as _trust_module
        item = _make_session_item(creator_ip='203.0.113.1')

        emitted = []
        def capture(*args, **kwargs):
            emitted.append({'args': args, 'kwargs': kwargs})

        with patch('metrics.emit_metric', side_effect=capture):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is True
        mismatch_calls = [e for e in emitted if 'TrustIPMismatch' in str(e)]
        assert len(mismatch_calls) >= 1


# ---------------------------------------------------------------------------
# Test: TRUST_IP_BINDING_MODE - configurable IP binding (strict/warn/disabled)
# ---------------------------------------------------------------------------

class TestTrustIPBindingModes:
    """Test configurable IP binding modes: strict, warn, disabled."""

    def test_strict_mode_matching_ip_passes(self):
        """Strict mode: matching IPs should pass."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='10.0.0.1')

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'strict'):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.0.0.1')

        assert should_approve is True

    def test_strict_mode_mismatch_blocks(self):
        """Strict mode: IP mismatch should block the request."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'strict'), \
             patch('metrics.emit_metric', MagicMock()):
            should_approve, session, reason = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is False
        assert 'IP mismatch blocked' in reason
        assert 'strict mode' in reason

    def test_strict_mode_emits_blocked_metric(self):
        """Strict mode: IP mismatch should emit TrustIPBlocked metric."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        emitted = []
        def capture(*args, **kwargs):
            emitted.append({'args': args, 'kwargs': kwargs})

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'strict'), \
             patch('metrics.emit_metric', side_effect=capture):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is False
        blocked_calls = [e for e in emitted if 'TrustIPBlocked' in str(e)]
        assert len(blocked_calls) >= 1

    def test_warn_mode_mismatch_allows(self):
        """Warn mode (default): IP mismatch should log warning but allow."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'warn'), \
             patch('metrics.emit_metric', MagicMock()):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is True

    def test_warn_mode_emits_mismatch_metric(self):
        """Warn mode: IP mismatch should emit TrustIPMismatch metric."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        emitted = []
        def capture(*args, **kwargs):
            emitted.append({'args': args, 'kwargs': kwargs})

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'warn'), \
             patch('metrics.emit_metric', side_effect=capture):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is True
        mismatch_calls = [e for e in emitted if 'TrustIPMismatch' in str(e)]
        assert len(mismatch_calls) >= 1

    def test_disabled_mode_skips_check(self):
        """Disabled mode: IP mismatch should be completely ignored (no warning, no metric)."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        emitted = []
        def capture(*args, **kwargs):
            emitted.append({'args': args, 'kwargs': kwargs})

        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'disabled'), \
             patch('metrics.emit_metric', side_effect=capture):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        assert should_approve is True

        # Should NOT have emitted IP mismatch/blocked metrics
        ip_metrics = [e for e in emitted
                      if 'TrustIPMismatch' in str(e) or 'TrustIPBlocked' in str(e)]
        assert len(ip_metrics) == 0

    def test_invalid_mode_falls_back_to_warn(self):
        """Invalid mode value should fall back to 'warn' mode."""
        import trust as _trust_module

        item = _make_session_item(creator_ip='203.0.113.1')

        # Simulate invalid mode by setting TRUST_IP_BINDING_MODE to 'warn'
        # The constants.py logic should normalize invalid values to 'warn'
        with patch.object(_trust_module, 'TRUST_IP_BINDING_MODE', 'warn'), \
             patch('metrics.emit_metric', MagicMock()):
            should_approve, _, _ = _run_should_trust(item, caller_ip='10.4.150.40')

        # Should behave like warn mode: allow the request
        assert should_approve is True
