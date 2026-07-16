"""Extended unit tests for monitor/handler.py

Covers:
- extract_model_name: metric label normalization
- should_suppress: alert suppression logic for 5min/15min/daily windows
- mark_alerted: state writing
- Alert triggering: threshold checks, multi-region failure alert
- fetch_region: single-pass totals + per-window per-model detail, NextToken pagination
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call

from monitor.handler import extract_model_name, extract_token_type, should_suppress, mark_alerted


# === extract_model_name tests ===


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
             patch('monitor.handler.get_account_id', return_value='123456789012'), \
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
        with patch('monitor.handler.get_monitor_enabled', return_value=True), \
             patch('monitor.handler.get_cost_thresholds', return_value={'5min': 1e12, '15min': 1e12, 'daily': 1e12}), \
             patch('monitor.handler.get_regions', return_value=['us-east-1', 'us-west-2']), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook.example.com', 'type': 'feishu'}]), \
             patch('monitor.handler.get_account_id', return_value='123456789012'), \
             patch('monitor.handler.query_by_pk', return_value=[]) as mock_query, \
             patch('monitor.handler.put_item') as mock_put, \
             patch('monitor.handler.fetch_region') as mock_fetch, \
             patch('monitor.handler.send_webhook_all') as mock_send, \
             patch('monitor.handler.get_alert_state', return_value=None), \
             patch('monitor.handler.set_alert_state'):
            yield {
                'put_item': mock_put,
                'fetch_region': mock_fetch,
                'send_webhook_all': mock_send,
                'query_by_pk': mock_query,
            }

    def test_no_alert_when_under_all_thresholds(self, mock_env):
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 100, '15min': 400, 'daily': 900,
            'models': {'5min': {}, '15min': {}, 'daily': {}},
        }
        from monitor.handler import handler
        result = handler({}, None)
        assert result['statusCode'] == 200
        assert result['alerts'] == []
        mock_env['send_webhook_all'].assert_not_called()

    def test_alert_triggered_when_5min_cost_exceeds_threshold(self, mock_env):
        # sonnet output $15/MTok × 1M × 2 regions = $30 estimated cost
        models = {'claude-sonnet-4': {'input': 0, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0}}
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 1_000_000, '15min': 400, 'daily': 900,
            'models': {'5min': models, '15min': {}, 'daily': models},
        }
        with patch('monitor.handler.get_cost_thresholds', return_value={'5min': 10, '15min': 1e12, 'daily': 1e12}):
            from monitor.handler import handler
            result = handler({}, None)
        # 2 regions × $15 = $30 > $10 threshold
        assert '5min' in result['alerts']
        mock_env['send_webhook_all'].assert_called()
        # 告警文案以 $ 计
        assert '$' in mock_env['send_webhook_all'].call_args[0][0]

    def test_unconfigured_cost_thresholds_notifies(self, mock_env):
        """未配置费用阈值时，直接通知用户（每日去重）。"""
        mock_env['fetch_region'].return_value = {
            'region': 'us-east-1', '5min': 0, '15min': 0, 'daily': 0,
            'models': {'5min': {}, '15min': {}, 'daily': {}},
        }
        with patch('monitor.handler.get_cost_thresholds', return_value={}):
            from monitor.handler import handler
            result = handler({}, None)
        assert result.get('cost_thresholds_configured') is False
        calls = mock_env['send_webhook_all'].call_args_list
        assert any('未配置' in c[0][0] for c in calls)

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

    def test_few_region_failures_send_daily_deduped_notice(self, mock_env):
        """#3：1~3 个 region 失败时也发一次每日去重提醒（原先 <=3 完全静默）。"""
        def fetch(region, *args, **kwargs):
            if region == 'us-west-2':
                raise Exception("timeout")
            return {'region': region, '5min': 100, '15min': 400, 'daily': 900,
                    'models': {'5min': {}, '15min': {}, 'daily': {}}}
        mock_env['fetch_region'].side_effect = fetch
        from monitor.handler import handler
        handler({}, None)

        calls = mock_env['send_webhook_all'].call_args_list
        notices = [c for c in calls if 'us-west-2' in c[0][0] and '费用可能偏低' in c[0][0]]
        assert len(notices) == 1

    def test_cost_alert_annotates_failed_regions(self, mock_env):
        """#3：成本告警文案标注有 region 数据缺失、实际费用可能更高。"""
        models = {'claude-sonnet-4': {'input': 0, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0}}
        def fetch(region, *args, **kwargs):
            if region == 'us-west-2':
                raise Exception("timeout")
            return {'region': region, '5min': 1_000_000, '15min': 400, 'daily': 900,
                    'models': {'5min': models, '15min': {}, 'daily': models}}
        mock_env['fetch_region'].side_effect = fetch
        with patch('monitor.handler.get_cost_thresholds', return_value={'5min': 10, '15min': 1e12, 'daily': 1e12}):
            from monitor.handler import handler
            handler({}, None)

        calls = mock_env['send_webhook_all'].call_args_list
        cost_alerts = [c for c in calls if '费用提醒' in c[0][0]]
        assert len(cost_alerts) == 1
        assert 'Region 查询失败' in cost_alerts[0][0][0]

    def test_threshold_read_failure_sends_alert(self):
        """When cost threshold read fails, an alert is sent and handler returns 500."""
        with patch('monitor.handler.get_monitor_enabled', return_value=True), \
             patch('monitor.handler.get_cost_thresholds', side_effect=Exception("DDB timeout")), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook', 'type': 'feishu'}]), \
             patch('monitor.handler.get_account_id', return_value='123456789012'), \
             patch('monitor.handler.send_webhook_all') as mock_send:
            from monitor.handler import handler
            result = handler({}, None)
        assert result['statusCode'] == 500
        assert '阈值失败' in mock_send.call_args[0][0]

    def test_no_regions_configured_sends_alert(self):
        """When no regions configured, sends alert and returns 500."""
        with patch('monitor.handler.get_monitor_enabled', return_value=True), \
             patch('monitor.handler.get_cost_thresholds', return_value={'5min': 1e12, '15min': 1e12, 'daily': 1e12}), \
             patch('monitor.handler.get_regions', return_value=[]), \
             patch('monitor.handler.get_webhook_config', return_value=[{'name': 'feishu', 'url': 'https://hook', 'type': 'feishu'}]), \
             patch('monitor.handler.get_account_id', return_value='123456789012'), \
             patch('monitor.handler.send_webhook_all') as mock_send:
            from monitor.handler import handler
            result = handler({}, None)
        assert result['statusCode'] == 500
        assert 'Region' in mock_send.call_args[0][0]


