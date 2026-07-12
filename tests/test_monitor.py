"""Unit tests for Monitor Lambda persistence logic.

Validates Requirements 3.4, 3.5, 3.6:
- put_item is called with correct PK/SK format after successful aggregation
- put_item is NOT called when region_results is empty
- DDB write failure is logged but does not prevent alert evaluation
"""
import re
import pytest
from unittest.mock import patch, MagicMock


MOCK_PATCHES = {
    'monitor.handler.get_cost_thresholds': {'5min': 1e12, '15min': 1e12, 'daily': 1e12},
    'monitor.handler.get_regions': ['us-east-1'],
    'monitor.handler.get_webhook_config': [],
}


@pytest.fixture
def mock_env():
    """Set up common mocks for monitor handler tests."""
    with patch('monitor.handler.get_monitor_enabled', return_value=True), \
         patch('monitor.handler.get_cost_thresholds', return_value=MOCK_PATCHES['monitor.handler.get_cost_thresholds']), \
         patch('monitor.handler.get_regions', return_value=MOCK_PATCHES['monitor.handler.get_regions']), \
         patch('monitor.handler.get_webhook_config', return_value=MOCK_PATCHES['monitor.handler.get_webhook_config']), \
         patch('monitor.handler.put_item') as mock_put_item, \
         patch('monitor.handler.fetch_region') as mock_fetch_region, \
         patch('monitor.handler.send_webhook_all') as mock_send_webhook, \
         patch('monitor.handler.get_alert_state', return_value=None), \
         patch('monitor.handler.set_alert_state'):
        yield {
            'put_item': mock_put_item,
            'fetch_region': mock_fetch_region,
            'send_webhook_all': mock_send_webhook,
        }


class TestMonitorPersistence:
    """Tests for Monitor Lambda DDB persistence logic."""

    def test_put_item_called_with_correct_pk_sk_format(self, mock_env):
        """Validates Requirement 3.4: put_item is called with correct PK/SK format
        after successful aggregation."""
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 100, '15min': 300, 'daily': 1000
        }

        from monitor.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_env['put_item'].assert_called_once()

        call_args = mock_env['put_item'].call_args
        pk = call_args[0][0]
        sk = call_args[0][1]

        # PK should match MONITOR#YYYY-MM-DD
        assert re.match(r'^MONITOR#\d{4}-\d{2}-\d{2}$', pk), f"PK format incorrect: {pk}"
        # SK should match T#HH:MM
        assert re.match(r'^T#\d{2}:\d{2}$', sk), f"SK format incorrect: {sk}"

        # Verify fields
        kwargs = call_args[1]
        assert kwargs['total_5min'] == 100
        assert kwargs['total_daily'] == 1000
        assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$', kwargs['timestamp'])
        assert kwargs['region_count'] == 1

    def test_put_item_not_called_when_region_results_empty(self, mock_env):
        """Validates Requirement 3.6: put_item is NOT called when all regions fail
        (region_results is empty)."""
        mock_env['fetch_region'].side_effect = Exception("CloudWatch timeout")

        from monitor.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_env['put_item'].assert_not_called()

    def test_ddb_write_failure_does_not_prevent_alert_evaluation(self, mock_env):
        """DDB write failure is logged but does not prevent (cost) alert evaluation."""
        # sonnet output $15/MTok × 2M tokens = $30 estimated cost
        models = {'claude-sonnet-4': {'input': 0, 'output': 2_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 2_000_000, '15min': 2_000_000, 'daily': 2_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }

        # Make put_item raise an exception
        mock_env['put_item'].side_effect = Exception("DDB error")

        # $1 threshold on 5min → $30 > $1 triggers alert
        with patch('monitor.handler.get_cost_thresholds', return_value={
            '5min': 1, '15min': 1e12, 'daily': 1e12
        }):
            from monitor.handler import handler
            result = handler({}, None)

        # Handler should still return 200 (did not crash)
        assert result['statusCode'] == 200
        # Alert should have been triggered ($30 > $1)
        assert '5min' in result['alerts']
        # Webhook should have been called for the alert
        mock_env['send_webhook_all'].assert_called()
