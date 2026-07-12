"""Unit tests for common/config.py

Covers:
- get_cost_thresholds: reads COST_THRESHOLD as floats ($), empty when unconfigured
- get_regions: normal read, first-run default write
- get_alert_state / set_alert_state
- get_webhook_config: normal read, no config fallback
- get_reconcile_dates: scan + sort + limit
- save_reconcile_record: correct PK/SK and expire_at
"""
import pytest
from unittest.mock import patch, MagicMock
from boto3.dynamodb.conditions import Key


# We need to mock boto3 before importing config to avoid real AWS calls
@pytest.fixture(autouse=True)
def mock_dynamodb():
    """Mock the DynamoDB table for all tests in this module."""
    mock_table = MagicMock()

    with patch('common.config.boto3') as mock_boto3:
        mock_boto3.resource.return_value.Table.return_value = mock_table
        # Reset the module-level _table cache
        import common.config
        common.config._table = mock_table
        yield mock_table

    # Reset after test
    common.config._table = None


class TestGetCostThresholds:
    """get_cost_thresholds: reads COST_THRESHOLD items as floats ($)."""

    def test_returns_cost_thresholds_as_float(self, mock_dynamodb):
        mock_dynamodb.query.return_value = {
            'Items': [
                {'PK': 'COST_THRESHOLD', 'SK': '5min', 'value': '2.5'},
                {'PK': 'COST_THRESHOLD', 'SK': 'daily', 'value': '100'},
            ]
        }
        from common.config import get_cost_thresholds
        result = get_cost_thresholds()
        assert result == {'5min': 2.5, 'daily': 100.0}

    def test_empty_when_unconfigured(self, mock_dynamodb):
        mock_dynamodb.query.return_value = {'Items': []}
        from common.config import get_cost_thresholds
        assert get_cost_thresholds() == {}


class TestGetRegions:
    """get_regions: reads CONFIG#regions or writes/returns defaults."""

    def test_returns_regions_from_ddb(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'regions', 'value': 'us-east-1,us-west-2,eu-west-1'}
        }
        from common.config import get_regions
        result = get_regions()
        assert result == ['us-east-1', 'us-west-2', 'eu-west-1']

    def test_writes_defaults_on_first_run(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {}  # No Item key
        from common.config import get_regions
        result = get_regions()

        # Should write default regions
        mock_dynamodb.put_item.assert_called_once()
        put_call = mock_dynamodb.put_item.call_args
        item = put_call[1]['Item'] if 'Item' in put_call[1] else put_call[0][0] if put_call[0] else None

        # Should return the default list
        assert 'us-east-1' in result
        assert 'us-west-2' in result
        assert len(result) == 10  # Default has 10 regions

    def test_strips_whitespace_from_regions(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'regions', 'value': ' us-east-1 , us-west-2 '}
        }
        from common.config import get_regions
        result = get_regions()
        assert result == ['us-east-1', 'us-west-2']

    def test_filters_empty_strings(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'regions', 'value': 'us-east-1,,us-west-2,'}
        }
        from common.config import get_regions
        result = get_regions()
        assert result == ['us-east-1', 'us-west-2']


