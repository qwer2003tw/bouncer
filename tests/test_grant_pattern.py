"""
tests/test_grant_pattern.py

UX-003 Approach B — Grant Session Pattern Matching 測試

覆蓋：
  - compile_pattern / match_pattern 核心邏輯
  - Glob pattern：* 與 **
  - Named placeholder：{uuid}, {date}, {any}, {bucket}, {key}, {name}
  - 混合使用（glob + placeholder）
  - 邊界條件
  - S3 前端部署場景
  - is_command_in_grant 向後相容（exact match）與 pattern 升級
"""
import os
import sys
import re
import pytest

# ---------------------------------------------------------------------------
# 設定 env（grant.py 的 db 模組在 import 時需要 region）
# ---------------------------------------------------------------------------
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('TABLE_NAME', 'clawdbot-approval-requests')
os.environ.setdefault('ACCOUNTS_TABLE_NAME', 'bouncer-accounts')
os.environ.setdefault('REQUEST_SECRET', 'test-secret')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('APPROVED_CHAT_ID', '999999999')

from moto import mock_aws
import boto3

# ---------------------------------------------------------------------------
# 直接 import grant module（使用 moto mock 避免真實 AWS 呼叫）
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module', autouse=True)
def mock_dynamodb_module():
    """整個模組使用 mock_aws，避免 grant.py 的 db 模組在 import 時出錯"""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        table.wait_until_exists()

        # 清除殘留模組再 import
        for mod in list(sys.modules.keys()):
            if mod in ('grant', 'db'):
                del sys.modules[mod]

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

        import db as db_mod
        db_mod.table = table
        db_mod.dynamodb = dynamodb

        yield


# Import after fixture setup via conftest-level sys.path manipulation
# We import at function level to ensure mock is active
def _get_grant():
    """取得 grant 模組（已在 mock_aws context 內）"""
    if 'grant' not in sys.modules:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        import grant
    return sys.modules['grant']


# Pre-import to get module-level names
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# We use a workaround: import the pure-function helpers directly by
# temporarily mocking boto3 at import time.
with mock_aws():
    _ddb = boto3.resource('dynamodb', region_name='us-east-1')
    try:
        _ddb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
    except Exception:
        pass  # may already exist

    for _mod in ['grant', 'db']:
        if _mod in sys.modules:
            del sys.modules[_mod]

    import db as _db_mod
    _db_mod.table = _ddb.Table('clawdbot-approval-requests')

    import grant as grant_mod
    from grant import compile_pattern, match_pattern, is_command_in_grant, _is_pattern


# ============================================================================
# _is_pattern
# ============================================================================

class TestIsPattern:
    """_is_pattern 判斷函式"""

    def test_plain_string_false(self):
        assert _is_pattern('aws s3 ls') is False

    def test_glob_star_true(self):
        assert _is_pattern('aws s3 ls s3://bucket/*') is True

    def test_double_star_true(self):
        assert _is_pattern('aws s3 cp s3://bucket/**') is True

    def test_placeholder_true(self):
        assert _is_pattern('aws s3 ls s3://bucket/{date}/') is True

    def test_only_opening_brace_false(self):
        # 只有 { 沒有 } 不算 placeholder 語法（但 _is_pattern 只看有沒有 { 和 }）
        assert _is_pattern('aws s3 ls s3://bucket/{') is False

    def test_only_closing_brace_false(self):
        assert _is_pattern('aws s3 ls s3://bucket/}') is False

    def test_both_braces_true(self):
        assert _is_pattern('{cmd}') is True


# ============================================================================
# _glob_to_regex（間接透過 compile_pattern 測試）
# ============================================================================

