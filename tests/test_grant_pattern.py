"""
Bouncer - Grant Session Pattern Matching 測試 (Approach C)
測試導向：完整覆蓋 glob pattern matching 的所有 edge cases

覆蓋：
- _match_command_pattern (低層 fnmatch 邏輯)
- is_command_in_grant (整合層)
- 共 15+ test cases
"""
import sys
import os
import time
import pytest
from unittest.mock import patch, MagicMock

from moto import mock_aws
import boto3


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dynamodb():
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'request_id', 'AttributeType': 'S'},
                {'AttributeName': 'status', 'AttributeType': 'S'},
                {'AttributeName': 'created_at', 'AttributeType': 'N'},
                {'AttributeName': 'source', 'AttributeType': 'S'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'status-created-index',
                    'KeySchema': [
                        {'AttributeName': 'status', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
                {
                    'IndexName': 'source-created-index',
                    'KeySchema': [
                        {'AttributeName': 'source', 'KeyType': 'HASH'},
                        {'AttributeName': 'created_at', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                },
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture
def grant_module(mock_dynamodb):
    """載入 grant 模組並注入 mock DynamoDB"""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['DEFAULT_ACCOUNT_ID'] = '111111111111'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['ACCOUNTS_TABLE_NAME'] = 'bouncer-accounts'
    os.environ['REQUEST_SECRET'] = 'test-secret'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999999999'
    os.environ['GRANT_SESSION_ENABLED'] = 'true'

    modules_to_clear = [
        'grant', 'db', 'constants', 'trust', 'commands', 'compliance_checker',
        'risk_scorer', 'mcp_tools', 'mcp_execute', 'mcp_upload', 'mcp_admin',
        'notifications', 'telegram', 'app', 'utils', 'accounts', 'rate_limit',
        'paging', 'callbacks', 'smart_approval', 'tool_schema', 'metrics',
    ]
    for mod in modules_to_clear:
        if mod in sys.modules:
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import db
    db.table = mock_dynamodb.Table('clawdbot-approval-requests')
    db.dynamodb = mock_dynamodb

    import grant
    yield grant

    sys.path.pop(0)


# ============================================================================
# Helper: build a grant dict with given patterns
# ============================================================================

def _grant_with_patterns(*patterns):
    """快速建立含 granted_commands 的 mock grant dict"""
    return {'granted_commands': list(patterns)}


# ============================================================================
# TestMatchCommandPattern — 低層 fnmatch 邏輯
# ============================================================================

class TestMatchCommandPattern:
    """_match_command_pattern 單元測試"""

    # TC-01: 精確匹配仍有效（向下相容）
    def test_exact_match_valid(self, grant_module):
        """精確 pattern 不含 glob 字元，應等同原本 exact match"""
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://my-bucket',
            'aws s3 ls s3://my-bucket',
        ) is True

    # TC-02: * 匹配 UUID segment
    def test_star_matches_uuid_segment(self, grant_module):
        """* 應可比對含 UUID 的動態路徑 segment"""
        uuid = '550e8400-e29b-41d4-a716-446655440000'
        cmd = f'aws s3 cp s3://my-bucket/uploads/{uuid}/report.pdf /tmp/report.pdf'
        pattern = 'aws s3 cp s3://my-bucket/uploads/*/report.pdf /tmp/report.pdf'
        assert grant_module._match_command_pattern(cmd, pattern) is True

    # TC-03: * 匹配日期 segment
    def test_star_matches_date_segment(self, grant_module):
        """* 應可比對含日期（YYYY-MM-DD）的路徑 segment"""
        cmd = 'aws s3 cp s3://logs/2026-02-25/app.log /tmp/app.log'
        pattern = 'aws s3 cp s3://logs/*/app.log /tmp/app.log'
        assert grant_module._match_command_pattern(cmd, pattern) is True

    # TC-04: ** 匹配含空格的路徑（跨 segment）
    def test_double_star_matches_path_with_spaces(self, grant_module):
        """** 等同 *，可匹配含空格的多 token 路徑"""
        cmd = 'aws s3 cp s3://bucket/path/to/deep/file.txt /tmp/file.txt'
        pattern = 'aws s3 cp s3://bucket/** /tmp/file.txt'
        assert grant_module._match_command_pattern(cmd, pattern) is True

    # TC-05: Pattern 不匹配 → 回傳 False
    def test_pattern_no_match_returns_false(self, grant_module):
        """命令與 pattern 不吻合時應回傳 False"""
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://bucket-a',
            'aws s3 ls s3://bucket-b',
        ) is False

    # TC-07: Pattern 部分匹配（不算 match）
    def test_partial_match_not_counted(self, grant_module):
        """pattern 比命令短（前綴）不算 match，fnmatch 要整體吻合"""
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://my-bucket/very/long/path',
            'aws s3 ls',  # no wildcard, just prefix
        ) is False

    # TC-09: 大小寫敏感性（normalize 後皆小寫，應不敏感）
    def test_case_insensitive_after_normalize(self, grant_module):
        """命令和 pattern 都被 normalize 為小寫，大小寫應不影響比對"""
        assert grant_module._match_command_pattern(
            'aws ec2 describe-instances',   # already lowercase
            'AWS EC2 DESCRIBE-INSTANCES',   # uppercase pattern
        ) is True

    # TC-13: ? 匹配單一字元
    def test_question_mark_matches_single_char(self, grant_module):
        """? 應只比對一個任意字元"""
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://bucket-a',
            'aws s3 ls s3://bucket-?',
        ) is True
        # ? does NOT match two chars
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://bucket-ab',
            'aws s3 ls s3://bucket-?',
        ) is False

    # TC-14: 命令長度邊界 — 空命令
    def test_empty_command_no_match(self, grant_module):
        """空命令字串不應匹配任何非空 pattern"""
        assert grant_module._match_command_pattern('', 'aws s3 ls') is False

    # TC-14b: 命令長度邊界 — 非常長的命令
    def test_very_long_command_with_wildcard(self, grant_module):
        """超長命令字串也應正確比對 wildcard pattern"""
        long_suffix = 'x' * 500
        cmd = f'aws s3 cp s3://bucket/{long_suffix} /tmp/out'
        pattern = 'aws s3 cp s3://bucket/* /tmp/out'
        assert grant_module._match_command_pattern(cmd, pattern) is True

    # TC-15: Pattern 本身含空格（normalize 後保留單一空格，正常比對）
    def test_pattern_with_spaces_normalized(self, grant_module):
        """Pattern 多餘空格被 normalize，應正常比對對應命令"""
        assert grant_module._match_command_pattern(
            'aws s3 ls s3://my-bucket',
            '  aws   s3  ls  s3://my-bucket  ',  # extra whitespace
        ) is True

    # TC-10: 特殊字元在 non-pattern 部分（--output, --region 等）
    def test_special_chars_in_non_pattern_part(self, grant_module):
        """含 -- 旗標的命令應可精確比對或配合 * 使用"""
        # Exact match with flags
        assert grant_module._match_command_pattern(
            'aws ec2 describe-instances --output json --region us-east-1',
            'aws ec2 describe-instances --output json --region us-east-1',
        ) is True
        # Wildcard for dynamic values
        assert grant_module._match_command_pattern(
            'aws ec2 describe-instances --output json --region ap-east-1',
            'aws ec2 describe-instances --output json --region *',
        ) is True


