"""
Tests for src/help_command.py - AWS CLI command help using botocore.

Tests built-in Bouncer command help, AWS command help parsing, and formatting.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# Ensure src is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import help_command

pytestmark = pytest.mark.xdist_group("help_command")


class TestGetBouncerCommandHelp:
    """Test get_bouncer_command_help() built-in command lookup."""

    def test_get_bouncer_command_help_batch_deploy(self):
        """get_bouncer_command_help() returns batch-deploy help."""
        result = help_command.get_bouncer_command_help("batch-deploy")

        assert result is not None
        assert result['name'] == 'batch-deploy'
        assert 'presigned_batch' in result['description']
        assert len(result['steps']) > 0

    def test_get_bouncer_command_help_with_bouncer_prefix(self):
        """get_bouncer_command_help() strips 'bouncer' prefix."""
        result = help_command.get_bouncer_command_help("bouncer batch-deploy")

        assert result is not None
        assert result['name'] == 'batch-deploy'

    def test_get_bouncer_command_help_case_insensitive(self):
        """get_bouncer_command_help() is case-insensitive."""
        result = help_command.get_bouncer_command_help("BATCH-DEPLOY")

        assert result is not None
        assert result['name'] == 'batch-deploy'

    def test_get_bouncer_command_help_unknown_command(self):
        """get_bouncer_command_help() returns None for unknown commands."""
        result = help_command.get_bouncer_command_help("unknown-command")

        assert result is None


class TestFormatBouncerHelpText:
    """Test format_bouncer_help_text() formatting."""

    def test_format_bouncer_help_text_includes_all_sections(self):
        """format_bouncer_help_text() includes name, description, steps, example, see_also."""
        help_data = {
            'name': 'test-command',
            'description': 'Test description',
            'steps': ['Step 1', 'Step 2'],
            'example': 'mcporter call bouncer test',
            'see_also': ['tool1', 'tool2']
        }

        result = help_command.format_bouncer_help_text(help_data)

        assert 'test-command' in result
        assert 'Test description' in result
        assert 'Step 1' in result
        assert 'Step 2' in result
        assert 'mcporter call bouncer test' in result
        assert 'tool1' in result

    def test_format_bouncer_help_text_no_optional_sections(self):
        """format_bouncer_help_text() handles missing example and see_also."""
        help_data = {
            'name': 'minimal',
            'description': 'Minimal help',
            'steps': ['One step']
        }

        result = help_command.format_bouncer_help_text(help_data)

        assert 'minimal' in result
        assert 'Minimal help' in result


class TestGetCommandHelp:
    """Test get_command_help() AWS CLI command parsing."""

    @patch('help_command.botocore.session.get_session')
    def test_get_command_help_success(self, mock_get_session):
        """get_command_help() parses ec2 describe-instances command."""
        mock_session = MagicMock()
        mock_service_model = MagicMock()
        mock_operation_model = MagicMock()

        mock_operation_model.documentation = "Describes EC2 instances"
        mock_operation_model.input_shape = None

        mock_service_model.operation_model.return_value = mock_operation_model
        mock_session.get_service_model.return_value = mock_service_model
        mock_get_session.return_value = mock_session

        result = help_command.get_command_help("ec2 describe-instances")

        assert result['service'] == 'ec2'
        assert result['operation'] == 'describe-instances'
        assert result['api_name'] == 'DescribeInstances'
        assert 'description' in result

    @patch('help_command.botocore.session.get_session')
    def test_get_command_help_with_aws_prefix(self, mock_get_session):
        """get_command_help() strips 'aws' prefix from command."""
        mock_session = MagicMock()
        mock_service_model = MagicMock()
        mock_operation_model = MagicMock()
        mock_operation_model.documentation = "Test"
        mock_operation_model.input_shape = None

        mock_service_model.operation_model.return_value = mock_operation_model
        mock_session.get_service_model.return_value = mock_service_model
        mock_get_session.return_value = mock_session

        result = help_command.get_command_help("aws s3 ls")

        assert result['service'] == 's3'

    def test_get_command_help_invalid_format(self):
        """get_command_help() returns error for invalid command format."""
        result = help_command.get_command_help("ec2")

        assert 'error' in result
        assert '無效命令格式' in result['error']

    @patch('help_command.botocore.session.get_session')
    def test_get_command_help_service_not_found(self, mock_get_session):
        """get_command_help() returns error for unknown service."""
        mock_session = MagicMock()
        mock_session.get_service_model.side_effect = Exception("Service not found")
        mock_get_session.return_value = mock_session

        result = help_command.get_command_help("invalid-service list")

        assert 'error' in result
        assert 'invalid-service' in result['error']

    @patch('help_command.botocore.session.get_session')
    def test_get_command_help_operation_not_found(self, mock_get_session):
        """get_command_help() suggests similar operations when not found."""
        mock_session = MagicMock()
        mock_service_model = MagicMock()
        mock_service_model.operation_model.side_effect = Exception("Not found")
        mock_service_model.operation_names = ['DescribeInstances', 'DescribeImages']

        mock_session.get_service_model.return_value = mock_service_model
        mock_get_session.return_value = mock_session

        result = help_command.get_command_help("ec2 describe-invalid")

        assert 'error' in result
        assert 'similar_operations' in result


class TestCamelToKebab:
    """Test camel_to_kebab() case conversion."""

    def test_camel_to_kebab_basic(self):
        """camel_to_kebab() converts CamelCase to kebab-case."""
        assert help_command.camel_to_kebab("DescribeInstances") == "describe-instances"

    def test_camel_to_kebab_single_word(self):
        """camel_to_kebab() handles single word."""
        assert help_command.camel_to_kebab("List") == "list"

    def test_camel_to_kebab_with_numbers(self):
        """camel_to_kebab() handles numbers in name."""
        assert help_command.camel_to_kebab("GetS3Object") == "get-s3-object"

    def test_camel_to_kebab_consecutive_caps(self):
        """camel_to_kebab() handles consecutive capital letters."""
        assert help_command.camel_to_kebab("CreateDBInstance") == "create-db-instance"


class TestGetTypeName:
    """Test get_type_name() parameter type extraction."""

    def test_get_type_name_structure(self):
        """get_type_name() returns 'JSON object' for structure type."""
        mock_shape = MagicMock()
        mock_shape.type_name = 'structure'

        result = help_command.get_type_name(mock_shape)

        assert result == 'JSON object'

    def test_get_type_name_list(self):
        """get_type_name() returns 'list of X' for list type."""
        mock_member = MagicMock()
        mock_member.type_name = 'string'

        mock_shape = MagicMock()
        mock_shape.type_name = 'list'
        mock_shape.member = mock_member

        result = help_command.get_type_name(mock_shape)

        assert result == 'list of string'

    def test_get_type_name_primitive(self):
        """get_type_name() returns type name for primitive types."""
        for type_name in ['boolean', 'integer', 'timestamp']:
            mock_shape = MagicMock()
            mock_shape.type_name = type_name

            result = help_command.get_type_name(mock_shape)

            assert result == type_name


class TestCleanDescription:
    """Test clean_description() documentation cleanup."""

    def test_clean_description_removes_html_tags(self):
        """clean_description() removes HTML tags."""
        doc = "This is <code>sample</code> text with <b>HTML</b>."
        result = help_command.clean_description(doc)

        assert '<code>' not in result
        assert '<b>' not in result
        assert 'sample' in result

    def test_clean_description_truncates_long_text(self):
        """clean_description() truncates descriptions over 200 chars."""
        doc = "x" * 300
        result = help_command.clean_description(doc)

        assert len(result) <= 200

    def test_clean_description_none(self):
        """clean_description() returns empty string for None."""
        result = help_command.clean_description(None)

        assert result == ''

    def test_clean_description_normalizes_whitespace(self):
        """clean_description() normalizes multiple spaces and newlines."""
        doc = "Line 1\n\n  Line 2    multiple   spaces"
        result = help_command.clean_description(doc)

        assert '\n\n' not in result
        assert '   ' not in result