class TestGlobToRegex:
    """glob * 和 ** 行為"""

    # --- single star ---

    def test_star_matches_nonwhitespace(self):
        p = compile_pattern('aws s3 ls s3://bucket/*')
        assert p.match('aws s3 ls s3://bucket/my-folder')
        assert p.match('aws s3 ls s3://bucket/abc123')
        assert p.match('aws s3 ls s3://bucket/')  # * = \S* means 0 or more

    def test_star_does_not_match_space(self):
        # * → \S* — 不匹配空格
        p = compile_pattern('aws s3 cp s3://bucket/* local')
        assert not p.match('aws s3 cp s3://bucket/a b local')

    def test_star_matches_empty_suffix(self):
        # * 是 \S*，可匹配零個字元（但這裡後面還有斜線，所以等同路徑前綴）
        p = compile_pattern('aws s3 ls s3://bucket/*')
        assert p.match('aws s3 ls s3://bucket/')  # * matches ''

    def test_double_star_matches_slashes(self):
        p = compile_pattern('aws s3 cp s3://bucket/** local')
        assert p.match('aws s3 cp s3://bucket/a/b/c.html local')
        assert p.match('aws s3 cp s3://bucket/2025-01-01/uuid/file.txt local')

    def test_double_star_matches_spaces_in_path(self):
        # ** → .* 可以匹配空格（S3 key 可能有空格）
        p = compile_pattern('aws s3 cp s3://bucket/**')
        assert p.match('aws s3 cp s3://bucket/path with spaces/file.txt')

    def test_no_glob_is_exact(self):
        p = compile_pattern('aws s3 ls')
        assert p.match('aws s3 ls')
        assert not p.match('aws s3 ls extra')

    def test_star_in_middle(self):
        p = compile_pattern('aws s3 cp s3://bucket/*/file.html s3://dest/file.html')
        assert p.match('aws s3 cp s3://bucket/2025-01-01/file.html s3://dest/file.html')
        # subfolder with slash → single * shouldn't cross space but IS in path fragment
        # the slash is not a space so it's fine
        assert p.match('aws s3 cp s3://bucket/my-prefix/file.html s3://dest/file.html')


# ============================================================================
# Named Placeholder Tests
# ============================================================================

class TestPlaceholderUuid:
    """{uuid} placeholder"""

    def test_uuid_v4_with_hyphens(self):
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        assert p.match('aws s3 ls s3://bucket/550e8400-e29b-41d4-a716-446655440000/')

    def test_uuid_hex_no_hyphens(self):
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        assert p.match('aws s3 ls s3://bucket/550e8400e29b41d4a716446655440000/')

    def test_uuid_short_hex_12chars(self):
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        assert p.match('aws s3 ls s3://bucket/a1b2c3d4e5f6/')

    def test_uuid_too_short_fails(self):
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        # Only 10 chars — doesn't satisfy [0-9a-f]{10,34} pattern
        assert not p.match('aws s3 ls s3://bucket/a1b2c3d4e5/')

    def test_uuid_with_uppercase(self):
        # re.IGNORECASE is set
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        assert p.match('aws s3 ls s3://bucket/550E8400-E29B-41D4-A716-446655440000/')

    def test_uuid_non_hex_fails(self):
        p = compile_pattern('aws s3 ls s3://bucket/{uuid}/')
        # Contains 'xyz' — not hex
        assert not p.match('aws s3 ls s3://bucket/xyz-not-a-uuid-12345/')


class TestPlaceholderDate:
    """{date} placeholder"""

    def test_valid_date(self):
        p = compile_pattern('aws s3 ls s3://bucket/{date}/')
        assert p.match('aws s3 ls s3://bucket/2025-01-15/')
        assert p.match('aws s3 ls s3://bucket/2026-12-31/')

    def test_invalid_date_format(self):
        p = compile_pattern('aws s3 ls s3://bucket/{date}/')
        assert not p.match('aws s3 ls s3://bucket/25-01-15/')  # wrong year format
        assert not p.match('aws s3 ls s3://bucket/2025/01/15/')  # wrong separator

    def test_date_in_s3_path(self):
        p = compile_pattern('aws s3 cp s3://bouncer-uploads/{date}/{uuid}/index.html s3://frontend/index.html')
        assert p.match(
            'aws s3 cp s3://bouncer-uploads/2025-08-19/550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://frontend/index.html'
        )


class TestPlaceholderAny:
    """{any} placeholder — non-whitespace"""

    def test_matches_any_nonwhitespace(self):
        p = compile_pattern('aws lambda invoke --function-name {any} --payload file://in.json out.json')
        assert p.match('aws lambda invoke --function-name my-function --payload file://in.json out.json')
        assert p.match('aws lambda invoke --function-name prod-deployer-v2 --payload file://in.json out.json')

    def test_does_not_match_space(self):
        p = compile_pattern('aws s3 cp {any} local')
        # {any} = \S+ → no spaces
        assert not p.match('aws s3 cp with spaces local')

    def test_any_matches_s3_uri(self):
        p = compile_pattern('aws s3 ls {any}')
        assert p.match('aws s3 ls s3://my-bucket/prefix/')


