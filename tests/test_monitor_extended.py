"""Extended unit tests for monitor/handler.py

Covers:
- clean_label: metric label normalization
- should_suppress: alert suppression logic for 5min/15min/daily windows
- mark_alerted: state writing
- Alert triggering: threshold checks, multi-region failure alert
- fetch_detail: time alignment logic
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call

from monitor.handler import extract_model_name, extract_token_type, should_suppress, mark_alerted, DETAIL_PERIOD


# === extract_model_name tests (formerly clean_label) ===


class TestCleanLabel:
    """extract_model_name strips namespace prefix and metric name suffix."""

    def test_bedrock_namespace_prefix_removed(self):
        assert 'AWS/Bedrock' not in extract_model_name('AWS/Bedrock claude-sonnet-4 InputTokenCount')

    def test_bedrock_mantle_prefix_removed(self):
        assert 'AWS/BedrockMantle' not in extract_model_name('AWS/BedrockMantle claude-opus-4 TotalInputTokens')

    def test_input_token_count_suffix_removed(self):
        result = extract_model_name('AWS/Bedrock claude-sonnet-4 InputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_output_token_count_suffix_removed(self):
        result = extract_model_name('AWS/Bedrock claude-sonnet-4 OutputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_cache_read_suffix_removed(self):
        result = extract_model_name('AWS/Bedrock claude-sonnet-4 CacheReadInputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_cache_write_suffix_removed(self):
        result = extract_model_name('AWS/Bedrock claude-sonnet-4 CacheWriteInputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_mantle_total_input_tokens_suffix(self):
        result = extract_model_name('AWS/BedrockMantle claude-opus-4 TotalInputTokens')
        assert result == 'claude-opus-4'

    def test_mantle_total_output_tokens_suffix(self):
        result = extract_model_name('AWS/BedrockMantle claude-opus-4 TotalOutputTokens')
        assert result == 'claude-opus-4'

    def test_global_anthropic_prefix_removed(self):
        result = extract_model_name('AWS/Bedrock global.anthropic.claude-sonnet-4 InputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_anthropic_prefix_removed(self):
        result = extract_model_name('AWS/Bedrock anthropic.claude-sonnet-4 InputTokenCount')
        assert result == 'claude-sonnet-4'

    def test_label_without_known_suffix(self):
        # If no known suffix matches, label is returned cleaned of namespace only
        result = extract_model_name('AWS/Bedrock some-model UnknownMetric')
        assert result == 'some-model UnknownMetric'

    def test_tokens_suffix_removed(self):
        result = extract_model_name('AWS/BedrockMantle some-model Tokens')
        assert result == 'some-model'


class TestExtractTokenType:
    """extract_token_type correctly identifies token type from label."""

    def test_input(self):
        assert extract_token_type('AWS/Bedrock claude-sonnet-4 InputTokenCount') == 'input'

    def test_output(self):
        assert extract_token_type('AWS/Bedrock claude-sonnet-4 OutputTokenCount') == 'output'

    def test_cache_read(self):
        assert extract_token_type('AWS/Bedrock claude-sonnet-4 CacheReadInputTokenCount') == 'cache_read'

    def test_cache_write(self):
        assert extract_token_type('AWS/Bedrock claude-sonnet-4 CacheWriteInputTokenCount') == 'cache_write'

    def test_mantle_total_input(self):
        assert extract_token_type('AWS/BedrockMantle claude-opus-4 TotalInputTokens') == 'input'

    def test_mantle_total_output(self):
        assert extract_token_type('AWS/BedrockMantle claude-opus-4 TotalOutputTokens') == 'output'

    def test_unknown_defaults_to_input(self):
        assert extract_token_type('AWS/Bedrock some-model Tokens') == 'input'


# === should_suppress tests ===


class TestShouldSuppress:
    """Alert suppression logic for different time windows."""

    def test_5min_never_suppressed(self):
        """5min alerts should never be suppressed regardless of state."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.get_alert_state', return_value='anything'):
            result = should_suppress('5min', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is False

    def test_daily_suppressed_if_already_alerted_today(self):
        """Daily alert suppressed if already alerted today."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.get_alert_state', return_value='2024-07-01'):
            result = should_suppress('daily', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is True

    def test_daily_not_suppressed_if_alerted_yesterday(self):
        """Daily alert not suppressed if last alert was yesterday."""
        now = datetime(2024, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.get_alert_state', return_value='2024-07-01'):
            result = should_suppress('daily', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is False

    def test_15min_suppressed_within_window(self):
        """15min alert suppressed if last alert was less than 15 minutes ago."""
        now = datetime(2024, 7, 1, 12, 10, 0, tzinfo=timezone.utc)
        # 5 minutes ago
        last_alert = str((now - timedelta(minutes=5)).timestamp())
        with patch('monitor.handler.get_alert_state', return_value=last_alert):
            result = should_suppress('15min', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is True

    def test_15min_not_suppressed_outside_window(self):
        """15min alert not suppressed if last alert was more than 15 minutes ago."""
        now = datetime(2024, 7, 1, 12, 30, 0, tzinfo=timezone.utc)
        # 20 minutes ago
        last_alert = str((now - timedelta(minutes=20)).timestamp())
        with patch('monitor.handler.get_alert_state', return_value=last_alert):
            result = should_suppress('15min', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is False

    def test_no_prior_state_not_suppressed(self):
        """No prior alert state means not suppressed."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.get_alert_state', return_value=None):
            result = should_suppress('daily', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is False

    def test_corrupted_state_sends_alert_and_not_suppressed(self):
        """Corrupted state data should send a warning and not suppress."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.get_alert_state', return_value='not-a-timestamp'), \
             patch('monitor.handler.send_webhook_all') as mock_send:
            result = should_suppress('15min', now, [{'url': 'http://hook', 'type': 'feishu'}])
        assert result is False
        mock_send.assert_called_once()
        assert '损坏' in mock_send.call_args[0][0]


# === mark_alerted tests ===


class TestMarkAlerted:
    """mark_alerted writes appropriate state values."""

    def test_5min_does_not_write_state(self):
        """5min window should not write any alert state."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.set_alert_state') as mock_set:
            mark_alerted('5min', now)
        mock_set.assert_not_called()

    def test_daily_writes_date_string(self):
        """Daily window stores today's date."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.set_alert_state') as mock_set:
            mark_alerted('daily', now)
        mock_set.assert_called_once_with('daily', '2024-07-01')

    def test_15min_writes_timestamp(self):
        """15min window stores current timestamp."""
        now = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch('monitor.handler.set_alert_state') as mock_set:
            mark_alerted('15min', now)
        mock_set.assert_called_once_with('15min', str(now.timestamp()))


# === Handler integration: alert triggers ===


class TestHandlerAlerts:
    """Handler-level alert triggering tests."""

    @pytest.fixture
    def mock_env(self):
        """Set up common mocks for handler tests."""
        with patch('monitor.handler.get_thresholds', return_value={'5min': 1000, '15min': 5000, 'daily': 10000}), \
             patch('monitor.handler.get_regions', return_value=['us-east-1', 'us-west-2']), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook.example.com', 'type': 'feishu'}]), \
             patch('monitor.handler.put_item') as mock_put, \
             patch('monitor.handler.fetch_region') as mock_fetch, \
             patch('monitor.handler.fetch_detail', return_value={}) as mock_detail, \
             patch('monitor.handler.send_webhook_all') as mock_send, \
             patch('monitor.handler.get_alert_state', return_value=None), \
             patch('monitor.handler.set_alert_state'):
            yield {
                'put_item': mock_put,
                'fetch_region': mock_fetch,
                'fetch_detail': mock_detail,
                'send_webhook_all': mock_send,
            }

    def test_no_alert_when_under_all_thresholds(self, mock_env):
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 100, '15min': 400, 'daily': 900
        }
        from monitor.handler import handler
        result = handler({}, None)
        assert result['statusCode'] == 200
        assert result['alerts'] == []
        mock_env['send_webhook_all'].assert_not_called()

    def test_alert_triggered_when_5min_exceeds_threshold(self, mock_env):
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 600, '15min': 400, 'daily': 900
        }
        from monitor.handler import handler
        result = handler({}, None)
        # 2 regions × 600 = 1200 > 1000 threshold
        assert '5min' in result['alerts']
        mock_env['send_webhook_all'].assert_called()

    def test_multi_region_failure_sends_alert(self, mock_env):
        """When >3 regions fail, a failure alert is sent."""
        with patch('monitor.handler.get_regions', return_value=['r1', 'r2', 'r3', 'r4', 'r5']):
            mock_env['fetch_region'].side_effect = Exception("timeout")
            from monitor.handler import handler
            result = handler({}, None)

        # Should have sent a failure alert
        calls = mock_env['send_webhook_all'].call_args_list
        failure_alerts = [c for c in calls if '查询失败' in c[0][0]]
        assert len(failure_alerts) >= 1

    def test_threshold_read_failure_sends_alert(self):
        """When threshold read fails, an alert is sent and handler returns 500."""
        with patch('monitor.handler.get_thresholds', side_effect=Exception("DDB timeout")), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook', 'type': 'feishu'}]), \
             patch('monitor.handler.send_webhook_all') as mock_send:
            from monitor.handler import handler
            result = handler({}, None)
        assert result['statusCode'] == 500
        assert '阈值失败' in mock_send.call_args[0][0]

    def test_no_regions_configured_sends_alert(self):
        """When no regions configured, sends alert and returns 500."""
        with patch('monitor.handler.get_thresholds', return_value={'5min': 1000, '15min': 5000, 'daily': 10000}), \
             patch('monitor.handler.get_regions', return_value=[]), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook', 'type': 'feishu'}]), \
             patch('monitor.handler.send_webhook_all') as mock_send:
            from monitor.handler import handler
            result = handler({}, None)
        assert result['statusCode'] == 500
        assert 'Region' in mock_send.call_args[0][0]


# === fetch_detail time alignment ===


class TestFetchDetailAlignment:
    """fetch_detail aligns start time to Period boundary."""

    @patch('monitor.handler.boto3.session.Session')
    def test_start_aligned_to_period_boundary(self, mock_session_cls):
        """Start time should be aligned down to Period boundary minus one period."""
        mock_cw = MagicMock()
        mock_cw.get_metric_data.return_value = {'MetricDataResults': []}
        mock_session_cls.return_value.client.return_value = mock_cw

        from monitor.handler import fetch_detail
        # 12:03:45 → aligned to floor(12:03:45 / 300s) - 1 period = 12:00:00 - 5min = 11:55:00
        start = datetime(2024, 7, 1, 12, 3, 45, tzinfo=timezone.utc)
        end = datetime(2024, 7, 1, 12, 5, 0, tzinfo=timezone.utc)
        fetch_detail('us-east-1', start, end)

        call_kwargs = mock_cw.get_metric_data.call_args[1]
        aligned_start = call_kwargs['StartTime']
        # aligned_start should be at a 5-minute boundary before start
        assert aligned_start.second == 0
        assert aligned_start < start
        assert (int(aligned_start.timestamp()) % DETAIL_PERIOD) == 0
