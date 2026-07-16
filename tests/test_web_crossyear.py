"""Tests for cross-year timeline sorting fix in web/app.py.

Covers:
- _build_cost_response: sorted output with YYYY-MM-DD HH:MM internal keys
- _calc_cost_from_ddb: cross-year items produce correct sorted timeline
- _monitor_models_last24h: cross-year DDB records produce sorted series
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from web.app import _build_cost_response, _calc_cost_from_ddb


class TestBuildCostResponseCrossYear:
    """_build_cost_response sorts internal keys correctly and formats output."""

    def test_cross_year_timeline_order(self):
        """跨年时间点排序：2025-12-31 在 2026-01-01 前面。"""
        timeline_points = {
            '2025-12-31 23:57': 1.0,
            '2026-01-01 00:02': 2.0,
        }
        model_totals = {}
        unpriced = set()

        result = _build_cost_response(model_totals, timeline_points, unpriced)

        assert len(result['timeline']) == 2
        assert result['timeline'][0]['time'] == '12/31 23:57'
        assert result['timeline'][1]['time'] == '01/01 00:02'
        # 累计单调递增
        assert result['timeline'][0]['cost'] == 1.0
        assert result['timeline'][1]['cost'] == 3.0

    def test_same_day_points_order(self):
        """同一天内的时间点也正确排序。"""
        timeline_points = {
            '2026-07-16 14:05': 0.5,
            '2026-07-16 09:30': 1.0,
            '2026-07-16 22:00': 0.3,
        }
        result = _build_cost_response({}, timeline_points, set())
        times = [p['time'] for p in result['timeline']]
        assert times == ['07/16 09:30', '07/16 14:05', '07/16 22:00']

    def test_output_format_mm_dd_hh_mm(self):
        """输出格式为 MM/DD HH:MM（前端兼容）。"""
        timeline_points = {'2026-03-05 08:15': 2.5}
        result = _build_cost_response({}, timeline_points, set())
        assert result['timeline'][0]['time'] == '03/05 08:15'


class TestCalcCostFromDdbCrossYear:
    """_calc_cost_from_ddb with cross-year items."""

    @patch('web.app._match_pricing')
    def test_cross_year_items_sorted_correctly(self, mock_pricing):
        """跨年的两条 DDB item 应产生时间正序的 timeline。"""
        # 模拟 claude-sonnet-4 的定价
        mock_pricing.return_value = {'input': 3.0, 'output': 15.0, 'cache_read': 0.3, 'cache_write': 3.75}

        items = [
            {
                'PK': 'MONITOR#2026-01-01',
                'SK': 'T#00:02',
                'timestamp': '2026-01-01T00:02:00Z',
                'models': {'claude-sonnet-4': {'input': 1000000, 'output': 0, 'cache_read': 0, 'cache_write': 0}},
            },
            {
                'PK': 'MONITOR#2025-12-31',
                'SK': 'T#23:57',
                'timestamp': '2025-12-31T23:57:00Z',
                'models': {'claude-sonnet-4': {'input': 500000, 'output': 0, 'cache_read': 0, 'cache_write': 0}},
            },
        ]
        result = _calc_cost_from_ddb(items)

        assert len(result['timeline']) == 2
        # 12/31 应排在 01/01 前
        assert result['timeline'][0]['time'] == '12/31 23:57'
        assert result['timeline'][1]['time'] == '01/01 00:02'
        # 费用累计单调递增
        assert result['timeline'][1]['cost'] > result['timeline'][0]['cost']

    @patch('web.app._match_pricing')
    def test_fallback_key_from_pk_sk(self, mock_pricing):
        """item 无 timestamp 字段时，从 PK+SK 拼出完整 key。"""
        mock_pricing.return_value = {'input': 3.0, 'output': 15.0, 'cache_read': 0.3, 'cache_write': 3.75}

        items = [
            {
                'PK': 'MONITOR#2026-07-16',
                'SK': 'T#14:05',
                # 无 timestamp 字段
                'models': {'claude-sonnet-4': {'input': 100000, 'output': 0, 'cache_read': 0, 'cache_write': 0}},
            },
        ]
        result = _calc_cost_from_ddb(items)
        assert result['timeline'][0]['time'] == '07/16 14:05'


class TestMonitorModelsLast24hCrossYear:
    """_monitor_models_last24h cross-year sorted output."""

    @patch('web.app.get_regions', return_value=[])
    @patch('web.app.query_by_pk')
    def test_cross_year_series_sorted(self, mock_query, mock_regions):
        """跨年两条记录的 series 应按时间正序排列。"""
        from web.app import _monitor_models_last24h

        # 模拟 query_by_pk 返回两天的数据（跨年）
        def mock_query_side_effect(pk):
            if pk == 'MONITOR#2026-01-01':
                return [{
                    'PK': 'MONITOR#2026-01-01',
                    'SK': 'T#00:02',
                    'timestamp': '2026-01-01T00:02:00Z',
                    'models': {'claude-sonnet-4': {'input': 200, 'output': 100, 'cache_read': 0, 'cache_write': 0}},
                }]
            elif pk == 'MONITOR#2025-12-31':
                return [{
                    'PK': 'MONITOR#2025-12-31',
                    'SK': 'T#23:57',
                    'timestamp': '2025-12-31T23:57:00Z',
                    'models': {'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0}},
                }]
            return []

        mock_query.side_effect = mock_query_side_effect

        # Mock datetime.now to be 2026-01-01 00:05 UTC
        fake_now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
        with patch('web.app.datetime') as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # cutoff = now - 24h = 2025-12-31 00:05
            result = _monitor_models_last24h()

        assert 'claude-sonnet-4' in result
        series = result['claude-sonnet-4']
        assert len(series) == 2
        # 12/31 在前
        assert series[0]['time'] == '12/31 23:57'
        assert series[1]['time'] == '01/01 00:02'
        # tokens 累计
        assert series[1]['tokens'] > series[0]['tokens']