class TestAlertState:
    """get_alert_state / set_alert_state: DDB read/write."""

    def test_get_alert_state_returns_value(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'ALERT_STATE', 'SK': 'last-alert-daily', 'value': '2024-07-01'}
        }
        from common.config import get_alert_state
        result = get_alert_state('daily')
        assert result == '2024-07-01'

    def test_get_alert_state_returns_none_when_missing(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {}
        from common.config import get_alert_state
        result = get_alert_state('15min')
        assert result is None

    def test_set_alert_state_writes_correct_key(self, mock_dynamodb):
        from common.config import set_alert_state
        set_alert_state('daily', '2024-07-01')

        mock_dynamodb.put_item.assert_called_once()
        put_call = mock_dynamodb.put_item.call_args
        item = put_call[1]['Item']
        assert item['PK'] == 'ALERT_STATE'
        assert item['SK'] == 'last-alert-daily'
        assert item['value'] == '2024-07-01'


class TestGetWebhookConfig:
    """get_webhook_config: reads CONFIG#webhooks (list) or migrates old CONFIG#webhook."""

    def test_returns_config_from_new_format(self, mock_dynamodb):
        """New format: SK=webhooks with items list."""
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'webhooks', 'items': [
                {'name': 'dingtalk', 'url': 'https://hook.example.com', 'type': 'dingtalk'}
            ]}
        }
        from common.config import get_webhook_config
        result = get_webhook_config()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]['url'] == 'https://hook.example.com'
        assert result[0]['type'] == 'dingtalk'

    def test_returns_empty_list_when_no_config(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {}
        from common.config import get_webhook_config
        result = get_webhook_config()
        assert result == []

    def test_migrates_old_format(self, mock_dynamodb):
        """Old format SK=webhook auto-migrates to new format."""
        # First call for 'webhooks' returns nothing, second for 'webhook' returns old format
        mock_dynamodb.get_item.side_effect = [
            {},  # SK=webhooks not found
            {'Item': {'PK': 'CONFIG', 'SK': 'webhook', 'url': 'https://old.com', 'type': 'feishu'}},
        ]
        from common.config import get_webhook_config
        result = get_webhook_config()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]['url'] == 'https://old.com'
        # Should have written the new format
        mock_dynamodb.put_item.assert_called()

    def test_returns_multiple_webhooks(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'webhooks', 'items': [
                {'name': '飞书', 'url': 'https://feishu.com/hook', 'type': 'feishu'},
                {'name': '钉钉', 'url': 'https://dingtalk.com/hook', 'type': 'dingtalk'},
            ]}
        }
        from common.config import get_webhook_config
        result = get_webhook_config()
        assert len(result) == 2
        assert result[0]['type'] == 'feishu'
        assert result[1]['type'] == 'dingtalk'


class TestGetReconcileDates:
    """get_reconcile_dates: scans for RECONCILE# prefixed items, sorts descending."""

    def test_returns_sorted_dates_descending(self, mock_dynamodb):
        mock_dynamodb.scan.return_value = {
            'Items': [
                {'PK': 'RECONCILE#2024-06-20'},
                {'PK': 'RECONCILE#2024-06-22'},
                {'PK': 'RECONCILE#2024-06-21'},
            ]
        }
        from common.config import get_reconcile_dates
        result = get_reconcile_dates(limit=30)
        assert result == ['2024-06-22', '2024-06-21', '2024-06-20']

    def test_respects_limit(self, mock_dynamodb):
        mock_dynamodb.scan.return_value = {
            'Items': [
                {'PK': 'RECONCILE#2024-06-20'},
                {'PK': 'RECONCILE#2024-06-22'},
                {'PK': 'RECONCILE#2024-06-21'},
            ]
        }
        from common.config import get_reconcile_dates
        result = get_reconcile_dates(limit=2)
        assert len(result) == 2
        assert result == ['2024-06-22', '2024-06-21']

    def test_returns_empty_list_when_no_data(self, mock_dynamodb):
        mock_dynamodb.scan.return_value = {'Items': []}
        from common.config import get_reconcile_dates
        result = get_reconcile_dates()
        assert result == []


class TestSaveReconcileRecord:
    """save_reconcile_record: writes with correct PK, SK, TTL."""

    def test_writes_correct_pk_sk(self, mock_dynamodb):
        from common.config import save_reconcile_record
        save_reconcile_record('2024-06-20', 'claude-sonnet-4', {'actual_cost': '1.23'})

        mock_dynamodb.put_item.assert_called_once()
        item = mock_dynamodb.put_item.call_args[1]['Item']
        assert item['PK'] == 'RECONCILE#2024-06-20'
        assert item['SK'] == 'claude-sonnet-4'
        assert item['actual_cost'] == '1.23'

    def test_sets_expire_at_approximately_90_days(self, mock_dynamodb):
        import time
        from common.config import save_reconcile_record
        before = int(time.time())
        save_reconcile_record('2024-06-20', '_summary', {'total_actual': '100'})

        item = mock_dynamodb.put_item.call_args[1]['Item']
        expire_at = item['expire_at']
        # Should be approximately 90 days from now
        expected_min = before + 89 * 86400
        expected_max = before + 91 * 86400
        assert expected_min <= expire_at <= expected_max
