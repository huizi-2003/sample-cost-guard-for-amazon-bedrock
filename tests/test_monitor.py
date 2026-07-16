"""Unit tests for Monitor Lambda persistence logic.

Validates Requirements 3.4, 3.5, 3.6:
- put_item is called with correct PK/SK format after successful aggregation
- put_item is NOT called when region_results is empty
- DDB write failure is logged but does not prevent alert evaluation

Also tests bucket boundary alignment for 5min/15min windows.
"""
import re
import pytest
from datetime import datetime, timezone, timedelta
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
         patch('monitor.handler.get_account_id', return_value='123456789012'), \
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


class TestBucketBoundaryAlignment:
    """Tests that 5min/15min windows use exactly one complete bucket (no overlap, no gap).

    CloudWatch aligns metric buckets to Period=300 from UTC midnight (00:00, 00:05, 00:10, ...).
    The monitor must:
      - bucket_end = floor(now) → most recent closed bucket boundary
      - start_5min = bucket_end - 300s → exactly 1 complete bucket
      - start_15min = bucket_end - 900s → exactly 3 complete buckets
      - fetch_region filters: start <= ts < bucket_end (upper bound excludes unclosed bucket)

    This avoids both:
      - Systematic undercount (old bug: unaligned start skips straddling bucket)
      - Systematic overcount (partial fix: no upper bound → unclosed bucket counted twice)
    """

    def _run_handler_and_capture_args(self, fake_now):
        """Run handler with a fake `now` and capture all args passed to fetch_region."""
        captured = {}

        def capture_fetch_region(region, start_daily, start_15min, start_5min, end, bucket_end=None):
            captured['start_daily'] = start_daily
            captured['start_15min'] = start_15min
            captured['start_5min'] = start_5min
            captured['end'] = end
            captured['bucket_end'] = bucket_end
            return {'region': region, '5min': 0, '15min': 0, 'daily': 0, 'models': {}}

        with patch('monitor.handler.get_monitor_enabled', return_value=True), \
             patch('monitor.handler.get_cost_thresholds', return_value={'5min': 1e12, '15min': 1e12, 'daily': 1e12}), \
             patch('monitor.handler.get_regions', return_value=['us-east-1']), \
             patch('monitor.handler.get_webhook_config', return_value=[]), \
             patch('monitor.handler.put_item'), \
             patch('monitor.handler.fetch_region', side_effect=capture_fetch_region) as mock_fr, \
             patch('monitor.handler.send_webhook_all'), \
             patch('monitor.handler.get_alert_state', return_value=None), \
             patch('monitor.handler.set_alert_state'), \
             patch('monitor.handler.datetime') as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            from monitor.handler import handler
            handler({}, None)

        return captured

    def test_5min_window_is_one_complete_bucket(self):
        """10:07:35 → bucket_end=10:05, start_5min=10:00. Exactly bucket [10:00, 10:05)."""
        fake_now = datetime(2026, 7, 16, 10, 7, 35, tzinfo=timezone.utc)
        captured = self._run_handler_and_capture_args(fake_now)

        assert captured['bucket_end'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)
        assert captured['start_5min'] == datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)

    def test_15min_window_is_three_complete_buckets(self):
        """10:07:35 → bucket_end=10:05, start_15min=09:50. Buckets [09:50, 09:55, 10:00)."""
        fake_now = datetime(2026, 7, 16, 10, 7, 35, tzinfo=timezone.utc)
        captured = self._run_handler_and_capture_args(fake_now)

        assert captured['bucket_end'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)
        assert captured['start_15min'] == datetime(2026, 7, 16, 9, 50, 0, tzinfo=timezone.utc)

    def test_trigger_exactly_on_boundary(self):
        """10:10:00 → bucket_end=10:10, start_5min=10:05. Bucket [10:05, 10:10)."""
        fake_now = datetime(2026, 7, 16, 10, 10, 0, tzinfo=timezone.utc)
        captured = self._run_handler_and_capture_args(fake_now)

        assert captured['bucket_end'] == datetime(2026, 7, 16, 10, 10, 0, tzinfo=timezone.utc)
        assert captured['start_5min'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)

    def test_worst_case_phase_just_after_boundary(self):
        """10:05:30 → bucket_end=10:05, start_5min=10:00. Bucket [10:00, 10:05)."""
        fake_now = datetime(2026, 7, 16, 10, 5, 30, tzinfo=timezone.utc)
        captured = self._run_handler_and_capture_args(fake_now)

        assert captured['bucket_end'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)
        assert captured['start_5min'] == datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)

    def test_start_daily_unchanged(self):
        """start_daily should remain midnight (already naturally aligned)."""
        fake_now = datetime(2026, 7, 16, 10, 7, 35, tzinfo=timezone.utc)
        captured = self._run_handler_and_capture_args(fake_now)

        assert captured['start_daily'] == datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

    def test_consecutive_runs_no_overlap_no_gap(self):
        """Two consecutive runs 5min apart should cover adjacent non-overlapping buckets."""
        # First run at 10:07:35 → covers bucket [10:00, 10:05)
        captured1 = self._run_handler_and_capture_args(
            datetime(2026, 7, 16, 10, 7, 35, tzinfo=timezone.utc))
        # Second run at 10:12:35 → covers bucket [10:05, 10:10)
        captured2 = self._run_handler_and_capture_args(
            datetime(2026, 7, 16, 10, 12, 35, tzinfo=timezone.utc))

        # No overlap: first's bucket_end == second's start_5min
        assert captured1['bucket_end'] == captured2['start_5min'], \
            "Adjacent runs should have contiguous, non-overlapping windows"
        # No gap: first's bucket_end is exactly second's start
        assert captured1['bucket_end'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)
        assert captured2['start_5min'] == datetime(2026, 7, 16, 10, 5, 0, tzinfo=timezone.utc)
