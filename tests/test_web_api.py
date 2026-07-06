"""Unit tests for Web API endpoints: /api/monitor/<date> and /api/backfill.

Uses pytest with unittest.mock to test Flask route handlers.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import patch

from web.app import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


# === GET /api/monitor/<date> ===


@patch('web.app.query_by_pk')
def test_monitor_valid_date_returns_sorted_records(mock_query, client):
    """GET /api/monitor/valid-date returns records sorted by time ascending.

    **Validates: Requirements 4.1, 4.2**
    """
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

    resp = client.get('/api/monitor/2024-07-01')
    assert resp.status_code == 200

    data = resp.get_json()
    assert len(data) == 2
    # Sorted by time ascending: 08:00 before 14:30
    assert data[0]['time'] == '08:00'
    assert data[1]['time'] == '14:30'
    # Verify correct fields
    assert data[0]['total_5min'] == 100
    assert data[0]['total_daily'] == 1000
    assert data[0]['timestamp'] == '2024-07-01T00:00:00Z'
    assert data[0]['region_count'] == 10
    assert data[1]['total_5min'] == 200
    assert data[1]['total_daily'] == 5000

    mock_query.assert_called_once_with('MONITOR#2024-07-01')


def test_monitor_invalid_format_returns_400(client):
    """GET /api/monitor/invalid-format returns 400.

    **Validates: Requirements 4.4**
    """
    resp = client.get('/api/monitor/not-a-date')
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data


def test_monitor_future_date_returns_400(client):
    """GET /api/monitor/future-date returns 400.

    **Validates: Requirements 4.4**
    """
    resp = client.get('/api/monitor/2099-12-31')
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data


@patch('web.app.query_by_pk')
def test_monitor_date_with_no_data_returns_empty_array(mock_query, client):
    """GET /api/monitor/date-with-no-data returns empty array.

    **Validates: Requirements 4.2**
    """
    mock_query.return_value = []

    resp = client.get('/api/monitor/2024-01-01')
    assert resp.status_code == 200

    data = resp.get_json()
    assert data == []

    mock_query.assert_called_once_with('MONITOR#2024-01-01')


# === POST /api/backfill ===


def test_backfill_days_zero_returns_400(client):
    """POST /api/backfill with days=0 returns 400.

    **Validates: Requirements 2.8**
    """
    resp = client.post('/api/backfill', json={'days': 0})
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data


def test_backfill_days_366_returns_400(client):
    """POST /api/backfill with days=366 returns 400.

    **Validates: Requirements 2.8**
    """
    resp = client.post('/api/backfill', json={'days': 366})
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data


# === Removed price-table endpoint ===


def test_prices_get_returns_404(client):
    """GET /api/config/prices returns 404 after the price-table feature removal.

    **Validates: Requirements 4.4**
    """
    resp = client.get('/api/config/prices')
    assert resp.status_code == 404


def test_prices_put_returns_404(client):
    """PUT /api/config/prices returns 404 (route no longer exists).

    **Validates: Requirements 4.4**
    """
    resp = client.put('/api/config/prices', json={'model': 'x', 'input': 1})
    assert resp.status_code == 404


def test_prices_delete_returns_404(client):
    """DELETE /api/config/prices returns 404 (route no longer exists).

    **Validates: Requirements 4.4**
    """
    resp = client.delete('/api/config/prices', json={'model': 'x'})
    assert resp.status_code == 404