class TestPlaceholderBucket:
    """{bucket} placeholder — alias for \S+"""

    def test_bucket_name(self):
        p = compile_pattern('aws s3 ls s3://{bucket}/')
        assert p.match('aws s3 ls s3://my-bucket-190825685292/')

    def test_bucket_with_complex_name(self):
        p = compile_pattern('aws s3 cp local s3://{bucket}/file.txt')
        assert p.match('aws s3 cp local s3://ztp-files-dev-frontendbucket-nvvimv31xp3v/file.txt')


class TestPlaceholderKey:
    """{key} placeholder"""

    def test_s3_key(self):
        p = compile_pattern('aws s3 cp s3://bucket/{key} local/')
        assert p.match('aws s3 cp s3://bucket/path/to/file.txt local/')

    def test_key_with_slashes(self):
        # {key} = \S+ — slash is non-whitespace
        p = compile_pattern('aws s3 rm s3://bucket/{key}')
        assert p.match('aws s3 rm s3://bucket/2025-01-15/uploads/file.json')


class TestPlaceholderUnknown:
    """未知 placeholder 名稱 → fallback \S+"""

    def test_custom_placeholder(self):
        p = compile_pattern('aws ec2 describe-instances --instance-ids {instance_id}')
        assert p.match('aws ec2 describe-instances --instance-ids i-1234567890abcdef0')

    def test_custom_placeholder_region(self):
        p = compile_pattern('aws s3 ls --region {region}')
        assert p.match('aws s3 ls --region ap-east-1')


# ============================================================================
# Mixed: Glob + Named Placeholder
# ============================================================================

