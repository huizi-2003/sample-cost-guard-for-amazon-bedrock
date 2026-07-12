"""Unit tests for notify_policy (common/config.py) and holiday module (common/holiday.py).

Covers:
- get_notify_policy / save_notify_policy: DDB read/write, validation
- is_workday: holiday API response handling, cache, fallback
- Reconciler integration: notify_policy controls webhook push
- Web API: GET/PUT /api/config/notify-policy
"""
import sys
import os
import json
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest


# ===== common/config.py: notify_policy =====


@pytest.fixture(autouse=True)
def mock_dynamodb():
    """Mock DynamoDB table for config tests."""
    mock_table = MagicMock()
    with patch('common.config.boto3') as mock_boto3:
        mock_boto3.resource.return_value.Table.return_value = mock_table
        import common.config
        common.config._table = mock_table
        yield mock_table
    common.config._table = None


class TestGetNotifyPolicy:
    """get_notify_policy: reads CONFIG#notify_policy or returns default."""

    def test_returns_always_by_default(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {}
        from common.config import get_notify_policy
        assert get_notify_policy() == 'always'

    def test_returns_always_when_stored(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'notify_policy', 'value': 'always'}
        }
        from common.config import get_notify_policy
        assert get_notify_policy() == 'always'

    def test_returns_workday_when_stored(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'notify_policy', 'value': 'workday'}
        }
        from common.config import get_notify_policy
        assert get_notify_policy() == 'workday'

    def test_returns_always_for_invalid_value(self, mock_dynamodb):
        mock_dynamodb.get_item.return_value = {
            'Item': {'PK': 'CONFIG', 'SK': 'notify_policy', 'value': 'bogus'}
        }
        from common.config import get_notify_policy
        assert get_notify_policy() == 'always'


class TestSaveNotifyPolicy:
    """save_notify_policy: writes to DDB, validates input."""

    def test_saves_always(self, mock_dynamodb):
        from common.config import save_notify_policy
        save_notify_policy('always')
        mock_dynamodb.put_item.assert_called_once()
        item = mock_dynamodb.put_item.call_args[1]['Item']
        assert item['PK'] == 'CONFIG'
        assert item['SK'] == 'notify_policy'
        assert item['value'] == 'always'

    def test_saves_workday(self, mock_dynamodb):
        from common.config import save_notify_policy
        save_notify_policy('workday')
        item = mock_dynamodb.put_item.call_args[1]['Item']
        assert item['value'] == 'workday'

    def test_rejects_invalid_value(self, mock_dynamodb):
        from common.config import save_notify_policy
        with pytest.raises(ValueError):
            save_notify_policy('weekends')


# ===== common/holiday.py: is_workday =====


