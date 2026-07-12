"""Extended unit tests for web/app.py API routes.

Covers:
- GET /api/reconcile/summary: cost aggregation, model ranking, routing breakdown
- GET /api/reconcile/dates: date listing
- GET /api/reconcile/{date}: per-date detail
- GET /api/monitor/{date}/models: DDB cache and CW fallback
- GET/PUT /api/config/*: configuration CRUD
- _extract_routing: routing classification logic
- POST /api/backfill: valid range triggering
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

from httpx import AsyncClient, ASGITransport
from web.app import app, _extract_routing


# === Test client fixture ===


@pytest.fixture
def client():
    """FastAPI test client using httpx."""
    import httpx
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# === _extract_routing tests ===


class TestExtractRouting:
    """_extract_routing: classifies model identity into routing type."""

    def test_cross_region(self):
        assert _extract_routing('claude4.6opus-cross-region-global') == 'cross-region'

    def test_mantle(self):
        assert _extract_routing('anthropic.claude-opus-4-8-mantle-global') == 'mantle'

    def test_direct(self):
        assert _extract_routing('claude4.6opus') == 'direct'

    def test_case_insensitive_cross_region(self):
        assert _extract_routing('Model-Cross-Region-Something') == 'cross-region'

    def test_case_insensitive_mantle(self):
        assert _extract_routing('anthropic.claude-MANTLE-global') == 'mantle'


# === GET /api/reconcile/summary ===


class TestReconcileSummary:
    """GET /api/reconcile/summary: aggregation and analytics."""

    @pytest.mark.anyio
    @patch('web.app.get_reconcile_by_date')
    @patch('web.app.get_reconcile_dates')
    async def test_summary_with_data(self, mock_dates, mock_detail, client):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        date1 = now.strftime('%Y-%m-02')
        date2 = now.strftime('%Y-%m-01')
        mock_dates.return_value = [date1, date2]
        mock_detail.side_effect = [
            # date1
            {
                'claude-sonnet-4-cross-region-global': {'actual_cost': '30.0'},
                'claude-haiku': {'actual_cost': '5.0'},
                '_summary': {'total_actual': '35.0'},
            },
            # date2
            {
                'claude-sonnet-4-cross-region-global': {'actual_cost': '25.0'},
                'claude-haiku': {'actual_cost': '3.0'},
                '_summary': {'total_actual': '28.0'},
            },
        ]

        resp = await client.get('/api/reconcile/summary')
        assert resp.status_code == 200
        data = resp.json()

        # Period info
        assert data['period']['days_with_data'] == 2
        assert data['period']['month'] == now.strftime('%Y-%m')

        # Totals
        assert data['totals']['total_cost'] == 63.0
        assert data['totals']['daily_avg'] == 31.5

        # Model totals sorted by cost descending
        assert data['model_totals'][0]['model'] == 'claude-sonnet-4-cross-region-global'
        assert data['model_totals'][0]['cost'] == 55.0  # 30 + 25

        # Routing breakdown
        routings = {r['routing'] for r in data['routing_breakdown']}
        assert 'cross-region' in routings
        assert 'direct' in routings

    @pytest.mark.anyio
    @patch('web.app.get_reconcile_by_date')
    @patch('web.app.get_reconcile_dates')
    async def test_summary_no_data(self, mock_dates, mock_detail, client):
        mock_dates.return_value = []
        resp = await client.get('/api/reconcile/summary')
        assert resp.status_code == 200
        data = resp.json()
        assert data['daily_costs'] == []
        assert data['model_totals'] == []


# === GET /api/reconcile/dates ===


class TestReconcileDates:
    """GET /api/reconcile/dates: returns date list."""

    @pytest.mark.anyio
    @patch('web.app.get_reconcile_dates')
    async def test_returns_dates(self, mock_dates, client):
        mock_dates.return_value = ['2024-07-02', '2024-07-01']
        resp = await client.get('/api/reconcile/dates')
        assert resp.status_code == 200
        assert resp.json() == ['2024-07-02', '2024-07-01']


# === GET /api/reconcile/{date} ===


class TestReconcileDetail:
    """GET /api/reconcile/{date}: returns per-date breakdown."""

    @pytest.mark.anyio
    @patch('web.app.get_reconcile_by_date')
    async def test_returns_detail(self, mock_detail, client):
        mock_detail.return_value = {
            'model-a': {'actual_cost': '10.0', 'cost_input': '7.0', 'cost_output': '3.0'},
            '_summary': {'total_actual': '10.0', 'model_count': '1'},
        }
        resp = await client.get('/api/reconcile/2024-07-01')
        assert resp.status_code == 200
        data = resp.json()
        assert 'model-a' in data
        assert '_summary' in data


# === GET /api/monitor/{date}/models ===


class TestMonitorModels:
    """GET /api/monitor/{date}/models: DDB cache vs CW fallback."""

    @pytest.mark.anyio
    @patch('web.app.query_by_pk')
    async def test_returns_models_from_ddb_cache(self, mock_query, client):
        """When DDB has models data, use it directly."""
        mock_query.return_value = [
            {'SK': 'T#08:00', 'models': {'claude-sonnet-4': 1000, 'claude-haiku': 500}},
            {'SK': 'T#08:05', 'models': {'claude-sonnet-4': 2000, 'claude-haiku': 800}},
        ]
        resp = await client.get('/api/monitor/2024-07-01/models')
        assert resp.status_code == 200
        data = resp.json()
        assert 'claude-sonnet-4' in data
        # Should be cumulative: 1000, then 1000+2000=3000
        assert data['claude-sonnet-4'][0]['tokens'] == 1000
        assert data['claude-sonnet-4'][1]['tokens'] == 3000

    @pytest.mark.anyio
    @patch('web.app._fetch_models_from_cw')
    @patch('web.app.query_by_pk')
    async def test_fallback_to_cw_when_no_models_field(self, mock_query, mock_cw, client):
        """When DDB items have no models field, falls back to CW."""
        mock_query.return_value = [
            {'SK': 'T#08:00', 'total_5min': 100},  # No 'models' key
        ]
        mock_cw.return_value = {'model-a': [{'time': '08:00', 'tokens': 500}]}

        resp = await client.get('/api/monitor/2024-07-01/models')
        assert resp.status_code == 200
        data = resp.json()
        assert 'model-a' in data

    @pytest.mark.anyio
    async def test_invalid_date_returns_400(self, client):
        resp = await client.get('/api/monitor/not-a-date/models')
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_future_date_returns_400(self, client):
        resp = await client.get('/api/monitor/2099-12-31/models')
        assert resp.status_code == 400


# === GET/PUT /api/config/regions ===


class TestConfigRegions:
    """Configuration API for monitored regions."""

    @pytest.mark.anyio
    @patch('web.app.get_regions')
    async def test_get_regions(self, mock_get, client):
        mock_get.return_value = ['us-east-1', 'us-west-2']
        resp = await client.get('/api/config/regions')
        assert resp.status_code == 200
        assert resp.json() == ['us-east-1', 'us-west-2']

    @pytest.mark.anyio
    @patch('web.app.put_item')
    async def test_put_regions(self, mock_put, client):
        resp = await client.put('/api/config/regions',
                               json={'regions': ['us-east-1', 'eu-west-1']})
        assert resp.status_code == 200
        mock_put.assert_called_once_with('CONFIG', 'regions', value='us-east-1,eu-west-1')


# === GET/PUT /api/config/cost-thresholds ===


class TestConfigCostThresholds:
    """Configuration API for cost ($) alert thresholds."""

    @pytest.mark.anyio
    @patch('web.app.get_cost_thresholds')
    async def test_get_cost_thresholds(self, mock_get, client):
        mock_get.return_value = {'5min': 2.5, 'daily': 100.0}
        resp = await client.get('/api/config/cost-thresholds')
        assert resp.status_code == 200
        data = resp.json()
        assert data['5min'] == 2.5

    @pytest.mark.anyio
    @patch('web.app.put_item')
    async def test_put_cost_thresholds(self, mock_put, client):
        resp = await client.put('/api/config/cost-thresholds',
                                json={'5min': 2.5, '15min': 10, 'daily': 100})
        assert resp.status_code == 200
        assert mock_put.call_count == 3
        # stored under COST_THRESHOLD
        pks = {c.args[0] for c in mock_put.call_args_list}
        assert pks == {'COST_THRESHOLD'}
        # value must be a str (DynamoDB resource rejects Python float) and round-trip via float()
        for c in mock_put.call_args_list:
            v = c.kwargs['value']
            assert isinstance(v, str)
            float(v)  # parseable

    @pytest.mark.anyio
    async def test_put_cost_thresholds_rejects_negative(self, client):
        resp = await client.put('/api/config/cost-thresholds', json={'5min': -1})
        assert resp.status_code == 400


# === GET/PUT /api/config/webhook ===


class TestConfigWebhook:
    """Configuration API for webhook settings."""

    @pytest.mark.anyio
    @patch('web.app.get_webhook_config')
    async def test_get_webhook(self, mock_get, client):
        mock_get.return_value = [{'name': 'dingtalk', 'url': 'https://hook.example.com', 'type': 'dingtalk'}]
        resp = await client.get('/api/config/webhook')
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]['url'] == 'https://hook.example.com'
        assert data[0]['type'] == 'dingtalk'

    @pytest.mark.anyio
    @patch('web.app.get_webhook_config')
    async def test_get_webhook_no_config(self, mock_get, client):
        mock_get.return_value = []
        resp = await client.get('/api/config/webhook')
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    @pytest.mark.anyio
    @patch('web.app.save_webhook_config')
    async def test_put_webhook(self, mock_save, client):
        resp = await client.put('/api/config/webhook',
                               json=[{'name': '企微', 'url': 'https://new-hook.example.com', 'type': 'wecom'}])
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['count'] == 1
        mock_save.assert_called_once_with([{'name': '企微', 'url': 'https://new-hook.example.com', 'type': 'wecom'}])


# === POST /api/backfill (extended) ===


class TestBackfill:
    """POST /api/backfill: triggers async reconciliation."""

    @pytest.mark.anyio
    @patch('web.app.boto3.client')
    async def test_valid_backfill_triggers_lambdas(self, mock_client_factory, client):
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {}
        mock_client_factory.return_value = mock_lambda

        resp = await client.post('/api/backfill', json={'days': 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 3
        assert data['triggered'] == 3
        assert mock_lambda.invoke.call_count == 3

    @pytest.mark.anyio
    async def test_backfill_negative_days_returns_400(self, client):
        resp = await client.post('/api/backfill', json={'days': -1})
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_backfill_non_integer_returns_400(self, client):
        resp = await client.post('/api/backfill', json={'days': 'abc'})
        assert resp.status_code == 400  # Handler validates before FastAPI schema
