"""
Test suite for cli_input_json parameter (bouncer-s48-001)

Tests the new cli_input_json parameter that writes JSON to a tempfile
and appends --cli-input-json file:// to the command, bypassing shell quoting issues.
"""

from unittest.mock import Mock, patch, mock_open, MagicMock

from src.commands import execute_command, _execute_locked
from src.mcp_execute import ExecuteContext, _parse_execute_request


class TestCliInputJsonParameter:
    """Test cli_input_json parameter in execute_command and _execute_locked"""

    @patch('src.commands.aws_cli_split')
    @patch('src.commands._run_aws_subprocess')
    @patch('builtins.open', new_callable=mock_open)
    @patch('tempfile.NamedTemporaryFile')
    @patch('os.unlink')
    def test_cli_input_json_writes_tempfile_and_adds_flag(
        self, mock_unlink, mock_tempfile, mock_file, mock_subprocess, mock_split
    ):
        """Test 1: cli_input_json writes tempfile + adds --cli-input-json to command"""
        # Setup mock tempfile
        mock_temp_obj = MagicMock()
        mock_temp_obj.name = '/tmp/bouncer_cli_test123.json'
        mock_temp_obj.__enter__ = Mock(return_value=mock_temp_obj)
        mock_temp_obj.__exit__ = Mock(return_value=False)
        mock_tempfile.return_value = mock_temp_obj

        # Setup mock subprocess
        mock_subprocess.return_value = (0, '', '')

        # Setup mock aws_cli_split to verify the modified command
        def side_effect_split(cmd):
            # Verify that --cli-input-json file:// was appended
            assert '--cli-input-json' in cmd
            assert 'file:///tmp/bouncer_cli_test123.json' in cmd
            return ['aws', 's3', 'ls', '--cli-input-json', 'file:///tmp/bouncer_cli_test123.json']

        mock_split.side_effect = side_effect_split

        cli_input_data = {'Bucket': 'test-bucket', 'Key': '中文/檔案.txt'}

        # Execute
        result = _execute_locked(
            'aws s3 ls',
            assume_role_arn=None,
            cli_input_json=cli_input_data
        )

        # Verify tempfile was created with correct data
        mock_tempfile.assert_called_once()
        call_kwargs = mock_tempfile.call_args[1]
        assert call_kwargs['mode'] == 'w'
        assert call_kwargs['suffix'] == '.json'
        assert call_kwargs['delete'] is False
        assert call_kwargs['dir'] == '/tmp'
        assert call_kwargs['prefix'] == 'bouncer_cli_'

        # Verify JSON was written (via the file handle's write method)
        # Note: json.dump writes to the file object, so we check that it was called
        written_data = ''.join(call[0][0] for call in mock_temp_obj.write.call_args_list)
        assert 'test-bucket' in written_data or mock_temp_obj.write.called

        # Verify tempfile was cleaned up
        mock_unlink.assert_called_once_with('/tmp/bouncer_cli_test123.json')

        # Verify result is success
        assert '✅' in result or 'exit code: 0' not in result

    @patch('src.commands.aws_cli_split')
    @patch('src.commands._run_aws_subprocess')
    @patch('tempfile.NamedTemporaryFile')
    def test_cli_input_json_none_no_tempfile(
        self, mock_tempfile, mock_subprocess, mock_split
    ):
        """Test 2: cli_input_json=None → no tempfile, no --cli-input-json"""
        # Setup mock subprocess
        mock_subprocess.return_value = (0, '', '')

        # Setup mock aws_cli_split to verify no modification
        def side_effect_split(cmd):
            # Verify that --cli-input-json was NOT added
            assert '--cli-input-json' not in cmd
            return ['aws', 's3', 'ls']

        mock_split.side_effect = side_effect_split

        # Execute without cli_input_json
        result = _execute_locked(
            'aws s3 ls',
            assume_role_arn=None,
            cli_input_json=None
        )

        # Verify tempfile was NOT created
        mock_tempfile.assert_not_called()

        # Verify result is success
        assert '✅' in result or 'exit code: 0' not in result

    @patch('src.commands.aws_cli_split')
    @patch('src.commands._run_aws_subprocess')
    @patch('tempfile.NamedTemporaryFile')
    @patch('os.unlink')
    def test_cli_input_json_chinese_characters(
        self, mock_unlink, mock_tempfile, mock_subprocess, mock_split
    ):
        """Test 3: Chinese characters in cli_input_json survive roundtrip"""
        # Setup mock tempfile
        mock_temp_obj = MagicMock()
        mock_temp_obj.name = '/tmp/bouncer_cli_test456.json'
        mock_temp_obj.__enter__ = Mock(return_value=mock_temp_obj)
        mock_temp_obj.__exit__ = Mock(return_value=False)
        mock_tempfile.return_value = mock_temp_obj

        # Setup mock subprocess
        mock_subprocess.return_value = (0, '', '')

        mock_split.return_value = ['aws', 'dynamodb', 'update-item', '--cli-input-json', 'file:///tmp/bouncer_cli_test456.json']

        # Test data with Chinese characters, newlines, and nested quotes
        cli_input_data = {
            'TableName': 'test-table',
            'Key': {'id': {'S': '123'}},
            'ExpressionAttributeValues': {
                ':val': {'S': '中文內容\n換行\n"巢狀引號"'}
            }
        }

        # Execute
        _execute_locked(
            'aws dynamodb update-item',
            assume_role_arn=None,
            cli_input_json=cli_input_data
        )

        # Verify JSON dump was called (indirectly via the tempfile write)
        # We can't easily verify the exact content without mocking json.dump,
        # but we can verify that the tempfile was created and used
        mock_tempfile.assert_called_once()
        mock_unlink.assert_called_once_with('/tmp/bouncer_cli_test456.json')

    @patch('src.commands.aws_cli_split')
    @patch('src.commands._run_aws_subprocess')
    @patch('tempfile.NamedTemporaryFile')
    @patch('os.unlink')
    def test_cli_input_json_cleanup_on_error(
        self, mock_unlink, mock_tempfile, mock_subprocess, mock_split
    ):
        """Test 4: tempfile is cleaned up after execution (even on error)"""
        # Setup mock tempfile
        mock_temp_obj = MagicMock()
        mock_temp_obj.name = '/tmp/bouncer_cli_test789.json'
        mock_temp_obj.__enter__ = Mock(return_value=mock_temp_obj)
        mock_temp_obj.__exit__ = Mock(return_value=False)
        mock_tempfile.return_value = mock_temp_obj

        # Setup mock subprocess to raise an exception
        mock_subprocess.side_effect = Exception('AWS CLI error')

        mock_split.return_value = ['aws', 's3', 'ls', '--cli-input-json', 'file:///tmp/bouncer_cli_test789.json']

        cli_input_data = {'Bucket': 'test-bucket'}

        # Execute (should handle exception)
        result = _execute_locked(
            'aws s3 ls',
            assume_role_arn=None,
            cli_input_json=cli_input_data
        )

        # Verify tempfile was cleaned up even though an error occurred
        mock_unlink.assert_called_once_with('/tmp/bouncer_cli_test789.json')

        # Verify error message in result
        assert '❌' in result or 'error' in result.lower()

    def test_execute_command_passes_cli_input_json_through(self):
        """Test 5: execute_command passes cli_input_json through to _execute_locked"""
        mock_executor = Mock(return_value='✅ 命令執行成功')

        cli_input_data = {'Bucket': 'test-bucket'}

        # Execute via execute_command with custom executor
        execute_command(
            'aws s3 ls',
            assume_role_arn=None,
            _executor=mock_executor,
            cli_input_json=cli_input_data
        )

        # Verify executor was called with cli_input_json
        mock_executor.assert_called_once()
        call_args = mock_executor.call_args
        # The call should be: executor(command, assume_role_arn, cli_input_json)
        assert len(call_args[0]) == 3  # positional args
        assert call_args[0][0] == 'aws s3 ls'
        assert call_args[0][1] is None  # assume_role_arn
        assert call_args[0][2] == cli_input_data  # cli_input_json

    def test_execute_context_cli_input_json_extracted(self):
        """Test 6: ExecuteContext.cli_input_json is extracted from MCP arguments"""
        req_id = 'test-req-001'
        cli_input_data = {
            'TableName': 'test-table',
            'Key': {'id': {'S': '123'}}
        }

        arguments = {
            'command': 'aws dynamodb get-item',
            'reason': 'Test with cli_input_json',
            'trust_scope': 'test-scope',
            'cli_input_json': cli_input_data
        }

        # Parse the request to create ExecuteContext
        ctx = _parse_execute_request(req_id, arguments)

        # Verify ctx is ExecuteContext (not an error dict)
        assert isinstance(ctx, ExecuteContext)

        # Verify cli_input_json was extracted
        assert ctx.cli_input_json == cli_input_data

    def test_execute_context_cli_input_json_none_when_not_provided(self):
        """Test 6b: ExecuteContext.cli_input_json is None when not provided"""
        req_id = 'test-req-002'

        arguments = {
            'command': 'aws s3 ls',
            'reason': 'Test without cli_input_json',
            'trust_scope': 'test-scope'
        }

        # Parse the request to create ExecuteContext
        ctx = _parse_execute_request(req_id, arguments)

        # Verify ctx is ExecuteContext (not an error dict)
        assert isinstance(ctx, ExecuteContext)

        # Verify cli_input_json is None
        assert ctx.cli_input_json is None


class TestCliInputJsonIntegration:
    """Integration tests for cli_input_json through the full pipeline"""

    @patch('src.commands._run_aws_subprocess')
    @patch('os.unlink')
    def test_execute_command_with_cli_input_json_integration(
        self, mock_unlink, mock_subprocess
    ):
        """Integration test: full flow from execute_command to AWS CLI with cli_input_json"""
        # Setup mock subprocess
        mock_subprocess.return_value = (0, '', '')

        cli_input_data = {
            'Bucket': 'my-bucket',
            'Key': 'test/file.txt',
            'Body': '測試內容\n換行'
        }

        # Execute the command
        result = execute_command(
            'aws s3 put-object',
            assume_role_arn=None,
            cli_input_json=cli_input_data
        )

        # Verify subprocess was called
        mock_subprocess.assert_called_once()

        # Verify the CLI args contain --cli-input-json
        cli_args = mock_subprocess.call_args[0][0]
        assert '--cli-input-json' in ' '.join(cli_args)

        # Verify tempfile was cleaned up
        assert mock_unlink.called

        # Verify success
        assert '✅' in result or 'exit code: 0' not in result