# ============================================================================
# TestIsCommandInGrant — 整合層（多 pattern）
# ============================================================================

class TestIsCommandInGrant:
    """is_command_in_grant 測試（支援 pattern list）"""

    # TC-01: 精確匹配仍有效
    def test_exact_match_backward_compat(self, grant_module):
        """無 glob 字元的 pattern 應維持原本 exact match 行為"""
        grant = _grant_with_patterns('aws s3 ls', 'aws ec2 describe-instances')
        assert grant_module.is_command_in_grant('aws s3 ls', grant) is True
        assert grant_module.is_command_in_grant('aws ec2 describe-instances', grant) is True
        assert grant_module.is_command_in_grant('aws s3 cp', grant) is False

    # TC-05: Pattern 不匹配 → fallthrough
    def test_no_match_returns_false(self, grant_module):
        """命令不在任何 pattern → 應回傳 False（fallthrough）"""
        grant = _grant_with_patterns('aws s3 ls s3://bucket-a')
        assert grant_module.is_command_in_grant('aws s3 ls s3://bucket-b', grant) is False

    # TC-06: 空 grant commands list
    def test_empty_grant_commands(self, grant_module):
        """granted_commands 為空 list → 任何命令都不匹配"""
        assert grant_module.is_command_in_grant('aws s3 ls', _grant_with_patterns()) is False
        assert grant_module.is_command_in_grant('', _grant_with_patterns()) is False

    # TC-08: 多個 pattern，第一個不匹配第二個匹配
    def test_second_pattern_matches(self, grant_module):
        """第一個 pattern 不中，第二個中，應回傳 True"""
        grant = _grant_with_patterns(
            'aws s3 ls s3://bucket-x',       # won't match
            'aws s3 ls s3://bucket-*',        # should match
        )
        assert grant_module.is_command_in_grant('aws s3 ls s3://bucket-prod', grant) is True

    # TC-11: 前端部署 s3 cp 場景（realistic）
    def test_frontend_deploy_s3_cp_scenario(self, grant_module):
        """模擬前端部署：s3 cp 含日期 + UUID 路徑"""
        # pattern 授權任意 date/uuid 下的 index.html
        grant = _grant_with_patterns(
            'aws s3 cp s3://bouncer-uploads-190825685292/*/*/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html --cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1',
        )
        cmd = (
            'aws s3 cp s3://bouncer-uploads-190825685292/2026-02-25/'
            '550e8400-e29b-41d4-a716-446655440000/index.html '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--content-type text/html --cache-control no-cache,no-store,must-revalidate '
            '--region us-east-1'
        )
        assert grant_module.is_command_in_grant(
            grant_module.normalize_command(cmd), grant
        ) is True

    # TC-12: Grant allow_repeat + pattern matching 組合
    # (allow_repeat 設定在 grant 物件層，is_command_in_grant 只負責匹配)
    # 這裡驗證 pattern match 在 allow_repeat grant 中同樣有效
    def test_allow_repeat_grant_with_pattern(self, grant_module):
        """allow_repeat grant 中 pattern matching 應同樣有效"""
        grant = {
            'granted_commands': ['aws s3 cp s3://bucket/*/file.txt /tmp/file.txt'],
            'allow_repeat': True,
        }
        assert grant_module.is_command_in_grant(
            'aws s3 cp s3://bucket/uuid-1234/file.txt /tmp/file.txt', grant
        ) is True
        assert grant_module.is_command_in_grant(
            'aws s3 cp s3://bucket/uuid-5678/file.txt /tmp/file.txt', grant
        ) is True
        # Different filename should NOT match
        assert grant_module.is_command_in_grant(
            'aws s3 cp s3://bucket/uuid-1234/other.txt /tmp/file.txt', grant
        ) is False

    # TC-09 (整合): 大小寫敏感性
    def test_case_insensitive_matching(self, grant_module):
        """granted pattern 大寫應能匹配正規化（小寫）命令"""
        grant = _grant_with_patterns('AWS S3 LS S3://MY-BUCKET')
        # normalize_command 會將命令轉小寫
        normalized = grant_module.normalize_command('aws s3 ls s3://my-bucket')
        assert grant_module.is_command_in_grant(normalized, grant) is True

    # Missing key fallback
    def test_missing_granted_commands_key(self, grant_module):
        """grant dict 缺 granted_commands key → 應安全回傳 False"""
        assert grant_module.is_command_in_grant('aws s3 ls', {}) is False

    # Pattern with ? single char
    def test_question_mark_in_grant_pattern(self, grant_module):
        """? wildcard 在 granted_commands pattern 中應能比對單一字元"""
        grant = _grant_with_patterns('aws s3 ls s3://bucket-?')
        assert grant_module.is_command_in_grant('aws s3 ls s3://bucket-a', grant) is True
        assert grant_module.is_command_in_grant('aws s3 ls s3://bucket-1', grant) is True
        assert grant_module.is_command_in_grant('aws s3 ls s3://bucket-ab', grant) is False

    # TC-03 (整合): * 匹配日期 segment
    def test_star_date_segment_in_grant(self, grant_module):
        """Grant pattern 中 * 應可比對日期段"""
        grant = _grant_with_patterns('aws s3 sync s3://logs/*/ /tmp/logs/')
        assert grant_module.is_command_in_grant(
            'aws s3 sync s3://logs/2026-02-25/ /tmp/logs/', grant
        ) is True
        assert grant_module.is_command_in_grant(
            'aws s3 sync s3://logs/2025-12-31/ /tmp/logs/', grant
        ) is True

    # TC-02 (整合): * 匹配 UUID segment
    def test_star_uuid_segment_in_grant(self, grant_module):
        """Grant pattern 中 * 應可比對 UUID 段"""
        grant = _grant_with_patterns(
            'aws s3 cp s3://uploads/*/report.pdf s3://archive/report.pdf'
        )
        uuid = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
        assert grant_module.is_command_in_grant(
            f'aws s3 cp s3://uploads/{uuid}/report.pdf s3://archive/report.pdf',
            grant,
        ) is True

    # TC-04 (整合): ** 跨 segment 匹配
    def test_double_star_cross_segment(self, grant_module):
        """** 應能跨越多個路徑 segment"""
        grant = _grant_with_patterns('aws s3 cp s3://bucket/** /tmp/out')
        # Deep path
        assert grant_module.is_command_in_grant(
            'aws s3 cp s3://bucket/a/b/c/d/file.txt /tmp/out', grant
        ) is True

    # TC-15 (整合): Pattern 本身含空格（多餘空白 normalize 後正常比對）
    def test_pattern_with_extra_whitespace(self, grant_module):
        """Pattern 含多餘空白，normalize 後應正常比對命令"""
        grant = _grant_with_patterns('  aws   s3   ls   s3://my-bucket  ')
        assert grant_module.is_command_in_grant('aws s3 ls s3://my-bucket', grant) is True

    # TC-07 (整合): Pattern 部分匹配（前綴不算 match）
    def test_prefix_only_not_a_match(self, grant_module):
        """Pattern 是命令的前綴（無 wildcard）不算 match"""
        grant = _grant_with_patterns('aws s3')  # no wildcard, just prefix
        assert grant_module.is_command_in_grant('aws s3 ls s3://my-bucket', grant) is False