# === fetch_region single-pass: totals + per-window detail + pagination ===


def _bedrock_series(model, token_metric, timestamps, values):
    """构造一条 AWS/Bedrock SEARCH 返回的 MetricDataResult。"""
    return {
        'Id': 'bedrock',
        'Label': f'AWS/Bedrock {model} {token_metric}',
        'Timestamps': list(timestamps),
        'Values': list(values),
    }


class TestFetchRegionSinglePass:
    """fetch_region 一次拉取即算出 5min/15min/daily 总量与各窗口每模型明细。"""

    def _run(self, pages):
        """用给定的分页响应驱动 fetch_region，返回结果。pages 为多页 MetricDataResults 列表。"""
        mock_cw = MagicMock()
        responses = []
        for i, results in enumerate(pages):
            resp = {'MetricDataResults': results}
            if i < len(pages) - 1:
                resp['NextToken'] = f'tok{i}'
            responses.append(resp)
        mock_cw.get_metric_data.side_effect = responses

        # 固定窗口：now=12:12:00，start_daily=00:00, start_15min=11:57, start_5min=12:07
        now = datetime(2024, 7, 1, 12, 12, 0, tzinfo=timezone.utc)
        start_daily = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_15min = now - timedelta(minutes=15)
        start_5min = now - timedelta(minutes=5)

        with patch('monitor.handler.boto3.session.Session') as mock_session_cls:
            mock_session_cls.return_value.client.return_value = mock_cw
            from monitor.handler import fetch_region
            result = fetch_region('us-east-1', start_daily, start_15min, start_5min, now)
        return result, mock_cw

    def test_windows_bucketed_by_timestamp(self):
        """同一模型跨三个时间段的数据点，应正确归入 daily/15min/5min 三个窗口。"""
        t_daily = datetime(2024, 7, 1, 3, 0, 0, tzinfo=timezone.utc)    # 只进 daily
        t_15min = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)   # 进 daily+15min
        t_5min = datetime(2024, 7, 1, 12, 10, 0, tzinfo=timezone.utc)   # 进全部三个
        series = _bedrock_series('claude-sonnet-4', 'InputTokenCount',
                                 [t_daily, t_15min, t_5min], [100, 20, 5])
        result, _ = self._run([[series]])

        assert result['daily'] == 125   # 100+20+5
        assert result['15min'] == 25    # 20+5
        assert result['5min'] == 5      # 5
        assert result['models']['daily']['claude-sonnet-4']['input'] == 125
        assert result['models']['15min']['claude-sonnet-4']['input'] == 25
        assert result['models']['5min']['claude-sonnet-4']['input'] == 5

    def test_total_equals_sum_of_per_model_series(self):
        """总量 = 所有 per-model 序列（含 cache 类型、多模型）之和，验证合并口径。"""
        t = datetime(2024, 7, 1, 12, 10, 0, tzinfo=timezone.utc)  # 落在 5min 窗口
        series = [
            _bedrock_series('claude-sonnet-4', 'InputTokenCount', [t], [10]),
            _bedrock_series('claude-sonnet-4', 'OutputTokenCount', [t], [4]),
            _bedrock_series('claude-sonnet-4', 'CacheReadInputTokenCount', [t], [3]),
            _bedrock_series('claude-sonnet-4', 'CacheWriteInputTokenCount', [t], [2]),
            _bedrock_series('claude-opus-4', 'InputTokenCount', [t], [1]),
            {'Id': 'mantle', 'Label': 'AWS/BedrockMantle openai.gpt-5 TotalInputTokens', 'Timestamps': [t], 'Values': [7]},
            {'Id': 'mantle', 'Label': 'AWS/BedrockMantle openai.gpt-5 TotalOutputTokens', 'Timestamps': [t], 'Values': [6]},
        ]
        result, _ = self._run([series])

        # 总量应等于全部序列之和，且 cache 计入总量
        assert result['5min'] == 10 + 4 + 3 + 2 + 1 + 7 + 6  # 33
        # 每模型每类型明细正确
        sonnet = result['models']['5min']['claude-sonnet-4']
        assert sonnet == {'input': 10, 'output': 4, 'cache_read': 3, 'cache_write': 2}
        assert result['models']['5min']['claude-opus-4']['input'] == 1
        # mantle 按模型拆分，TotalInputTokens→input, TotalOutputTokens→output
        gpt = result['models']['5min']['openai.gpt-5']
        assert gpt['input'] == 7 and gpt['output'] == 6

    def test_zero_values_skipped(self):
        """值为 0 的数据点不计入总量，也不创建空模型项。"""
        t = datetime(2024, 7, 1, 12, 10, 0, tzinfo=timezone.utc)
        series = _bedrock_series('claude-zero', 'InputTokenCount', [t], [0])
        result, _ = self._run([[series]])
        assert result['5min'] == 0
        assert 'claude-zero' not in result['models']['5min']

    def test_nexttoken_pagination_consumed(self):
        """跨两页返回的数据都应被累加（NextToken 循环）。"""
        t = datetime(2024, 7, 1, 12, 10, 0, tzinfo=timezone.utc)
        page1 = [_bedrock_series('claude-sonnet-4', 'InputTokenCount', [t], [10])]
        page2 = [_bedrock_series('claude-opus-4', 'InputTokenCount', [t], [30])]
        result, mock_cw = self._run([page1, page2])

        # 两页都被消费
        assert mock_cw.get_metric_data.call_count == 2
        # 第二次调用带上了第一页返回的 NextToken
        second_call_kwargs = mock_cw.get_metric_data.call_args_list[1][1]
        assert second_call_kwargs.get('NextToken') == 'tok0'
        # 两页数据都计入总量与明细
        assert result['5min'] == 40
        assert result['models']['5min']['claude-sonnet-4']['input'] == 10
        assert result['models']['5min']['claude-opus-4']['input'] == 30


