"""Tests for upload file scanner security screening (#smart-phase5)."""
import pytest
from src.upload_scanner import scan_upload, UploadScanResult


class TestBlockedExtensions:
    """Test blocked file extensions rejection."""

    def test_exe_blocked(self):
        """Executable files (.exe) should be blocked."""
        result = scan_upload('malware.exe', b'fake content', 'application/octet-stream')
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'
        assert '.exe' in result.summary
        assert 'Blocked file type: .exe' in result.findings

    def test_sh_blocked(self):
        """Shell scripts (.sh) should be blocked."""
        result = scan_upload('script.sh', b'#!/bin/bash\necho "test"', 'text/x-shellscript')
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'
        assert '.sh' in result.summary

    def test_bat_blocked(self):
        """Batch files (.bat) should be blocked."""
        result = scan_upload('run.bat', b'@echo off', 'application/x-bat')
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'

    def test_dll_blocked(self):
        """DLL files (.dll) should be blocked."""
        result = scan_upload('library.dll', b'MZ\x90\x00', 'application/x-msdownload')
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'

    def test_ps1_blocked(self):
        """PowerShell scripts (.ps1) should be blocked."""
        result = scan_upload('script.ps1', b'Write-Host "test"', 'text/plain')
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'


class TestSecretDetection:
    """Test secret pattern detection in text files."""

    def test_aws_secret_key_detected(self):
        """AWS Secret Access Key should be detected."""
        content = b"""
# Config file
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
region = us-east-1
"""
        result = scan_upload('config.txt', content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'AWS Secret Access Key' in result.findings
        assert '敏感資訊' in result.summary

    def test_aws_access_key_id_detected(self):
        """AWS Access Key ID should be detected."""
        content = b"""
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_REGION=us-west-2
"""
        result = scan_upload('env.sh', content, 'text/plain')
        # .sh extension is blocked first
        assert result.is_blocked is True
        assert result.risk_level == 'blocked'

    def test_aws_key_in_yaml(self):
        """AWS credentials in YAML should be detected."""
        content = b"""
aws:
  access_key: AKIAIOSFODNN7EXAMPLE
  secret_key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
  region: us-east-1
"""
        result = scan_upload('config.yaml', content, 'application/yaml')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'AWS Access Key ID' in result.findings
        assert 'AWS Secret Access Key' in result.findings

    def test_github_pat_detected(self):
        """GitHub Personal Access Token should be detected."""
        content = b"""
# GitHub Actions config
GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuv12
"""
        result = scan_upload('workflow.yml', content, 'text/yaml')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'GitHub PAT' in result.findings

    def test_hardcoded_password_detected(self):
        """Hardcoded passwords should be detected."""
        content = b"""
const config = {
  database: {
    host: "localhost",
    password: "MySecretP@ssw0rd123",
    user: "admin"
  }
};
"""
        result = scan_upload('config.js', content, 'application/javascript')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'Hardcoded credential' in result.findings

    def test_private_key_detected(self):
        """Private SSH keys should be detected."""
        content = b"""
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyz
-----END RSA PRIVATE KEY-----
"""
        result = scan_upload('id_rsa', content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'Private key' in result.findings

    def test_openssh_private_key_detected(self):
        """OpenSSH format private keys should be detected."""
        content = b"""
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABFwAAAAdzc2gtcn
-----END OPENSSH PRIVATE KEY-----
"""
        result = scan_upload('id_ed25519', content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'high'
        assert 'Private key' in result.findings


class TestSafeFiles:
    """Test that safe files pass without issues."""

    def test_normal_yaml_safe(self):
        """Normal YAML config without secrets should be safe."""
        content = b"""
app:
  name: my-app
  version: 1.0.0
  settings:
    debug: false
    port: 8080
"""
        result = scan_upload('config.yaml', content, 'application/yaml')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'
        assert len(result.findings) == 0

    def test_json_data_safe(self):
        """JSON data files without secrets should be safe."""
        content = b'{"users": [{"name": "Alice", "role": "admin"}], "count": 1}'
        result = scan_upload('data.json', content, 'application/json')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'

    def test_text_file_safe(self):
        """Plain text files without secrets should be safe."""
        content = b"This is a normal text file.\nIt contains no secrets.\n"
        result = scan_upload('notes.txt', content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'

    def test_csv_file_safe(self):
        """CSV files without secrets should be safe."""
        content = b"name,age,city\nAlice,30,NYC\nBob,25,SF\n"
        result = scan_upload('data.csv', content, 'text/csv')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'


class TestBinaryFiles:
    """Test that binary files are not scanned."""

    def test_png_not_scanned(self):
        """PNG images should not be scanned."""
        content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x00' * 100
        result = scan_upload('image.png', content, 'image/png')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'
        # Binary files are not scanned, so even if they contained secrets, they'd pass

    def test_jpeg_not_scanned(self):
        """JPEG images should not be scanned."""
        content = b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 100
        result = scan_upload('photo.jpg', content, 'image/jpeg')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'

    def test_pdf_not_scanned(self):
        """PDF files should not be scanned (too large or binary)."""
        content = b'%PDF-1.4\n' + b'\x00' * 100
        result = scan_upload('document.pdf', content, 'application/pdf')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'


class TestLargeFiles:
    """Test that large files are not scanned."""

    def test_large_file_not_scanned(self):
        """Files larger than 1MB should not be scanned."""
        # Create a file just over 1MB with potential secrets
        large_content = b"password='secret123'\n" * 100000  # > 1MB
        result = scan_upload('large.txt', large_content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'
        # Large files are not scanned for performance


class TestErrorHandling:
    """Test error handling and fail-open behavior."""

    def test_scanner_never_raises(self):
        """Scanner should never raise exceptions, always return a result."""
        # Try various edge cases
        result1 = scan_upload('', b'', '')
        assert isinstance(result1, UploadScanResult)

        result2 = scan_upload('test.txt', b'\xff\xfe\xff\xfe', 'text/plain')
        assert isinstance(result2, UploadScanResult)

        # Invalid UTF-8 sequences
        result3 = scan_upload('invalid.txt', b'\x80\x81\x82', 'text/plain')
        assert isinstance(result3, UploadScanResult)
        assert result3.risk_level == 'safe'  # Fail-open on decode error

    def test_non_utf8_text_file_safe(self):
        """Non-UTF-8 text files should fail gracefully."""
        content = b'\xff\xfe\x00\x00invalid utf-8 \x80\x90'
        result = scan_upload('weird.txt', content, 'text/plain')
        assert result.is_blocked is False
        assert result.risk_level == 'safe'


class TestExtensionBasedScanning:
    """Test that file extensions trigger scanning even without content-type."""

    def test_yaml_extension_triggers_scan(self):
        """Files with .yaml extension should be scanned."""
        content = b'password: "MyP@ssw0rd123"'
        result = scan_upload('config.yaml', content, 'application/octet-stream')
        assert result.risk_level == 'high'

    def test_env_extension_triggers_scan(self):
        """.env files should be scanned."""
        content = b'SECRET_KEY=abcdefghij1234567890'
        result = scan_upload('.env', content, 'application/octet-stream')
        assert result.risk_level == 'high'

    def test_py_extension_triggers_scan(self):
        """.py files should be scanned."""
        content = b'API_KEY = "sk-1234567890abcdefghijklmno"'
        result = scan_upload('settings.py', content, 'application/octet-stream')
        assert result.risk_level == 'high'


class TestCaseInsensitivity:
    """Test case-insensitive pattern matching."""

    def test_password_case_insensitive(self):
        """Password patterns should match case-insensitively."""
        content1 = b'PASSWORD="SecretValue123"'
        content2 = b'password="SecretValue123"'
        content3 = b'Password="SecretValue123"'

        result1 = scan_upload('config1.txt', content1, 'text/plain')
        result2 = scan_upload('config2.txt', content2, 'text/plain')
        result3 = scan_upload('config3.txt', content3, 'text/plain')

        assert result1.risk_level == 'high'
        assert result2.risk_level == 'high'
        assert result3.risk_level == 'high'
