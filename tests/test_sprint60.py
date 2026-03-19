"""Sprint 60 test suite.

s60-008: template_s3_url format validation
"""
from src.deployer import validate_template_s3_url


class TestValidateTemplateS3Url:
    """Test validate_template_s3_url format validation (s60-008)."""

    def test_valid_virtual_hosted_style_url(self):
        """Valid virtual-hosted-style S3 URL."""
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""

    def test_valid_path_style_url(self):
        """Valid path-style S3 URL."""
        url = "https://s3.amazonaws.com/my-bucket/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""

    def test_valid_dash_region_url(self):
        """Valid S3 URL with dash-region format."""
        url = "https://s3-us-east-1.amazonaws.com/my-bucket/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""

    def test_empty_url(self):
        """Empty URL should be invalid."""
        is_valid, reason = validate_template_s3_url("")
        assert is_valid is False
        assert "empty" in reason.lower()

    def test_s3_protocol_url(self):
        """s3:// protocol should be invalid (CloudFormation requires https://)."""
        url = "s3://my-bucket/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is False
        assert "https" in reason.lower()

    def test_http_url(self):
        """http:// (non-secure) should be invalid."""
        url = "http://my-bucket.s3.us-east-1.amazonaws.com/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is False
        assert "https" in reason.lower()

    def test_no_s3_domain(self):
        """URL without S3 domain should be invalid."""
        url = "https://example.com/template.yaml"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is False
        assert "s3 domain" in reason.lower()

    def test_url_too_long(self):
        """URL exceeding 1024 characters should be invalid."""
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/" + "x" * 1020
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is False
        assert "long" in reason.lower()

    def test_valid_url_at_max_length(self):
        """Valid URL at exactly 1024 characters should be valid."""
        # Build URL to exactly 1024 chars
        base = "https://my-bucket.s3.us-east-1.amazonaws.com/"
        path = "x" * (1024 - len(base))
        url = base + path
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""

    def test_valid_url_with_query_params(self):
        """Valid S3 URL with query parameters."""
        url = "https://my-bucket.s3.us-east-1.amazonaws.com/template.yaml?versionId=abc123"
        is_valid, reason = validate_template_s3_url(url)
        assert is_valid is True
        assert reason == ""
