"""Tests for metrics module."""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from unittest.mock import patch
from io import StringIO


class TestEmitMetric:
    """Tests for emit_metric function."""

    @patch('sys.stdout', new_callable=StringIO)
    def test_basic_metric_emission(self, mock_stdout):
        """Test basic metric emission with minimal parameters."""
        from metrics import emit_metric

        with patch('metrics.time.time', return_value=1000.5):
            emit_metric('TestNamespace', 'TestMetric', 42.0)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['TestMetric'] == 42.0
        assert emf_data['_aws']['Timestamp'] == 1000500  # milliseconds
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Namespace'] == 'TestNamespace'
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Metrics'][0]['Name'] == 'TestMetric'
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Metrics'][0]['Unit'] == 'Count'

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_custom_unit(self, mock_stdout):
        """Test metric with custom unit."""
        from metrics import emit_metric

        emit_metric('TestNamespace', 'ResponseTime', 150.5, unit='Milliseconds')

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['ResponseTime'] == 150.5
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Metrics'][0]['Unit'] == 'Milliseconds'

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_dict_dimensions(self, mock_stdout):
        """Test metric with dimensions as dict (legacy format)."""
        from metrics import emit_metric

        dimensions = {'Status': 'success', 'Path': 'auto_approve'}
        emit_metric('Bouncer', 'CommandExecution', 1, dimensions=dimensions)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # Dimensions should be included in the EMF data
        assert emf_data['Status'] == 'success'
        assert emf_data['Path'] == 'auto_approve'
        assert emf_data['CommandExecution'] == 1

        # Dimension keys should be in the Dimensions array
        dim_keys = emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'][0]
        assert 'Status' in dim_keys
        assert 'Path' in dim_keys

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_list_dimensions(self, mock_stdout):
        """Test metric with dimensions as list (CloudWatch EMF format)."""
        from metrics import emit_metric

        dimensions = [
            {'Name': 'Decision', 'Value': 'approved'},
            {'Name': 'Source', 'Value': 'telegram'}
        ]
        emit_metric('Bouncer', 'Approval', 1, dimensions=dimensions)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # Should convert list to dict format
        assert emf_data['Decision'] == 'approved'
        assert emf_data['Source'] == 'telegram'
        assert emf_data['Approval'] == 1

        dim_keys = emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'][0]
        assert 'Decision' in dim_keys
        assert 'Source' in dim_keys

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_no_dimensions(self, mock_stdout):
        """Test metric without dimensions."""
        from metrics import emit_metric

        emit_metric('TestNamespace', 'SimpleCounter', 1)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['SimpleCounter'] == 1
        # Should have empty dimensions array
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'] == [[]]

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_none_dimensions(self, mock_stdout):
        """Test metric with explicit None dimensions."""
        from metrics import emit_metric

        emit_metric('TestNamespace', 'NoDimMetric', 5, dimensions=None)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['NoDimMetric'] == 5
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'] == [[]]

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_empty_list_dimensions(self, mock_stdout):
        """Test metric with empty list dimensions."""
        from metrics import emit_metric

        emit_metric('TestNamespace', 'EmptyDim', 3, dimensions=[])

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['EmptyDim'] == 3
        assert emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'] == [[]]

    @patch('sys.stdout', new_callable=StringIO)
    def test_metric_with_malformed_list_dimensions(self, mock_stdout):
        """Test metric with malformed list dimensions (missing Name or Value)."""
        from metrics import emit_metric

        dimensions = [
            {'Name': 'Valid', 'Value': 'yes'},
            {'Name': 'NoValue'},  # Missing Value
            {'Value': 'NoName'},  # Missing Name
            {'NotValid': 'neither'}  # Wrong keys
        ]
        emit_metric('TestNamespace', 'MalformedDim', 1, dimensions=dimensions)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # Only valid dimension should be included
        assert emf_data['Valid'] == 'yes'
        assert 'NoValue' not in emf_data
        assert 'NoName' not in emf_data
        assert 'NotValid' not in emf_data

    @patch('sys.stdout', new_callable=StringIO)
    def test_timestamp_format(self, mock_stdout):
        """Test that timestamp is in milliseconds."""
        from metrics import emit_metric

        with patch('metrics.time.time', return_value=1234.567):
            emit_metric('Test', 'TimestampTest', 1)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # Should be in milliseconds
        assert emf_data['_aws']['Timestamp'] == 1234567

    @patch('sys.stdout', new_callable=StringIO)
    def test_emf_structure_compliance(self, mock_stdout):
        """Test that EMF structure complies with CloudWatch requirements."""
        from metrics import emit_metric

        emit_metric('Bouncer', 'TestMetric', 100, unit='Count',
                   dimensions={'Env': 'prod', 'Region': 'us-east-1'})

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # Verify required EMF structure
        assert '_aws' in emf_data
        assert 'CloudWatchMetrics' in emf_data['_aws']
        assert 'Timestamp' in emf_data['_aws']

        cw_metrics = emf_data['_aws']['CloudWatchMetrics'][0]
        assert 'Namespace' in cw_metrics
        assert 'Dimensions' in cw_metrics
        assert 'Metrics' in cw_metrics
        assert isinstance(cw_metrics['Dimensions'], list)
        assert isinstance(cw_metrics['Dimensions'][0], list)

    @patch('sys.stdout', new_callable=StringIO)
    def test_float_value_precision(self, mock_stdout):
        """Test that float values are preserved with precision."""
        from metrics import emit_metric

        emit_metric('Test', 'FloatMetric', 123.456789)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        assert emf_data['FloatMetric'] == 123.456789

    @patch('sys.stdout', new_callable=StringIO)
    def test_multiple_dimensions(self, mock_stdout):
        """Test metric with multiple dimensions."""
        from metrics import emit_metric

        dimensions = {
            'Environment': 'production',
            'Service': 'bouncer',
            'Region': 'us-east-1',
            'AccountId': '123456789012'
        }
        emit_metric('Bouncer', 'MultiDim', 1, dimensions=dimensions)

        output = mock_stdout.getvalue()
        emf_data = json.loads(output.strip())

        # All dimensions should be present
        assert emf_data['Environment'] == 'production'
        assert emf_data['Service'] == 'bouncer'
        assert emf_data['Region'] == 'us-east-1'
        assert emf_data['AccountId'] == '123456789012'

        # All dimension keys should be in Dimensions array
        dim_keys = emf_data['_aws']['CloudWatchMetrics'][0]['Dimensions'][0]
        assert len(dim_keys) == 4