class TestIsWorkday:
    """is_workday: determines if a date is a Chinese workday."""

    def setup_method(self):
        from common.holiday import clear_cache
        clear_cache()

    @patch('common.holiday.urllib.request.urlopen')
    def test_holiday_is_not_workday(self, mock_urlopen):
        """A date marked as isOffDay=true should not be a workday."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'year': 2026,
            'days': [
                {'name': '元旦', 'date': '2026-01-01', 'isOffDay': True},
            ]
        }).encode()
        mock_urlopen.return_value = mock_resp

        from common.holiday import is_workday
        assert is_workday(date(2026, 1, 1)) is False

    @patch('common.holiday.urllib.request.urlopen')
    def test_makeup_workday_is_workday(self, mock_urlopen):
        """A date marked as isOffDay=false (调休上班) should be a workday."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'year': 2026,
            'days': [
                {'name': '春节', 'date': '2026-02-14', 'isOffDay': False},  # 调休上班（周六）
            ]
        }).encode()
        mock_urlopen.return_value = mock_resp

        from common.holiday import is_workday
        # 2026-02-14 is Saturday but marked as work day (调休)
        assert is_workday(date(2026, 2, 14)) is True

    @patch('common.holiday.urllib.request.urlopen')
    def test_normal_weekday_is_workday(self, mock_urlopen):
        """A normal weekday not in holiday data should be a workday."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'year': 2026,
            'days': [
                {'name': '元旦', 'date': '2026-01-01', 'isOffDay': True},
            ]
        }).encode()
        mock_urlopen.return_value = mock_resp

        from common.holiday import is_workday
        # 2026-01-05 is Monday, not in holiday data
        assert is_workday(date(2026, 1, 5)) is True

    @patch('common.holiday.urllib.request.urlopen')
    def test_normal_weekend_is_not_workday(self, mock_urlopen):
        """A normal weekend not in holiday data should not be a workday."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'year': 2026,
            'days': []
        }).encode()
        mock_urlopen.return_value = mock_resp

        from common.holiday import is_workday
        # 2026-01-10 is Saturday, not in data → fallback to weekday check
        assert is_workday(date(2026, 1, 10)) is False

    @patch('common.holiday.urllib.request.urlopen')
    def test_api_failure_fallback_weekday(self, mock_urlopen):
        """When API fails, fallback to weekday logic: Mon-Fri = workday."""
        mock_urlopen.side_effect = Exception("Network error")

        from common.holiday import is_workday
        # Monday
        assert is_workday(date(2026, 1, 5)) is True
        # Saturday
        assert is_workday(date(2026, 1, 10)) is False

    @patch('common.holiday.urllib.request.urlopen')
    def test_cache_prevents_repeated_requests(self, mock_urlopen):
        """Second call within cache TTL should not make another HTTP request."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'year': 2026, 'days': []
        }).encode()
        mock_urlopen.return_value = mock_resp

        from common.holiday import is_workday
        is_workday(date(2026, 3, 2))  # Monday
        is_workday(date(2026, 3, 3))  # Tuesday, same year → cached

        # urlopen should only be called once (for 2026)
        assert mock_urlopen.call_count == 1


# ===== Reconciler integration: policy controls push =====


class TestReconcilerNotifyPolicy:
    """Reconciler handler respects notify_policy setting."""

    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_notify_policy')
    @patch('reconciler.handler.is_workday')
    def test_workday_policy_skips_push_on_holiday(
        self, mock_is_workday, mock_get_policy, mock_get_webhooks, mock_reconcile, mock_send
    ):
        """When policy=workday and today is not a workday, skip push."""
        mock_get_policy.return_value = 'workday'
        mock_is_workday.return_value = False  # 今天不是工作日
        mock_get_webhooks.return_value = [{'url': 'http://test', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'test report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_send.assert_not_called()

    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_notify_policy')
    @patch('reconciler.handler.is_workday')
    def test_workday_policy_pushes_on_workday(
        self, mock_is_workday, mock_get_policy, mock_get_webhooks, mock_reconcile, mock_send
    ):
        """When policy=workday and today is a workday, push normally."""
        mock_get_policy.return_value = 'workday'
        mock_is_workday.return_value = True
        mock_get_webhooks.return_value = [{'url': 'http://test', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'test report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_send.assert_called_once()

    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_notify_policy')
    def test_always_policy_pushes_regardless(
        self, mock_get_policy, mock_get_webhooks, mock_reconcile, mock_send
    ):
        """When policy=always, always push."""
        mock_get_policy.return_value = 'always'
        mock_get_webhooks.return_value = [{'url': 'http://test', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'test report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_send.assert_called_once()


# ===== Web API: /api/config/notify-policy =====


@pytest.fixture
def web_client():
    from httpx import AsyncClient, ASGITransport
    from web.app import app
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestNotifyPolicyAPI:
    """GET/PUT /api/config/notify-policy."""

    @pytest.mark.anyio
    @patch('web.app.get_notify_policy')
    async def test_get_returns_current_policy(self, mock_get, web_client):
        mock_get.return_value = 'workday'
        resp = await web_client.get('/api/config/notify-policy')
        assert resp.status_code == 200
        assert resp.json() == {'policy': 'workday'}

    @pytest.mark.anyio
    @patch('web.app.save_notify_policy')
    async def test_put_saves_valid_policy(self, mock_save, web_client):
        resp = await web_client.put(
            '/api/config/notify-policy',
            json={'policy': 'workday'}
        )
        assert resp.status_code == 200
        assert resp.json() == {'ok': True, 'policy': 'workday'}
        mock_save.assert_called_once_with('workday')

    @pytest.mark.anyio
    async def test_put_rejects_invalid_policy(self, web_client):
        resp = await web_client.put(
            '/api/config/notify-policy',
            json={'policy': 'invalid'}
        )
        assert resp.status_code == 400
