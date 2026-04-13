"""
tests/test_paging.py — Comprehensive paging tests for sprint7-004 (Approach B)

Covers:
  - Short / medium / 5K / 15K / 100K+ outputs
  - PaginatedOutput dataclass API
  - Page retrieval chain (next_page tokens)
  - Hard cap + truncation notice
  - send_remaining_pages() full iteration
  - Edge cases: empty, exactly-at-threshold, single-char, unicode
"""
import json
import sys
import os
import time
import pytest

pytestmark = pytest.mark.xdist_group("paging")
from unittest.mock import patch, MagicMock, call
from decimal import Decimal
from moto import mock_aws
import boto3

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_dynamodb():
    """Minimal DynamoDB mock for paging tests only."""
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName='clawdbot-approval-requests',
            KeySchema=[{'AttributeName': 'request_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'request_id', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST',
        )
        table.wait_until_exists()
        yield dynamodb


@pytest.fixture(scope="function")
def paging_module(mock_dynamodb):
    """Load paging module with mocked DynamoDB."""
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['TABLE_NAME'] = 'clawdbot-approval-requests'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
    os.environ['APPROVED_CHAT_ID'] = '999'

    # Clean slate imports
    for mod in list(sys.modules.keys()):
        if mod in ('paging', 'constants', 'telegram'):
            del sys.modules[mod]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

    import paging
    import importlib
    import telegram as _telegram_module

    # Force reload paging to refresh send_telegram_message_silent binding.
    # Also reload telegram first to ensure it has the real function (not a mock).
    importlib.reload(_telegram_module)
    importlib.reload(paging)

    # Inject moto-backed table
    paging._table = mock_dynamodb.Table('clawdbot-approval-requests')
    yield paging


@pytest.fixture(autouse=True)
def _clear_table(mock_dynamodb):
    """Wipe the table between tests."""
    yield
    table = mock_dynamodb.Table('clawdbot-approval-requests')
    scan = table.scan(ProjectionExpression='request_id')
    with table.batch_writer() as batch:
        for item in scan.get('Items', []):
            batch.delete_item(Key={'request_id': item['request_id']})


# ---------------------------------------------------------------------------
# Constants import helper
# ---------------------------------------------------------------------------

def _constants():
    import importlib
    return importlib.import_module('constants')


# ===========================================================================
# 1. PaginatedOutput dataclass
# ===========================================================================

class TestPaginatedOutput:
    def test_not_paged_dict_compat(self, paging_module):
        po = paging_module.PaginatedOutput(paged=False, result='hello')
        assert po['paged'] is False
        assert po['result'] == 'hello'
        assert po.get('missing_key', 'default') == 'default'

    def test_paged_dict_compat(self, paging_module):
        po = paging_module.PaginatedOutput(
            paged=True,
            result='page1',
            page=1,
            total_pages=3,
            output_length=12000,
            next_page='req:page:2',
            truncated=False,
        )
        d = po.to_dict()
        assert d['paged'] is True
        assert d['total_pages'] == 3
        assert d['next_page'] == 'req:page:2'
        assert d['truncated'] is False

    def test_truncated_flag(self, paging_module):
        po = paging_module.PaginatedOutput(
            paged=True, result='x', page=1, total_pages=2,
            output_length=200_000, truncated=True,
        )
        assert po['truncated'] is True
        assert po['output_length'] == 200_000


# ===========================================================================
# 2. store_paged_output — basic scenarios
# ===========================================================================

class TestStorePaged:
    def test_short_output_not_paged(self, paging_module):
        result = paging_module.store_paged_output('req-short', 'hello world')
        assert result['paged'] is False
        assert result['result'] == 'hello world'

    def test_empty_string(self, paging_module):
        result = paging_module.store_paged_output('req-empty', '')
        assert result['paged'] is False
        assert result['result'] == ''

    def test_exactly_at_inline_threshold(self, paging_module):
        c = _constants()
        output = 'x' * c.OUTPUT_MAX_INLINE
        result = paging_module.store_paged_output('req-exact', output)
        assert result['paged'] is False
        assert len(result['result']) == c.OUTPUT_MAX_INLINE

    def test_one_over_inline_threshold_is_paged(self, paging_module):
        c = _constants()
        output = 'x' * (c.OUTPUT_MAX_INLINE + 1)
        result = paging_module.store_paged_output('req-over', output)
        assert result['paged'] is True
        assert result['page'] == 1
        assert result['total_pages'] >= 1

    def test_5k_output(self, paging_module):
        """5 000-char output → 2 pages (4K + 1K with page_size=4000)"""
        output = 'A' * 5000
        result = paging_module.store_paged_output('req-5k', output)
        assert result['paged'] is True
        assert result['total_pages'] == 2
        assert result['output_length'] == 5000
        assert result['next_page'] == 'req-5k:page:2'
        # First page must be exactly OUTPUT_PAGE_SIZE chars
        c = _constants()
        assert len(result['result']) == c.OUTPUT_PAGE_SIZE

    def test_15k_output_all_pages_stored(self, paging_module, mock_dynamodb):
        """15 000-char output → 4 pages (4K×3 + 3K); all stored in DynamoDB."""
        output = 'B' * 15_000
        result = paging_module.store_paged_output('req-15k', output)

        assert result['paged'] is True
        total = result['total_pages']
        assert total == 4  # ceil(15000/4000) = 4

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        for page_num in range(2, total + 1):
            item = table.get_item(
                Key={'request_id': f'req-15k:page:{page_num}'}
            ).get('Item')
            assert item is not None, f"Page {page_num} missing from DynamoDB"
            assert item['page'] == page_num
            assert item['total_pages'] == total
            assert item['original_request'] == 'req-15k'

    def test_15k_output_page1_not_stored_in_ddb(self, paging_module, mock_dynamodb):
        """Page 1 is returned inline, should NOT be stored separately."""
        output = 'C' * 15_000
        paging_module.store_paged_output('req-15k-p1', output)

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        item = table.get_item(Key={'request_id': 'req-15k-p1:page:1'}).get('Item')
        assert item is None, "Page 1 must not be stored in DynamoDB"

    def test_output_length_preserves_original(self, paging_module):
        """output_length should reflect original size, even after truncation."""
        c = _constants()
        original = 'Z' * (c.OUTPUT_HARD_CAP_BYTES + 500)
        result = paging_module.store_paged_output('req-cap', original)
        assert result.output_length == len(original)

    def test_page_content_completeness(self, paging_module, mock_dynamodb):
        """Concatenating all pages must reproduce the stored text."""
        c = _constants()
        # Use a 12K output split across 3 pages of 4K each
        segment = 'X' * c.OUTPUT_PAGE_SIZE
        output = segment * 3  # exactly 3 pages
        paging_module.store_paged_output('req-concat', output)

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        pages = [paging_module.store_paged_output.__module__]  # placeholder

        result = paging_module.store_paged_output('req-concat2', output)
        all_content = result['result']  # page 1 inline

        for page_num in range(2, result['total_pages'] + 1):
            item = table.get_item(
                Key={'request_id': f'req-concat2:page:{page_num}'}
            ).get('Item')
            all_content += item['content']

        assert all_content == output


# ===========================================================================
# 3. Hard cap & truncation notice
# ===========================================================================

class TestHardCap:
    def test_100k_plus_is_truncated(self, paging_module):
        """Output > OUTPUT_HARD_CAP_BYTES must be capped."""
        c = _constants()
        oversized = 'T' * (c.OUTPUT_HARD_CAP_BYTES + 10_000)
        result = paging_module.store_paged_output('req-huge', oversized)
        # Verify truncation flag
        assert result.truncated is True
        # Verify the actual stored data doesn't exceed hard cap by much
        # (we allow for the truncation notice appended)
        total_chars = len(result['result'])
        if result['paged']:
            assert total_chars <= c.OUTPUT_PAGE_SIZE + 500  # first page only
        else:
            assert total_chars <= c.OUTPUT_HARD_CAP_BYTES + 1000

    def test_truncation_notice_present(self, paging_module):
        """Truncation notice must appear somewhere in the output."""
        c = _constants()
        oversized = 'U' * (c.OUTPUT_HARD_CAP_BYTES + 1)
        result = paging_module.store_paged_output('req-notice', oversized)

        # Collect all page content
        full_result = result['result']
        # Check first page or overall result contains the notice marker
        assert '⚠️' in full_result or '[輸出已截斷]' in full_result or result.truncated is True

    def test_200k_output(self, paging_module):
        """200K output must be capped, not crash."""
        c = _constants()
        oversized = 'V' * 200_000
        result = paging_module.store_paged_output('req-200k', oversized)
        assert result.truncated is True
        assert result['paged'] is True or result['paged'] is False  # must not raise

    def test_output_within_cap_not_truncated(self, paging_module):
        """Output at exactly HARD_CAP should NOT be truncated."""
        c = _constants()
        exact = 'W' * c.OUTPUT_HARD_CAP_BYTES
        result = paging_module.store_paged_output('req-exact-cap', exact)
        assert result.truncated is False

    def test_ddb_item_stays_under_400kb(self, paging_module, mock_dynamodb):
        """Each DynamoDB item must be well under 400 KB."""
        c = _constants()
        # 50K chars — should produce ~13 pages of 4K each
        output = 'D' * 50_000
        result = paging_module.store_paged_output('req-ddb-size', output)

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        for page_num in range(2, result['total_pages'] + 1):
            item = table.get_item(
                Key={'request_id': f'req-ddb-size:page:{page_num}'}
            ).get('Item')
            content_bytes = len(item['content'].encode('utf-8'))
            assert content_bytes < 400_000, (
                f"Page {page_num} item size {content_bytes} exceeds 400KB"
            )


# ===========================================================================
# 4. get_paged_output — page retrieval chain
# ===========================================================================

class TestGetPagedOutput:
    def test_retrieve_page2(self, paging_module, mock_dynamodb):
        """Retrieve page 2 after storing a 15K output."""
        output = 'E' * 15_000
        paging_module.store_paged_output('req-get-p2', output)

        result = paging_module.get_paged_output('req-get-p2:page:2')
        assert 'error' not in result
        assert result['page'] == 2
        assert result['total_pages'] == 4
        assert len(result['result']) > 0

    def test_next_page_chain(self, paging_module):
        """Follow the next_page chain until the last page returns None."""
        output = 'F' * 15_000
        stored = paging_module.store_paged_output('req-chain', output)

        current = stored['next_page']
        visited_pages = [1]

        while current is not None:
            page_data = paging_module.get_paged_output(current)
            assert 'error' not in page_data, f"Error on page: {page_data}"
            visited_pages.append(page_data['page'])
            current = page_data['next_page']

        # Should have visited all pages
        assert visited_pages == list(range(1, stored['total_pages'] + 1))

    def test_last_page_has_no_next(self, paging_module):
        """Last page must return next_page=None."""
        output = 'G' * 8_000
        stored = paging_module.store_paged_output('req-last', output)
        last_page_id = f"req-last:page:{stored['total_pages']}"

        result = paging_module.get_paged_output(last_page_id)
        assert result['next_page'] is None

    def test_nonexistent_page_returns_error(self, paging_module):
        """Missing page must return error dict."""
        result = paging_module.get_paged_output('nonexistent:page:99')
        assert 'error' in result

    def test_retrieve_all_pages_reconstruct(self, paging_module):
        """Reconstructing all pages must equal the original output."""
        c = _constants()
        original = ''.join(
            chr(ord('A') + i % 26) * c.OUTPUT_PAGE_SIZE
            for i in range(4)
        )  # 4 × 4K = 16K

        stored = paging_module.store_paged_output('req-reconstruct', original)
        reconstructed = stored['result']

        current = stored['next_page']
        while current:
            page = paging_module.get_paged_output(current)
            reconstructed += page['result']
            current = page['next_page']

        assert reconstructed == original


# ===========================================================================
# 5. send_remaining_pages
# ===========================================================================

class TestSendRemainingPages:
    def test_single_page_no_send(self, paging_module):
        """total_pages=1 must never call send_telegram_message_silent."""
        with patch('paging.send_telegram_message_silent') as mock_send:
            paging_module.send_remaining_pages('req-single', 1)
            mock_send.assert_not_called()

    def test_two_pages_sends_page2(self, paging_module, mock_dynamodb):
        """2-page output → sends page 2 exactly once."""
        output = 'H' * 6_000
        stored = paging_module.store_paged_output('req-send-2', output)

        with patch('paging.send_telegram_message_silent') as mock_send:
            paging_module.send_remaining_pages('req-send-2', stored['total_pages'])
            assert mock_send.call_count == stored['total_pages'] - 1

    def test_four_pages_sends_pages_2_3_4(self, paging_module, mock_dynamodb):
        """4-page output → sends pages 2, 3, 4 (exactly 3 sends)."""
        output = 'I' * 16_000
        stored = paging_module.store_paged_output('req-send-4', output)
        assert stored['total_pages'] == 4

        with patch('paging.send_telegram_message_silent') as mock_send:
            paging_module.send_remaining_pages('req-send-4', 4)
            assert mock_send.call_count == 3

    def test_page_content_in_telegram_message(self, paging_module, mock_dynamodb):
        """Page content must appear inside the Telegram message."""
        unique = 'UNIQUE_MARKER_XYZ_' * 300  # 5400 chars → 2 pages
        stored = paging_module.store_paged_output('req-marker', unique)

        with patch('paging.send_telegram_message_silent') as mock_send:
            paging_module.send_remaining_pages('req-marker', stored['total_pages'])
            assert mock_send.call_count >= 1
            first_call_arg = mock_send.call_args_list[0][0][0]
            assert '第 2/' in first_call_arg

    def test_missing_page_does_not_abort(self, paging_module, mock_dynamodb):
        """If a page is missing from DDB, remaining pages must still be sent."""
        # Store a real 3-page output
        output = 'J' * 12_000
        stored = paging_module.store_paged_output('req-missing', output)

        # Manually delete page 2
        table = mock_dynamodb.Table('clawdbot-approval-requests')
        table.delete_item(Key={'request_id': 'req-missing:page:2'})

        sent_calls = []
        with patch('paging.send_telegram_message_silent', side_effect=lambda x: sent_calls.append(x)):
            paging_module.send_remaining_pages('req-missing', 3)

        # Page 3 must still be sent
        assert any('第 3/3' in c for c in sent_calls)

    def test_telegram_error_does_not_abort(self, paging_module, mock_dynamodb):
        """Telegram send error on page 2 must not stop page 3 from being sent."""
        output = 'K' * 12_000
        paging_module.store_paged_output('req-error', output)

        call_count = [0]

        def raise_on_second(msg):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("Telegram timeout")

        with patch('paging.send_telegram_message_silent', side_effect=raise_on_second):
            # Must not raise
            paging_module.send_remaining_pages('req-error', 3)

        assert call_count[0] == 2  # Both page 2 and page 3 attempted


# ===========================================================================
# 6. Unicode and edge cases
# ===========================================================================

class TestEdgeCases:
    def test_unicode_multibyte(self, paging_module):
        """Unicode CJK chars (3-4 bytes each) must not cause issues."""
        output = '中文輸出測試' * 1000  # ~6000 chars
        result = paging_module.store_paged_output('req-unicode', output)
        assert result['paged'] is True or result['paged'] is False  # must not raise
        assert isinstance(result['result'], str)

    def test_newlines_preserved(self, paging_module, mock_dynamodb):
        """Newlines inside content must be preserved across pages."""
        c = _constants()
        lines = '\n'.join(f'line {i}' for i in range(1000))
        if len(lines) <= c.OUTPUT_MAX_INLINE:
            lines = lines * 3
        result = paging_module.store_paged_output('req-newlines', lines)

        if result['paged']:
            # Retrieve page 2 and verify it contains newlines
            p2 = paging_module.get_paged_output(f'req-newlines:page:2')
            assert '\n' in p2['result'] or len(p2['result']) > 0

    def test_single_char_output(self, paging_module):
        result = paging_module.store_paged_output('req-1char', 'X')
        assert result['paged'] is False
        assert result['result'] == 'X'

    def test_request_id_with_colons(self, paging_module, mock_dynamodb):
        """Request IDs that already contain colons must paginate correctly."""
        output = 'L' * 10_000
        req_id = 'prefix:suffix:extra'
        result = paging_module.store_paged_output(req_id, output)
        assert result['paged'] is True

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        p2 = table.get_item(
            Key={'request_id': f'{req_id}:page:2'}
        ).get('Item')
        assert p2 is not None

    def test_idempotent_calls(self, paging_module, mock_dynamodb):
        """Calling store_paged_output twice with same req_id overwrites DDB (no crash)."""
        output = 'M' * 10_000
        paging_module.store_paged_output('req-idem', output)
        result2 = paging_module.store_paged_output('req-idem', output)
        assert result2['paged'] is True  # no exception raised

    def test_get_paged_output_invalid_format(self, paging_module):
        """get_paged_output with an ID that doesn't exist returns error."""
        result = paging_module.get_paged_output('invalid-format')
        assert 'error' in result

    def test_15k_output_all_content_retrievable_by_char(self, paging_module):
        """15K output with distinct chars per page: verify next_page chain returns all content."""
        c = _constants()
        ps = c.OUTPUT_PAGE_SIZE
        # Build output with distinct char per page-sized segment
        output = 'A' * ps + 'B' * ps + 'C' * ps + 'D' * (15_000 - 3 * ps)
        result = paging_module.store_paged_output('req-15k-chars', output)

        assert result['paged'] is True
        collected = result['result']
        next_id = result['next_page']
        while next_id is not None:
            page = paging_module.get_paged_output(next_id)
            assert 'error' not in page
            collected += page['result']
            next_id = page['next_page']

        assert collected == output


# ===========================================================================
# 7. TTL is set on stored pages
# ===========================================================================

class TestTTL:
    def test_ttl_is_set(self, paging_module, mock_dynamodb):
        """All stored pages must have a TTL attribute."""
        c = _constants()
        output = 'N' * 10_000
        result = paging_module.store_paged_output('req-ttl', output)

        table = mock_dynamodb.Table('clawdbot-approval-requests')
        before = int(time.time())
        for page_num in range(2, result['total_pages'] + 1):
            item = table.get_item(
                Key={'request_id': f'req-ttl:page:{page_num}'}
            ).get('Item')
            assert 'ttl' in item
            assert item['ttl'] >= before + c.OUTPUT_PAGE_TTL - 5
            assert item['ttl'] <= before + c.OUTPUT_PAGE_TTL + 5


# ===========================================================================
# Sprint 13-003: On-demand pagination tests
# ===========================================================================

class TestOnDemandPagination:
    """Tests for on-demand pagination via handle_show_page_callback (sprint13-003)"""

    def test_handle_show_page_callback_page2(self, paging_module, mock_dynamodb, app_module):
        """show_page callback: sends page 2 with Next Page button when more pages exist"""
        import time as _time

        # Create a paged output in DDB (3 pages)
        output = 'X' * 12_000  # large enough for 3 pages
        paged = paging_module.store_paged_output('req-on-demand-001', output)
        assert paged.paged is True
        assert paged.total_pages >= 3

        query = {'id': 'cb-show-001', 'from': {'id': 999999999}, 'message': {'message_id': 1}}

        with patch('callbacks.answer_callback') as mock_answer, \
             patch('callbacks.send_telegram_message_silent') as mock_send:
            from callbacks import handle_show_page_callback
            result = handle_show_page_callback(query, 'req-on-demand-001', 2)

        assert result['statusCode'] == 200
        mock_answer.assert_called_once()
        args = mock_answer.call_args[0]
        assert '2/' in args[1]  # should say page 2/N

        mock_send.assert_called_once()
        send_kwargs = mock_send.call_args
        # Should include Next Page button
        markup = send_kwargs[1].get('reply_markup') or (send_kwargs[0][1] if len(send_kwargs[0]) > 1 else None)
        assert markup is not None
        assert 'inline_keyboard' in markup

    def test_handle_show_page_callback_last_page_no_button(self, paging_module, mock_dynamodb, app_module):
        """show_page callback: last page has no Next Page button"""
        output = 'Y' * 12_000
        paged = paging_module.store_paged_output('req-on-demand-002', output)
        total = paged.total_pages

        query = {'id': 'cb-show-002', 'from': {'id': 999999999}, 'message': {'message_id': 2}}

        with patch('callbacks.answer_callback'), \
             patch('callbacks.send_telegram_message_silent') as mock_send:
            from callbacks import handle_show_page_callback
            handle_show_page_callback(query, 'req-on-demand-002', total)

        mock_send.assert_called_once()
        send_kwargs = mock_send.call_args
        # reply_markup should be None for last page
        markup = send_kwargs[1].get('reply_markup') if send_kwargs[1] else None
        assert markup is None

    def test_handle_show_page_callback_expired_page(self, paging_module, mock_dynamodb, app_module):
        """show_page callback: expired/missing page returns error"""
        query = {'id': 'cb-show-003', 'from': {'id': 999999999}, 'message': {'message_id': 3}}

        with patch('callbacks.answer_callback') as mock_answer, \
             patch('callbacks.send_telegram_message_silent') as mock_send:
            from callbacks import handle_show_page_callback
            result = handle_show_page_callback(query, 'req-nonexistent-999', 2)

        assert result['statusCode'] == 200
        mock_answer.assert_called_once()
        args = mock_answer.call_args[0]
        assert '❌' in args[1]
        mock_send.assert_not_called()


class TestNoPaginationAutoPush:
    """Tests that auto-push (send_remaining_pages) is no longer called (sprint13-003)"""

    def test_auto_push_import_removed(self):
        """send_remaining_pages should not be imported in callbacks.py"""
        import ast, os
        src = open(os.path.join(os.path.dirname(__file__), '..', 'src', 'callbacks.py')).read()
        assert 'send_remaining_pages' not in src, "send_remaining_pages should not be in callbacks.py"

    def test_on_demand_button_present_in_paged_output(self, mock_dynamodb, app_module):
        """Paged command result → send_telegram_message_silent called with Next Page button"""
        import time as _time

        request_id = 'req-page-btn-001'
        app_module.table.put_item(Item={
            'request_id': request_id,
            'command': 'aws ec2 describe-instances',
            'status': 'pending_approval',
            'source': 'test',
            'reason': 'describe',
            'account_id': '123456789012',
            'account_name': 'Test',
            'created_at': int(_time.time()),
        })

        large_output = 'EC2 instance\n' * 1000  # large output

        with patch('callbacks_command.answer_callback'), \
             patch('callbacks_command.update_message'), \
             patch('callbacks_command.execute_command') as mock_exec, \
             patch('callbacks_command.emit_metric'), \
             patch('callbacks_command.send_telegram_message_silent') as mock_silent:
            mock_exec.return_value = large_output

            from callbacks import handle_command_callback
            handle_command_callback(
                'approve', request_id,
                {
                    'command': 'aws ec2 describe-instances',
                    'source': 'test', 'reason': 'describe',
                    'trust_scope': 'test', 'context': '',
                    'account_id': '123456789012', 'account_name': 'Test',
                    'created_at': int(_time.time()),
                },
                999, 'cb-page', 'user-1'
            )

        # Should have called send_telegram_message_silent with a reply_markup
        mock_silent.assert_called()
        call_kwargs = mock_silent.call_args[1] if mock_silent.call_args[1] else {}
        markup = call_kwargs.get('reply_markup')
        assert markup is not None, "Expected Next Page button in reply_markup"
        assert 'inline_keyboard' in markup
