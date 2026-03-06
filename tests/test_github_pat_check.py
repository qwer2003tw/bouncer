"""
Tests for GitHub PAT health check in sam_deploy.py (Sprint 13, #57).
Validates _check_github_pat() behavior for 401, 403, success, and network errors.
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock
import urllib.error

# sam_deploy.py is not in src/, import directly
DEPLOYER_SCRIPTS = os.path.join(os.path.dirname(__file__), '..', 'deployer', 'scripts')
sys.path.insert(0, DEPLOYER_SCRIPTS)


class TestCheckGithubPat:
    """Tests for _check_github_pat() function in sam_deploy.py."""

    def _call(self, token='ghp_test_token'):
        from sam_deploy import _check_github_pat
        os.environ['GITHUB_PAT'] = token
        try:
            _check_github_pat()
        finally:
            os.environ.pop('GITHUB_PAT', None)

    def test_valid_pat_does_not_raise(self):
        """HTTP 200 → no exception raised."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            self._call()  # should not raise

    def test_expired_pat_raises_system_exit(self):
        """HTTP 401 → sys.exit(1)."""
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.HTTPError(None, 401, 'Unauthorized', {}, None)):
            with pytest.raises(SystemExit) as exc_info:
                self._call()
            assert exc_info.value.code == 1

    def test_rate_limited_does_not_raise(self):
        """HTTP 403 (rate limit) → graceful degradation, no exit."""
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.HTTPError(None, 403, 'Forbidden', {}, None)):
            self._call()  # should not raise

    def test_network_error_does_not_raise(self):
        """Network error → graceful degradation."""
        with patch('urllib.request.urlopen', side_effect=Exception('Connection refused')):
            self._call()  # should not raise

    def test_empty_pat_skips_check(self):
        """Empty PAT → skip validation, no API call."""
        with patch('urllib.request.urlopen') as mock_urlopen:
            os.environ['GITHUB_PAT'] = ''
            try:
                from sam_deploy import _check_github_pat
                _check_github_pat()
            finally:
                os.environ.pop('GITHUB_PAT', None)
            mock_urlopen.assert_not_called()

    def test_no_pat_env_skips_check(self):
        """No GITHUB_PAT env var → skip validation."""
        with patch('urllib.request.urlopen') as mock_urlopen:
            os.environ.pop('GITHUB_PAT', None)
            from sam_deploy import _check_github_pat
            _check_github_pat()
            mock_urlopen.assert_not_called()

    def test_401_error_message_mentions_secrets_manager(self, capsys):
        """401 error message should tell user where to update the secret."""
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.HTTPError(None, 401, 'Unauthorized', {}, None)):
            with pytest.raises(SystemExit):
                self._call()
        captured = capsys.readouterr()
        assert 'sam-deployer/github-pat' in captured.err or 'Secrets Manager' in captured.err
