"""Unit tests for monitor delta alert logic.

Tests:
- _pick_baselines: baseline selection from records
- _delta: incremental cost calculation
- _model_deltas: per-model token increment
- Handler integration: delta-based alert triggering with patched query_by_pk
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from monitor.handler import _pick_baselines, _delta, _model_deltas, TOKEN_TYPES


# === _pick_baselines tests ===


class TestPickBaselines:
    """Baseline selection logic for delta computation."""

    def test_empty_records_returns_none(self):
        """No records → both baselines None."""
        now = datetime(2026, 7, 16, 10, 15, 0, tzinfo=timezone.utc)
        base_5, base_15 = _pick_baselines([], now)
        assert base_5 is None
        assert base_15 is None

    def test_all_incomplete_returns_none(self):
        """Records exist but all have complete=False → no valid baselines."""
        now = datetime(2026, 7, 16, 10, 15, 0, tzinfo=timezone.utc)
        records = [
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': False, 'cost_daily': '1.5'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': False, 'cost_daily': '2.0'},
        ]
        base_5, base_15 = _pick_baselines(records, now)
        assert base_5 is None
        assert base_15 is None

    def test_records_without_cost_daily_excluded(self):
        """Old records without cost_daily field are not valid baselines."""
        now = datetime(2026, 7, 16, 10, 15, 0, tzinfo=timezone.utc)
        records = [
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True},  # no cost_daily
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': True, 'cost_daily': '3.0'},
        ]
        base_5, base_15 = _pick_baselines(records, now)
        assert base_5 is not None
        assert base_5['cost_daily'] == '3.0'

    def test_base_5_picks_latest_valid(self):
        """base_5 should be the most recent valid record."""
        now = datetime(2026, 7, 16, 10, 15, 0, tzinfo=timezone.utc)
        records = [
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True, 'cost_daily': '1.0'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': True, 'cost_daily': '2.0'},
            {'timestamp': '2026-07-16T10:10:00Z', 'complete': True, 'cost_daily': '3.0'},
        ]
        base_5, _ = _pick_baselines(records, now)
        assert base_5['cost_daily'] == '3.0'

    def test_base_15_picks_record_at_least_15min_before_now(self):
        """base_15 should be the latest valid record whose timestamp <= now - 15min."""
        now = datetime(2026, 7, 16, 10, 20, 0, tzinfo=timezone.utc)
        # cutoff = 10:05:00
        records = [
            {'timestamp': '2026-07-16T09:55:00Z', 'complete': True, 'cost_daily': '1.0'},
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True, 'cost_daily': '2.0'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': True, 'cost_daily': '3.0'},
            {'timestamp': '2026-07-16T10:10:00Z', 'complete': True, 'cost_daily': '4.0'},
            {'timestamp': '2026-07-16T10:15:00Z', 'complete': True, 'cost_daily': '5.0'},
        ]
        _, base_15 = _pick_baselines(records, now)
        # cutoff = '2026-07-16T10:05:00Z', records at 09:55, 10:00, 10:05 are <= cutoff
        # latest of those is 10:05
        assert base_15['cost_daily'] == '3.0'

    def test_base_15_none_when_all_records_too_recent(self):
        """If all records are within last 15 minutes, base_15 is None."""
        now = datetime(2026, 7, 16, 10, 10, 0, tzinfo=timezone.utc)
        # cutoff = 09:55:00
        records = [
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True, 'cost_daily': '1.0'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': True, 'cost_daily': '2.0'},
        ]
        _, base_15 = _pick_baselines(records, now)
        assert base_15 is None

    def test_incomplete_record_between_valid_records_skipped(self):
        """Incomplete records are skipped even if recent."""
        now = datetime(2026, 7, 16, 10, 20, 0, tzinfo=timezone.utc)
        records = [
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True, 'cost_daily': '1.0'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': False, 'cost_daily': '2.0'},  # skipped
            {'timestamp': '2026-07-16T10:10:00Z', 'complete': True, 'cost_daily': '3.0'},
            {'timestamp': '2026-07-16T10:15:00Z', 'complete': True, 'cost_daily': '4.0'},
        ]
        base_5, base_15 = _pick_baselines(records, now)
        assert base_5['cost_daily'] == '4.0'
        # cutoff = 10:05:00, only 10:00 record qualifies
        assert base_15['cost_daily'] == '1.0'

    def test_unsorted_records_handled_correctly(self):
        """Records should be sorted internally, not rely on input order."""
        now = datetime(2026, 7, 16, 10, 20, 0, tzinfo=timezone.utc)
        records = [
            {'timestamp': '2026-07-16T10:10:00Z', 'complete': True, 'cost_daily': '3.0'},
            {'timestamp': '2026-07-16T10:00:00Z', 'complete': True, 'cost_daily': '1.0'},
            {'timestamp': '2026-07-16T10:05:00Z', 'complete': True, 'cost_daily': '2.0'},
        ]
        base_5, _ = _pick_baselines(records, now)
        assert base_5['cost_daily'] == '3.0'


# === _delta tests ===


class TestDelta:
    """Incremental cost computation."""

    def test_no_baseline_returns_full_daily(self):
        """No baseline (first run of day) → delta is the full daily cost."""
        result = _delta(10.5, None)
        assert result == 10.5

    def test_normal_increment(self):
        """Normal case: delta = current - baseline."""
        base = {'cost_daily': '5.0'}
        result = _delta(8.0, base)
        assert abs(result - 3.0) < 1e-9

    def test_negative_clamped_to_zero(self):
        """If current < baseline (region jitter), clamp to 0."""
        base = {'cost_daily': '10.0'}
        result = _delta(8.0, base)
        assert result == 0

    def test_exact_equal_returns_zero(self):
        """Same value → delta is 0."""
        base = {'cost_daily': '5.0'}
        result = _delta(5.0, base)
        assert result == 0

    def test_very_small_increment(self):
        """Small increments are preserved (not rounded away)."""
        base = {'cost_daily': '1.000001'}
        result = _delta(1.000005, base)
        assert result > 0
        assert abs(result - 0.000004) < 1e-9

    def test_baseline_cost_daily_is_string(self):
        """cost_daily is stored as string in DDB, _delta must parse it."""
        base = {'cost_daily': '12.345678'}
        result = _delta(15.0, base)
        assert abs(result - 2.654322) < 1e-6


# === _model_deltas tests ===


class TestModelDeltas:
    """Per-model token increment computation."""

    def test_no_baseline_returns_all_models(self):
        """No baseline → all current models appear in deltas."""
        models_now = {
            'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
        }
        result = _model_deltas(models_now, None)
        assert 'claude-sonnet-4' in result
        assert result['claude-sonnet-4'] == {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0}

    def test_baseline_without_models_daily_returns_all(self):
        """Baseline exists but has no models_daily field → same as no baseline."""
        models_now = {
            'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
        }
        base = {'cost_daily': '1.0'}  # no models_daily key
        result = _model_deltas(models_now, base)
        assert result['claude-sonnet-4']['input'] == 100

    def test_normal_increment_per_model(self):
        """Delta = current - baseline per model per type."""
        models_now = {
            'claude-sonnet-4': {'input': 200, 'output': 100, 'cache_read': 50, 'cache_write': 10},
        }
        base = {
            'models_daily': {
                'claude-sonnet-4': {'input': 150, 'output': 80, 'cache_read': 50, 'cache_write': 5},
            }
        }
        result = _model_deltas(models_now, base)
        assert result['claude-sonnet-4'] == {'input': 50, 'output': 20, 'cache_read': 0, 'cache_write': 5}

    def test_negative_clamped_per_type(self):
        """Negative per-type deltas clamped to 0 (region jitter)."""
        models_now = {
            'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
        }
        base = {
            'models_daily': {
                'claude-sonnet-4': {'input': 200, 'output': 30, 'cache_read': 0, 'cache_write': 0},
            }
        }
        result = _model_deltas(models_now, base)
        # input: max(100-200, 0) = 0, output: max(50-30, 0) = 20
        assert result['claude-sonnet-4'] == {'input': 0, 'output': 20, 'cache_read': 0, 'cache_write': 0}

    def test_model_with_zero_delta_excluded(self):
        """Models with all-zero deltas should not appear in result."""
        models_now = {
            'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
            'claude-opus-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
        }
        base = {
            'models_daily': {
                'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0},
                # opus not in baseline → full increment
            }
        }
        result = _model_deltas(models_now, base)
        assert 'claude-sonnet-4' not in result  # all zero delta
        assert 'claude-opus-4' in result

    def test_new_model_not_in_baseline(self):
        """A model that appeared after the baseline is fully included."""
        models_now = {
            'new-model': {'input': 500, 'output': 200, 'cache_read': 0, 'cache_write': 0},
        }
        base = {'models_daily': {}}
        result = _model_deltas(models_now, base)
        assert result['new-model'] == {'input': 500, 'output': 200, 'cache_read': 0, 'cache_write': 0}

    def test_handles_string_values_in_records(self):
        """DDB may store numbers as Decimal/int, test both int and str coercion."""
        models_now = {
            'claude-sonnet-4': {'input': '200', 'output': '100', 'cache_read': '0', 'cache_write': '0'},
        }
        base = {
            'models_daily': {
                'claude-sonnet-4': {'input': '150', 'output': '80', 'cache_read': '0', 'cache_write': '0'},
            }
        }
        result = _model_deltas(models_now, base)
        assert result['claude-sonnet-4'] == {'input': 50, 'output': 20, 'cache_read': 0, 'cache_write': 0}


# === Handler integration: delta-based alerting ===


class TestHandlerDeltaAlert:
    """Integration tests: verify delta logic triggers alerts correctly via handler."""

    @pytest.fixture
    def mock_env(self):
        """Set up mocks with query_by_pk for delta tests."""
        with patch('monitor.handler.get_monitor_enabled', return_value=True), \
             patch('monitor.handler.get_cost_thresholds', return_value={'5min': 5.0, '15min': 10.0, 'daily': 50.0}), \
             patch('monitor.handler.get_regions', return_value=['us-east-1']), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'test', 'url': 'http://hook', 'type': 'feishu'}]), \
             patch('monitor.handler.get_account_id', return_value='123456789012'), \
             patch('monitor.handler.query_by_pk', return_value=[]) as mock_query, \
             patch('monitor.handler.put_item') as mock_put, \
             patch('monitor.handler.fetch_region') as mock_fetch, \
             patch('monitor.handler.send_webhook_all') as mock_send, \
             patch('monitor.handler.get_alert_state', return_value=None), \
             patch('monitor.handler.set_alert_state'), \
             patch('monitor.handler.datetime') as mock_dt:
            # Fix now to 10:17:35 UTC so tests are deterministic
            fake_now = datetime(2026, 7, 16, 10, 17, 35, tzinfo=timezone.utc)
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            yield {
                'put_item': mock_put,
                'fetch_region': mock_fetch,
                'send_webhook_all': mock_send,
                'query_by_pk': mock_query,
                'now': fake_now,
            }

    def test_delta_alert_triggered_when_increment_exceeds_threshold(self, mock_env):
        """Delta exceeds 5min threshold → alert fires."""
        # current daily cost will be ~$30 (sonnet output 2M tokens × $15/MTok)
        models = {'claude-sonnet-4': {'input': 0, 'output': 2_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 2_000_000, '15min': 2_000_000, 'daily': 2_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        # Previous baseline: $20 daily cost
        mock_env['query_by_pk'].return_value = [
            {'SK': 'T#10:00', 'timestamp': '2026-07-16T10:00:00Z',
             'complete': True, 'cost_daily': '20.0',
             'models_daily': {'claude-sonnet-4': {'input': 0, 'output': 1_300_000, 'cache_read': 0, 'cache_write': 0}}},
        ]

        from monitor.handler import handler
        result = handler({}, None)

        # Delta = ~$30 - $20 = ~$10 > $5 threshold
        assert '5min' in result['alerts']
        mock_env['send_webhook_all'].assert_called()
        msg = mock_env['send_webhook_all'].call_args[0][0]
        assert '增量' in msg

    def test_no_alert_when_delta_under_threshold(self, mock_env):
        """Delta is below threshold → no alert."""
        models = {'claude-sonnet-4': {'input': 0, 'output': 2_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 2_000_000, '15min': 2_000_000, 'daily': 2_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        # now=10:17:35, cutoff for 15min = 10:02:35
        # Baselines close to current: 5min delta < $5, 15min delta < $10
        mock_env['query_by_pk'].return_value = [
            {'SK': 'T#10:00', 'timestamp': '2026-07-16T10:00:00Z',
             'complete': True, 'cost_daily': '28.0',
             'models_daily': {'claude-sonnet-4': {'input': 0, 'output': 1_850_000, 'cache_read': 0, 'cache_write': 0}}},
            {'SK': 'T#10:10', 'timestamp': '2026-07-16T10:10:00Z',
             'complete': True, 'cost_daily': '29.0',
             'models_daily': {'claude-sonnet-4': {'input': 0, 'output': 1_900_000, 'cache_read': 0, 'cache_write': 0}}},
        ]

        from monitor.handler import handler
        result = handler({}, None)

        assert result['alerts'] == []

    def test_incomplete_baseline_skipped(self, mock_env):
        """Incomplete records are not used as baselines (delta uses older valid one)."""
        models = {'claude-sonnet-4': {'input': 0, 'output': 2_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 2_000_000, '15min': 2_000_000, 'daily': 2_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        # Most recent record is incomplete → should use the earlier complete one
        mock_env['query_by_pk'].return_value = [
            {'SK': 'T#09:50', 'timestamp': '2026-07-16T09:50:00Z',
             'complete': True, 'cost_daily': '20.0',
             'models_daily': {'claude-sonnet-4': {'input': 0, 'output': 1_300_000, 'cache_read': 0, 'cache_write': 0}}},
            {'SK': 'T#10:00', 'timestamp': '2026-07-16T10:00:00Z',
             'complete': False, 'cost_daily': '25.0',
             'models_daily': {'claude-sonnet-4': {'input': 0, 'output': 1_600_000, 'cache_read': 0, 'cache_write': 0}}},
        ]

        from monitor.handler import handler
        result = handler({}, None)

        # Delta = ~$30 - $20 = ~$10 > $5 threshold
        assert '5min' in result['alerts']

    def test_warmup_protection_skips_5min_15min(self, mock_env):
        """Warm-up: records exist but no valid baseline → skip 5min/15min alerts."""
        models = {'claude-sonnet-4': {'input': 0, 'output': 5_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 5_000_000, '15min': 5_000_000, 'daily': 5_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        # Old records without cost_daily (pre-upgrade) → no valid baseline
        mock_env['query_by_pk'].return_value = [
            {'SK': 'T#10:00', 'timestamp': '2026-07-16T10:00:00Z', 'complete': True},
            {'SK': 'T#10:05', 'timestamp': '2026-07-16T10:05:00Z', 'complete': True},
        ]

        from monitor.handler import handler
        result = handler({}, None)

        # daily=$75 > $50 threshold should still fire; but 5min/15min suppressed by warm-up
        assert 'daily' in result['alerts']
        assert '5min' not in result['alerts']
        assert '15min' not in result['alerts']

    def test_first_run_of_day_no_records_uses_full_daily(self, mock_env):
        """Midnight first run: no records → delta = full daily cost."""
        models = {'claude-sonnet-4': {'input': 0, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 1_000_000, '15min': 1_000_000, 'daily': 1_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        # No records today
        mock_env['query_by_pk'].return_value = []

        from monitor.handler import handler
        result = handler({}, None)

        # $15 > $5 threshold → alert
        assert '5min' in result['alerts']

    def test_query_by_pk_failure_graceful_fallback(self, mock_env):
        """query_by_pk failure → delta uses no baseline (full daily), no crash."""
        models = {'claude-sonnet-4': {'input': 0, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 1_000_000, '15min': 1_000_000, 'daily': 1_000_000,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        mock_env['query_by_pk'].side_effect = Exception("DDB read failed")

        from monitor.handler import handler
        result = handler({}, None)

        # Should not crash, alert based on full daily
        assert result['statusCode'] == 200
        assert '5min' in result['alerts']

    def test_put_item_includes_new_delta_fields(self, mock_env):
        """put_item call includes cost_daily, models_daily, complete fields."""
        models = {'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 150, '15min': 150, 'daily': 150,
            'models': {'5min': models, '15min': models, 'daily': models},
        }
        mock_env['query_by_pk'].return_value = []

        from monitor.handler import handler
        handler({}, None)

        mock_env['put_item'].assert_called_once()
        kwargs = mock_env['put_item'].call_args[1]
        # cost_daily should be a string
        assert 'cost_daily' in kwargs
        assert isinstance(kwargs['cost_daily'], str)
        assert float(kwargs['cost_daily']) >= 0
        # models_daily should be a dict
        assert 'models_daily' in kwargs
        assert 'claude-sonnet-4' in kwargs['models_daily']
        # complete should be True (no failed regions)
        assert kwargs['complete'] is True

    def test_complete_false_when_regions_fail(self, mock_env):
        """When a region fails, complete=False in persisted record."""
        with patch('monitor.handler.get_regions', return_value=['us-east-1', 'us-west-2']):
            models = {'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0}}

            def fetch(region, *args, **kwargs):
                if region == 'us-west-2':
                    raise Exception("timeout")
                return {
                    'region': region, '5min': 150, '15min': 150, 'daily': 150,
                    'models': {'5min': models, '15min': models, 'daily': models},
                }
            mock_env['fetch_region'].side_effect = fetch

            from monitor.handler import handler
            handler({}, None)

        kwargs = mock_env['put_item'].call_args[1]
        assert kwargs['complete'] is False

    def test_alert_message_contains_incremental_top_models(self, mock_env):
        """Alert message shows top models by incremental cost, not cumulative."""
        # Two models: opus has big cumulative but small increment, sonnet has big increment
        models_daily = {
            'claude-opus-4': {'input': 0, 'output': 10_000_000, 'cache_read': 0, 'cache_write': 0},
            'claude-sonnet-4': {'input': 0, 'output': 2_000_000, 'cache_read': 0, 'cache_write': 0},
        }
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 12_000_000, '15min': 12_000_000, 'daily': 12_000_000,
            'models': {'5min': models_daily, '15min': models_daily, 'daily': models_daily},
        }
        # Baseline: opus already had 9.5M output, sonnet had 0
        # So delta: opus = 500K output ($7.5), sonnet = 2M output ($30) → sonnet is top
        mock_env['query_by_pk'].return_value = [
            {'SK': 'T#10:00', 'timestamp': '2026-07-16T10:00:00Z',
             'complete': True, 'cost_daily': '100.0',
             'models_daily': {
                 'claude-opus-4': {'input': 0, 'output': 9_500_000, 'cache_read': 0, 'cache_write': 0},
             }},
        ]

        from monitor.handler import handler
        result = handler({}, None)

        assert '5min' in result['alerts']
        msg = mock_env['send_webhook_all'].call_args[0][0]
        # Sonnet should appear before opus in top models (higher increment)
        sonnet_pos = msg.find('claude-sonnet-4')
        opus_pos = msg.find('claude-opus-4')
        assert sonnet_pos < opus_pos, f"Sonnet should be listed before opus in incremental top. msg={msg}"

    def test_baseline_read_happens_before_record_write(self, mock_env):
        """回归：基线必须在写入本轮记录之前读取。

        若先写后读，查回的最新记录是本轮自己（complete=True、cost_daily=当前值），
        基线==当前值 → delta 恒 0，5min 告警永久失效。
        """
        order = []
        mock_env['query_by_pk'].side_effect = lambda pk: order.append('read') or []
        mock_env['put_item'].side_effect = lambda *a, **kw: order.append('write')
        models = {'claude-sonnet-4': {'input': 100, 'output': 50, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 150, '15min': 150, 'daily': 150,
            'models': {'5min': models, '15min': models, 'daily': models},
        }

        from monitor.handler import handler
        handler({}, None)

        assert 'read' in order and 'write' in order
        assert order.index('read') < order.index('write'), \
            "baseline read must happen BEFORE this run's record is persisted"
