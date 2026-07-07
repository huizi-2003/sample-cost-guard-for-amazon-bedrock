"""Unit tests for Web API endpoints: /api/monitor/<date> and /api/backfill.

Uses pytest with httpx AsyncClient for FastAPI testing.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import patch

from httpx import AsyncClient, ASGITransport
from web.app import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# === GET /api/monitor/<date> ===


class TestMonitorEndpoint:
    """Tests for GET /api/monitor/<date>."""

    @pytest.mark.anyio
    @patch('web.app.query_by_pk')
    async def test_monitor_valid_date_returns_sorted_records(self, mock_query, client):
        """GET /api/monitor/valid-date returns records sorted by time ascending."""
        mock_query.return_value = [
            {
                'SK': 'T#14:30',
                'total_5min': 200,
                'total_daily': 5000,
                'timestamp': '2024-07-01T06:30:00Z',
                'region_count': 10,
            },
            {
                'SK': 'T#08:00',
                'total_5min': 100,
                'total_daily': 1000,
                'timestamp': '2024-07-01T00:00:00Z',
                'region_count': 10,
            },
        ]

        resp = await client.get('/api/monitor/2024-07-01')
        assert resp.status_code == 200

        data = resp.json()
        assert len(data) == 2
        # Sorted by time ascending: 08:00 before 14:30
        assert data[0]['time'] == '08:00'
        assert data[1]['time'] == '14:30'
        assert data[0]['total_5min'] == 100
        assert data[0]['total_daily'] == 1000
        assert data[0]['timestamp'] == '2024-07-01T00:00:00Z'
        assert data[0]['region_count'] == 10
        assert data[1]['total_5min'] == 200
        assert data[1]['total_daily'] == 5000

        mock_query.assert_called_once_with('MONITOR#2024-07-01')

    @pytest.mark.anyio
    async def test_monitor_invalid_format_returns_400(self, client):
        """GET /api/monitor/invalid-format returns 400."""
        resp = await client.get('/api/monitor/not-a-date')
        assert resp.status_code == 400
        data = resp.json()
        assert 'error' in data

    @pytest.mark.anyio
    async def test_monitor_future_date_returns_400(self, client):
        """GET /api/monitor/future-date returns 400."""
        resp = await client.get('/api/monitor/2099-12-31')
        assert resp.status_code == 400
        data = resp.json()
        assert 'error' in data

    @pytest.mark.anyio
    @patch('web.app.query_by_pk')
    async def test_monitor_date_with_no_data_returns_empty_array(self, mock_query, client):
        """GET /api/monitor/date-with-no-data returns empty array."""
        mock_query.return_value = []

        resp = await client.get('/api/monitor/2024-01-01')
        assert resp.status_code == 200

        data = resp.json()
        assert data == []

        mock_query.assert_called_once_with('MONITOR#2024-01-01')


# === POST /api/backfill ===


class TestBackfillEndpoint:
    """Tests for POST /api/backfill."""

    @pytest.mark.anyio
    async def test_backfill_days_zero_returns_400(self, client):
        """POST /api/backfill with days=0 returns 400."""
        resp = await client.post('/api/backfill', json={'days': 0})
        assert resp.status_code == 400
        data = resp.json()
        assert 'error' in data

    @pytest.mark.anyio
    async def test_backfill_days_366_returns_400(self, client):
        """POST /api/backfill with days=366 returns 400."""
        resp = await client.post('/api/backfill', json={'days': 366})
        assert resp.status_code == 400
        data = resp.json()
        assert 'error' in data