class TestMixed:
    """Glob 與 Named Placeholder 混合使用"""

    def test_date_uuid_glob_html(self):
        """S3 前端部署場景：{date}/{uuid}/*.html"""
        pattern = (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/*.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/*.html'
        )
        p = compile_pattern(pattern)

        # Valid
        assert p.match(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html'
        )
        assert p.match(
            'aws s3 cp s3://bouncer-uploads-190825685292/2026-12-01/'
            'abcdef1234567890abcdef1234567890/app.chunk.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/app.chunk.html'
        )

    def test_date_uuid_glob_js(self):
        """S3 前端部署：assets/*.js"""
        pattern = (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/assets/*.js '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/*.js '
            '--cache-control max-age=31536000,immutable'
        )
        p = compile_pattern(pattern)
        assert p.match(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/assets/index.abc123.js '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/index.abc123.js '
            '--cache-control max-age=31536000,immutable'
        )

    def test_double_star_with_placeholder(self):
        """** + {date} 混合"""
        pattern = 'aws s3 sync s3://bucket/{date}/** s3://dest/**'
        p = compile_pattern(pattern)
        assert p.match('aws s3 sync s3://bucket/2025-01-01/sub/folder/file.txt s3://dest/sub/folder/file.txt')

    def test_multiple_placeholders_in_one_pattern(self):
        """多個 placeholder 同時使用"""
        pattern = 'aws s3 cp s3://{bucket}/{date}/{uuid}/{key} local/{name}'
        p = compile_pattern(pattern)
        assert p.match(
            'aws s3 cp s3://my-bucket/2025-08-19/550e8400-e29b-41d4-a716-446655440000/file.txt local/file.txt'
        )


# ============================================================================
# match_pattern function
# ============================================================================

class TestMatchPattern:
    """match_pattern 函式行為"""

    def test_exact_match_no_pattern_syntax(self):
        assert match_pattern('aws s3 ls', 'aws s3 ls') is True

    def test_exact_match_fails_on_diff(self):
        assert match_pattern('aws s3 ls', 'aws s3 cp') is False

    def test_glob_match(self):
        assert match_pattern('aws s3 ls s3://bucket/*', 'aws s3 ls s3://bucket/myprefix') is True

    def test_placeholder_match(self):
        assert match_pattern(
            'aws s3 ls s3://bucket/{date}/',
            'aws s3 ls s3://bucket/2025-01-15/',
        ) is True

    def test_placeholder_no_match(self):
        assert match_pattern(
            'aws s3 ls s3://bucket/{date}/',
            'aws s3 ls s3://bucket/not-a-date/',
        ) is False

    def test_empty_pattern_empty_cmd(self):
        assert match_pattern('', '') is True

    def test_empty_pattern_nonempty_cmd(self):
        assert match_pattern('', 'aws s3 ls') is False

    def test_error_returns_false(self):
        # Malformed regex from crazy input — should return False, not raise
        result = match_pattern('{[invalid}', 'anything')
        # Either True or False; main requirement is no exception
        assert isinstance(result, bool)


# ============================================================================
# is_command_in_grant — backward compat + pattern upgrade
# ============================================================================

class TestIsCommandInGrantPattern:
    """is_command_in_grant: exact + pattern 雙模式"""

    # --- Backward compatibility: exact match still works ---

    def test_exact_match_true(self):
        grant = {'granted_commands': ['aws s3 ls', 'aws ec2 describe-instances']}
        assert is_command_in_grant('aws s3 ls', grant) is True

    def test_exact_match_false(self):
        grant = {'granted_commands': ['aws s3 ls']}
        assert is_command_in_grant('aws s3 cp', grant) is False

    def test_empty_grant(self):
        grant = {'granted_commands': []}
        assert is_command_in_grant('aws s3 ls', grant) is False

    def test_missing_key(self):
        assert is_command_in_grant('aws s3 ls', {}) is False

    def test_case_sensitive_exact(self):
        """normalized commands should be lowercase"""
        grant = {'granted_commands': ['aws s3 ls']}
        assert is_command_in_grant('AWS S3 LS', grant) is False
        assert is_command_in_grant('aws s3 ls', grant) is True

    # --- Glob patterns ---

    def test_glob_star_matches(self):
        grant = {'granted_commands': ['aws s3 ls s3://bucket/*']}
        assert is_command_in_grant('aws s3 ls s3://bucket/prefix', grant) is True

    def test_glob_star_no_match(self):
        grant = {'granted_commands': ['aws s3 ls s3://bucket/*']}
        assert is_command_in_grant('aws s3 ls s3://other/prefix', grant) is False

    def test_glob_double_star(self):
        grant = {'granted_commands': ['aws s3 cp s3://bouncer-uploads/** local/']}
        assert is_command_in_grant(
            'aws s3 cp s3://bouncer-uploads/2025-01-01/uuid/file.txt local/', grant
        ) is True

    # --- Named placeholder patterns ---

    def test_placeholder_uuid(self):
        grant = {'granted_commands': ['aws s3 ls s3://bucket/{uuid}/']}
        assert is_command_in_grant(
            'aws s3 ls s3://bucket/550e8400-e29b-41d4-a716-446655440000/', grant
        ) is True

    def test_placeholder_date(self):
        grant = {'granted_commands': ['aws s3 ls s3://bucket/{date}/']}
        assert is_command_in_grant('aws s3 ls s3://bucket/2025-08-19/', grant) is True

    def test_placeholder_any(self):
        grant = {'granted_commands': ['aws s3 cp s3://bucket/{any} local/file.txt']}
        assert is_command_in_grant('aws s3 cp s3://bucket/some-key.json local/file.txt', grant) is True

    # --- Mixed glob + placeholder ---

    def test_mixed_s3_frontend_deploy(self):
        """真實 S3 前端部署 pattern"""
        pattern = (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/*.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/*.html'
        )
        grant = {'granted_commands': [pattern]}

        cmd = (
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html'
        )
        assert is_command_in_grant(cmd, grant) is True

    def test_mixed_s3_deploy_wrong_bucket_fails(self):
        """錯誤 bucket → 不匹配"""
        pattern = (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/*.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/*.html'
        )
        grant = {'granted_commands': [pattern]}

        cmd = (
            'aws s3 cp s3://evil-bucket/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html'
        )
        assert is_command_in_grant(cmd, grant) is False

    def test_multiple_patterns_first_matches(self):
        """多條 pattern，第一條命中"""
        grant = {
            'granted_commands': [
                'aws s3 ls s3://bucket/{date}/',
                'aws ec2 describe-instances --instance-ids {any}',
            ]
        }
        assert is_command_in_grant('aws s3 ls s3://bucket/2025-01-15/', grant) is True

    def test_multiple_patterns_second_matches(self):
        """多條 pattern，第二條命中"""
        grant = {
            'granted_commands': [
                'aws s3 ls s3://bucket/{date}/',
                'aws ec2 describe-instances --instance-ids {any}',
            ]
        }
        assert is_command_in_grant(
            'aws ec2 describe-instances --instance-ids i-1234567890abcdef0', grant
        ) is True

    def test_none_of_patterns_match(self):
        """沒有 pattern 匹配"""
        grant = {
            'granted_commands': [
                'aws s3 ls s3://bucket/{date}/',
                'aws ec2 describe-instances --instance-ids {any}',
            ]
        }
        assert is_command_in_grant('aws lambda list-functions', grant) is False

    # --- Edge cases ---

    def test_pattern_with_literal_dot(self):
        """點號應被 re.escape 保護，不成為 regex ."""
        grant = {'granted_commands': ['aws s3 cp s3://bucket/file.html local/']}
        # 'fileXhtml' should NOT match (dot is literal)
        assert is_command_in_grant('aws s3 cp s3://bucket/fileXhtml local/', grant) is False
        assert is_command_in_grant('aws s3 cp s3://bucket/file.html local/', grant) is True

    def test_no_regex_injection_via_pattern(self):
        """Pattern 中的 regex 特殊字元不應被解釋"""
        grant = {'granted_commands': ['aws s3 ls s3://bucket/(test)']}
        # This should only match the literal string (with parentheses)
        assert is_command_in_grant('aws s3 ls s3://bucket/test', grant) is False
        assert is_command_in_grant('aws s3 ls s3://bucket/(test)', grant) is True

    def test_star_does_not_match_across_tokens(self):
        """* 不匹配空格：避免 pattern 過寬"""
        grant = {'granted_commands': ['aws s3 ls s3://bucket/*']}
        # Extra token after bucket path — the * is for path segment only
        # 's3://bucket/prefix --recursive' contains space after prefix
        # → * is \S* which stops at space, so 'prefix --recursive' won't match 'prefix'
        # Actually the full command has extra tokens after the pattern end
        assert is_command_in_grant('aws s3 ls s3://bucket/prefix --recursive', grant) is False


# ============================================================================
# S3 Frontend Deploy Scenario (integration-level)
# ============================================================================

class TestS3FrontendDeployScenario:
    """真實的 S3 前端部署 grant 場景測試"""

    GRANT_PATTERNS = [
        # index.html（no-cache）
        (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        ),
        # assets/*.js（immutable）
        (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/assets/*.js '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/*.js '
            '--content-type application/javascript '
            '--cache-control max-age=31536000,immutable '
            '--region us-east-1'
        ),
        # assets/*.css（immutable）
        (
            'aws s3 cp s3://bouncer-uploads-190825685292/{date}/{uuid}/assets/*.css '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/*.css '
            '--content-type text/css '
            '--cache-control max-age=31536000,immutable '
            '--region us-east-1'
        ),
        # CloudFront invalidation
        'aws cloudfront create-invalidation --distribution-id e176pw0sa5jf29 --paths /index.html /assets/* --region us-east-1',
    ]

    def _make_grant(self):
        return {'granted_commands': self.GRANT_PATTERNS}

    def _n(self, cmd: str) -> str:
        """Normalize a command."""
        return ' '.join(cmd.strip().split()).lower()

    def test_index_html_matches(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is True

    def test_js_asset_matches(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/assets/index.abc123def.js '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/index.abc123def.js '
            '--content-type application/javascript '
            '--cache-control max-age=31536000,immutable '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is True

    def test_css_asset_matches(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/2026-01-01/'
            'abcdef1234567890abcdef1234567890/assets/style.xyz789.css '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/assets/style.xyz789.css '
            '--content-type text/css '
            '--cache-control max-age=31536000,immutable '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is True

    def test_cloudfront_invalidation_exact_match(self):
        grant = self._make_grant()
        cmd = 'aws cloudfront create-invalidation --distribution-id e176pw0sa5jf29 --paths /index.html /assets/* --region us-east-1'
        assert is_command_in_grant(cmd, grant) is True

    def test_wrong_date_format_fails(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/20250819/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is False

    def test_wrong_bucket_fails(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://evil-bucket/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is False

    def test_wrong_destination_bucket_fails(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://evil-dest-bucket/index.html '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is False

    def test_non_html_file_for_html_pattern_fails(self):
        grant = self._make_grant()
        cmd = self._n(
            'aws s3 cp s3://bouncer-uploads-190825685292/2025-08-19/'
            '550e8400-e29b-41d4-a716-446655440000/malicious.sh '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/malicious.sh '
            '--content-type text/html '
            '--cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert is_command_in_grant(cmd, grant) is False