# ============================================================================
# TestPatternMatchingWithApprove — 端對端（含 DynamoDB approve 流程）
# ============================================================================

class TestPatternMatchingWithApprove:
    """End-to-end：pattern 存入 DynamoDB 後 approve，再用 is_command_in_grant 驗證"""

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_pattern_grant_approved_and_matched(
        self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module
    ):
        """建立含 pattern 的 grant，批准後，用 is_command_in_grant 驗證比對"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        # 建立 grant，命令 pattern 含 *（UUID path）
        pattern_cmd = (
            'aws s3 cp s3://bouncer-uploads-190825685292/*/* '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--region us-east-1'
        )
        result = grant_module.create_grant_request(
            commands=[pattern_cmd],
            reason='前端部署',
            source='Private Bot',
            account_id='111111111111',
        )
        grant_id = result['grant_id']

        # Approve
        grant = grant_module.approve_grant(grant_id, '999999999', mode='all')
        assert grant is not None
        assert grant['status'] == 'active'

        # 驗證：動態命令應匹配 pattern
        real_cmd = (
            'aws s3 cp s3://bouncer-uploads-190825685292/2026-02-25/'
            '550e8400-e29b-41d4-a716-446655440000 '
            's3://ztp-files-dev-frontendbucket-nvvimv31xp3v/index.html '
            '--region us-east-1'
        )
        normalized = grant_module.normalize_command(real_cmd)
        assert grant_module.is_command_in_grant(normalized, grant) is True

    @patch('risk_scorer.calculate_risk')
    @patch('trust.is_trust_excluded', return_value=False)
    @patch('commands.is_blocked', return_value=False)
    @patch('compliance_checker.check_compliance', return_value=(True, None))
    def test_allow_repeat_with_pattern_multiple_uuids(
        self, mock_compliance, mock_blocked, mock_excluded, mock_risk, grant_module
    ):
        """allow_repeat grant + pattern：多個 UUID 路徑都能匹配"""
        mock_risk_result = MagicMock()
        mock_risk_result.score = 20
        mock_risk.return_value = mock_risk_result

        pattern_cmd = 'aws s3 cp s3://uploads/*/file.pdf /tmp/file.pdf'
        result = grant_module.create_grant_request(
            commands=[pattern_cmd],
            reason='批次下載',
            source='Private Bot',
            account_id='111111111111',
            allow_repeat=True,
        )
        grant_id = result['grant_id']
        grant = grant_module.approve_grant(grant_id, '999999999')
        assert grant['allow_repeat'] is True

        uuids = [
            '550e8400-e29b-41d4-a716-446655440000',
            'f47ac10b-58cc-4372-a567-0e02b2c3d479',
            '6ba7b810-9dad-11d1-80b4-00c04fd430c8',
        ]
        for u in uuids:
            cmd = grant_module.normalize_command(f'aws s3 cp s3://uploads/{u}/file.pdf /tmp/file.pdf')
            assert grant_module.is_command_in_grant(cmd, grant) is True, f"UUID {u} should match"