# === fetch_region midnight bucket fix tests ===


class TestFetchRegionMidnight:
    """fetch_region 午夜场景：query_start 扩展 + daily 过滤。"""

    def _run(self, pages, start_daily, start_15min, start_5min, end, bucket_end=None):
        """可自定义窗口参数的 fetch_region 驱动。"""
        mock_cw = MagicMock()
        responses = []
        for i, results in enumerate(pages):
            resp = {'MetricDataResults': results}
            if i < len(pages) - 1:
                resp['NextToken'] = f'tok{i}'
            responses.append(resp)
        mock_cw.get_metric_data.side_effect = responses

        with patch('monitor.handler.boto3.session.Session') as mock_session_cls:
            mock_session_cls.return_value.client.return_value = mock_cw
            from monitor.handler import fetch_region
            result = fetch_region('us-east-1', start_daily, start_15min, start_5min, end, bucket_end)
        return result, mock_cw

    def test_midnight_query_start_extends_to_previous_day(self):
        """00:02 运行：start_15min=昨日 23:47 < start_daily=今日 00:00，
        断言 StartTime == start_15min（查询窗口扩展到前一天）。"""
        start_daily = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)
        start_15min = datetime(2026, 7, 15, 23, 45, 0, tzinfo=timezone.utc)
        start_5min = datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 16, 0, 2, 0, tzinfo=timezone.utc)
        bucket_end = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

        series = _bedrock_series('claude-sonnet-4', 'InputTokenCount',
                                 [datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)], [100])
        _, mock_cw = self._run([[series]], start_daily, start_15min, start_5min, end, bucket_end)

        call_kwargs = mock_cw.get_metric_data.call_args[1]
        assert call_kwargs['StartTime'] == start_15min  # 扩展到前一天

    def test_midnight_yesterday_bucket_in_5min_not_daily(self):
        """午夜场景：ts=昨日 23:55 的数据点应计入 5min 和 15min，不计入 daily。"""
        start_daily = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)
        start_15min = datetime(2026, 7, 15, 23, 45, 0, tzinfo=timezone.utc)
        start_5min = datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 16, 0, 2, 0, tzinfo=timezone.utc)
        bucket_end = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

        ts_yesterday = datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)
        series = _bedrock_series('claude-sonnet-4', 'InputTokenCount',
                                 [ts_yesterday], [200])
        result, _ = self._run([[series]], start_daily, start_15min, start_5min, end, bucket_end)

        # ts < start_daily → 不计入 daily
        assert result['daily'] == 0
        assert 'claude-sonnet-4' not in result['models']['daily']
        # ts >= start_15min 且 ts < bucket_end → 计入 15min
        assert result['15min'] == 200
        assert result['models']['15min']['claude-sonnet-4']['input'] == 200
        # ts >= start_5min 且 ts < bucket_end → 计入 5min
        assert result['5min'] == 200
        assert result['models']['5min']['claude-sonnet-4']['input'] == 200

    def test_midnight_mixed_yesterday_and_today(self):
        """午夜场景：前一天和今天的数据点混合，验证正确分配。"""
        start_daily = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)
        start_15min = datetime(2026, 7, 15, 23, 45, 0, tzinfo=timezone.utc)
        start_5min = datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 16, 0, 2, 0, tzinfo=timezone.utc)
        bucket_end = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

        ts_yesterday = datetime(2026, 7, 15, 23, 55, 0, tzinfo=timezone.utc)
        ts_today = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)  # == bucket_end → excluded from 5min/15min
        series = _bedrock_series('claude-sonnet-4', 'InputTokenCount',
                                 [ts_yesterday, ts_today], [100, 50])
        result, _ = self._run([[series]], start_daily, start_15min, start_5min, end, bucket_end)

        # daily 只含今天的: ts_today >= start_daily
        assert result['daily'] == 50
        # 15min: ts_yesterday >= start_15min 且 < bucket_end → 计入; ts_today >= bucket_end → 排除
        assert result['15min'] == 100
        # 5min: ts_yesterday >= start_5min 且 < bucket_end → 计入; ts_today >= bucket_end → 排除
        assert result['5min'] == 100

    def test_midday_query_start_unchanged(self):
        """中午运行：start_daily < start_15min，query_start == start_daily（回归保护）。"""
        start_daily = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)
        start_15min = datetime(2026, 7, 16, 11, 57, 0, tzinfo=timezone.utc)
        start_5min = datetime(2026, 7, 16, 12, 7, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 16, 12, 12, 0, tzinfo=timezone.utc)

        series = _bedrock_series('claude-sonnet-4', 'InputTokenCount',
                                 [datetime(2026, 7, 16, 3, 0, 0, tzinfo=timezone.utc)], [10])
        _, mock_cw = self._run([[series]], start_daily, start_15min, start_5min, end)

        call_kwargs = mock_cw.get_metric_data.call_args[1]
        assert call_kwargs['StartTime'] == start_daily  # 白天行为不变
