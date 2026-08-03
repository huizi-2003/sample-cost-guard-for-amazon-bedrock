"""Extended unit tests for reconciler/handler.py utility functions.

Covers:
- is_token_usage: token vs non-token usage type detection
- extract_region_from_usage_type: prefix → region mapping
- get_regions_from_ce: deduplicate and sort regions from CE results
- reconcile_one: integration test with mocked CE/CW
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from reconciler.handler import (
    is_token_usage,
    extract_region_from_usage_type,
    get_regions_from_ce,
    USAGE_PREFIX_TO_REGION,
    reconcile_one,
)


# === is_token_usage tests ===


class TestIsTokenUsage:
    """is_token_usage: identifies token-bearing usage types."""

    def test_input_tokens(self):
        assert is_token_usage('USE1-Claude4.6Opus-input-tokens-cross-region-global') is True

    def test_output_tokens(self):
        assert is_token_usage('USW2-Claude4.6Opus-output-tokens') is True

    def test_input_token_count(self):
        assert is_token_usage('USE1-Claude4.6Opus-cache-read-input-token-count-cross-region-global') is True

    def test_cache_write_tokens(self):
        assert is_token_usage('EUW1-claude-sonnet-4-cache-write-tokens') is True

    def test_mantle_tokens(self):
        assert is_token_usage('USW2-anthropic.claude-opus-4-8-mantle-input-tokens-global-standard') is True

    def test_searchunits_excluded(self):
        """searchunits should NOT be counted as token usage."""
        assert is_token_usage('USE1-BedrockKnowledgeBase-searchunits-token') is False

    def test_non_token_usage_type(self):
        assert is_token_usage('USE1-BedrockKnowledgeBase-searchunits') is False

    def test_case_insensitive(self):
        assert is_token_usage('USE1-Model-INPUT-TOKENS') is True

    def test_token_count_variant(self):
        assert is_token_usage('APN1-model-output-token-count') is True


# === extract_region_from_usage_type tests ===


class TestExtractRegionFromUsageType:
    """extract_region_from_usage_type: maps prefix to AWS region name."""

    def test_use1_maps_to_us_east_1(self):
        assert extract_region_from_usage_type('USE1-Claude-input-tokens') == 'us-east-1'

    def test_usw2_maps_to_us_west_2(self):
        assert extract_region_from_usage_type('USW2-Claude-input-tokens') == 'us-west-2'

    def test_euw1_maps_to_eu_west_1(self):
        assert extract_region_from_usage_type('EUW1-Claude-input-tokens') == 'eu-west-1'

    def test_euc1_maps_to_eu_central_1(self):
        assert extract_region_from_usage_type('EUC1-Claude-input-tokens') == 'eu-central-1'

    def test_apn1_maps_to_ap_northeast_1(self):
        assert extract_region_from_usage_type('APN1-Claude-input-tokens') == 'ap-northeast-1'

    def test_aps1_maps_to_ap_southeast_1(self):
        assert extract_region_from_usage_type('APS1-Claude-input-tokens') == 'ap-southeast-1'

    def test_unknown_prefix_returns_none(self):
        assert extract_region_from_usage_type('XXX1-Claude-input-tokens') is None

    def test_case_insensitive_prefix(self):
        """Prefix matching should be case-insensitive."""
        assert extract_region_from_usage_type('use1-Claude-input-tokens') == 'us-east-1'

    def test_no_dash_returns_none(self):
        """Usage type without dash returns None (can't extract prefix)."""
        assert extract_region_from_usage_type('nodashhere') is None

    def test_all_known_prefixes_mapped(self):
        """Verify all 37 known prefixes are in the mapping."""
        assert len(USAGE_PREFIX_TO_REGION) >= 30  # At least 30 mappings


# === get_regions_from_ce tests ===


class TestGetRegionsFromCE:
    """get_regions_from_ce: deduplicates and sorts regions from CE results."""

    def test_deduplicates_regions(self):
        ce_results = [
            {'usage_type': 'USE1-Claude-input-tokens'},
            {'usage_type': 'USE1-Claude-output-tokens'},
            {'usage_type': 'USW2-Claude-input-tokens'},
        ]
        regions = get_regions_from_ce(ce_results)
        assert regions == ['us-east-1', 'us-west-2']

    def test_sorts_alphabetically(self):
        ce_results = [
            {'usage_type': 'USW2-Claude-input-tokens'},
            {'usage_type': 'APN1-Claude-input-tokens'},
            {'usage_type': 'USE1-Claude-input-tokens'},
        ]
        regions = get_regions_from_ce(ce_results)
        assert regions == ['ap-northeast-1', 'us-east-1', 'us-west-2']

    def test_skips_unknown_prefixes(self):
        ce_results = [
            {'usage_type': 'USE1-Claude-input-tokens'},
            {'usage_type': 'XXX1-Unknown-input-tokens'},
        ]
        regions = get_regions_from_ce(ce_results)
        assert regions == ['us-east-1']

    def test_empty_results_returns_empty(self):
        assert get_regions_from_ce([]) == []

    def test_multiple_regions_from_real_data(self):
        """Simulates real-world CE data with multiple regions."""
        ce_results = [
            {'usage_type': 'USE1-Claude4.6Opus-input-tokens-cross-region-global'},
            {'usage_type': 'USE1-Claude4.6Opus-output-tokens-cross-region-global'},
            {'usage_type': 'USW2-anthropic.claude-opus-4-8-mantle-input-tokens-global-standard'},
            {'usage_type': 'EUC1-claude-sonnet-4-cache-read-input-token-count-cross-region-global'},
            {'usage_type': 'APN1-claude-haiku-input-tokens'},
        ]
        regions = get_regions_from_ce(ce_results)
        assert 'us-east-1' in regions
        assert 'us-west-2' in regions
        assert 'eu-central-1' in regions
        assert 'ap-northeast-1' in regions
        assert len(regions) == 4


# === reconcile_one integration tests ===


class TestReconcileOneIntegration:
    """Integration tests for reconcile_one with mocked AWS calls."""

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_successful_reconciliation(self, mock_ce, mock_cw, mock_save):
        """Full successful flow: CE data → CW comparison → save records."""
        mock_ce.return_value = [
            {'usage_type': 'USE1-claude-sonnet-4-input-tokens-cross-region-global',
             'cost': 10.5, 'quantity': 1000.0, 'unit': '1K tokens'},
            {'usage_type': 'USE1-claude-sonnet-4-output-tokens-cross-region-global',
             'cost': 31.5, 'quantity': 500.0, 'unit': '1K tokens'},
        ]
        mock_cw.return_value = (1500000, [], {'us-east-1': 1500000})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        assert 'msg' in result
        assert result['total_actual'] == pytest.approx(42.0)
        # Should save model record + _summary + _ce_detail + _cw_detail
        assert mock_save.call_count >= 4

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_ce_failure_returns_error(self, mock_ce, mock_cw, mock_save):
        """CE failure returns ce_error without crashing."""
        mock_ce.side_effect = Exception("Access Denied")
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        assert 'ce_error' in result
        mock_save.assert_not_called()

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_no_ce_data_reports_no_usage(self, mock_ce, mock_cw, mock_save):
        """When CE returns nothing, report shows no usage."""
        mock_ce.return_value = []
        mock_cw.return_value = (0, [], {})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        assert 'msg' in result
        assert result['total_actual'] == 0
        assert '未发现 Bedrock 用量' in result['msg']

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_diff_percentage_calculated_correctly(self, mock_ce, mock_cw, mock_save):
        """diff% = (CE - CW) / CW × 100."""
        mock_ce.return_value = [
            {'usage_type': 'USE1-model-input-tokens',
             'cost': 5.0, 'quantity': 1000.0, 'unit': '1K tokens'},  # 1000 * 1000 = 1,000,000 tokens
        ]
        # CW reports slightly more: 1,050,000
        mock_cw.return_value = (1050000, [], {'us-east-1': 1050000})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        # diff = (1000000 - 1050000) / 1050000 * 100 ≈ -4.76%
        assert result['reconcile_diff_pct'] == pytest.approx(-4.76, rel=0.01)

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_cw_failed_regions_noted_in_report(self, mock_ce, mock_cw, mock_save):
        """Failed CW regions appear in the report."""
        mock_ce.return_value = [
            {'usage_type': 'USE1-model-input-tokens',
             'cost': 5.0, 'quantity': 1000.0, 'unit': '1K tokens'},
        ]
        mock_cw.return_value = (900000, ['us-west-2'], {'us-east-1': 900000})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        assert 'us-west-2' in result['msg']

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_five_token_types_aggregated(self, mock_ce, mock_cw, mock_save):
        """All 5 token types are correctly bucketed."""
        mock_ce.return_value = [
            {'usage_type': 'USE1-model-input-tokens', 'cost': 1.0, 'quantity': 100.0, 'unit': '1K tokens'},
            {'usage_type': 'USE1-model-output-tokens', 'cost': 3.0, 'quantity': 50.0, 'unit': '1K tokens'},
            {'usage_type': 'USE1-model-cache-read-tokens', 'cost': 0.1, 'quantity': 200.0, 'unit': '1K tokens'},
            {'usage_type': 'USE1-model-cache-write-tokens', 'cost': 0.5, 'quantity': 30.0, 'unit': '1K tokens'},
            {'usage_type': 'USE1-model-cache-write-1h-tokens', 'cost': 0.8, 'quantity': 20.0, 'unit': '1K tokens'},
        ]
        mock_cw.return_value = (400000, [], {'us-east-1': 400000})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        # Total cost = 1.0 + 3.0 + 0.1 + 0.5 + 0.8 = 5.4
        assert result['total_actual'] == pytest.approx(5.4)

        # Check the saved model record has all token type fields
        model_save_calls = [c for c in mock_save.call_args_list
                           if c[0][1] != '_summary' and c[0][1] != '_ce_detail' and c[0][1] != '_cw_detail']
        assert len(model_save_calls) == 1
        record_data = model_save_calls[0][0][2]
        assert 'cost_input' in record_data
        assert 'cost_output' in record_data
        assert 'cost_cache_read' in record_data
        assert 'cost_cache_write' in record_data
        assert 'cost_cache_write_1h' in record_data

    @patch('reconciler.handler._get_table')
    @patch('reconciler.handler.query_by_pk')
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_stale_per_model_records_deleted_before_rewrite(self, mock_ce, mock_cw, mock_save, mock_query, mock_table):
        """#2 幂等：重新对账时先删旧的 per-model SK，保留 _ 开头的元记录。"""
        mock_ce.return_value = [
            {'usage_type': 'USE1-model-input-tokens', 'cost': 5.0, 'quantity': 1000.0, 'unit': '1K tokens'},
        ]
        mock_cw.return_value = (1000000, [], {'us-east-1': 1000000})
        # 该日期已存在一条旧模型记录 + 元记录
        mock_query.return_value = [
            {'PK': 'RECONCILE#2024-07-01', 'SK': 'stale-model-cross-region-global'},
            {'PK': 'RECONCILE#2024-07-01', 'SK': '_summary'},
        ]
        table = MagicMock()
        mock_table.return_value = table
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        reconcile_one('2024-07-01', '2024-07-02', now)

        # 只删 per-model 旧记录，_summary 不删
        table.delete_item.assert_called_once_with(
            Key={'PK': 'RECONCILE#2024-07-01', 'SK': 'stale-model-cross-region-global'})

    @patch('reconciler.handler._get_table', new=MagicMock())
    @patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
    @patch('reconciler.handler.save_reconcile_record')
    @patch('reconciler.handler.get_cloudwatch_token_total')
    @patch('reconciler.handler.get_cost_explorer_data')
    def test_diff_not_computed_when_regions_failed(self, mock_ce, mock_cw, mock_save):
        """#4：有 region 查询失败时不计算 diff%（cw_total 不完整会误导）。"""
        mock_ce.return_value = [
            {'usage_type': 'USE1-model-input-tokens', 'cost': 5.0, 'quantity': 1000.0, 'unit': '1K tokens'},
        ]
        mock_cw.return_value = (900000, ['us-west-2'], {'us-east-1': 900000})
        now = datetime(2024, 7, 3, 1, 0, 0, tzinfo=timezone.utc)

        result = reconcile_one('2024-07-01', '2024-07-02', now)

        assert result['reconcile_diff_pct'] is None
        assert '数据缺失' in result['msg']

    @patch('reconciler.handler.boto3')
    def test_ce_pagination_follows_next_token(self, mock_boto3):
        """#1：CE 返回 NextPageToken 时循环取完所有页。"""
        from reconciler.handler import get_cost_explorer_data
        ce = MagicMock()
        mock_boto3.client.return_value = ce
        page1 = {
            'ResultsByTime': [{'Groups': [
                {'Keys': ['USE1-a-input-tokens'],
                 'Metrics': {'UnblendedCost': {'Amount': '1.0'}, 'UsageQuantity': {'Amount': '100', 'Unit': '1K tokens'}}},
            ]}],
            'NextPageToken': 'PAGE2',
        }
        page2 = {
            'ResultsByTime': [{'Groups': [
                {'Keys': ['USE1-b-output-tokens'],
                 'Metrics': {'UnblendedCost': {'Amount': '2.0'}, 'UsageQuantity': {'Amount': '200', 'Unit': '1K tokens'}}},
            ]}],
        }
        ce.get_cost_and_usage.side_effect = [page1, page2]

        results = get_cost_explorer_data('2024-07-01', '2024-07-02')

        # 两页都被取到
        assert len(results) == 2
        assert ce.get_cost_and_usage.call_count == 2
        # 第二次调用带上了 NextPageToken
        second_call_kwargs = ce.get_cost_and_usage.call_args_list[1][1]
        assert second_call_kwargs.get('NextPageToken') == 'PAGE2'



# === _get_month_summary / mobile summary tests ===


class TestMonthSummary:
    """_get_month_summary: 按报告日期所在月汇总，且不被日期检索窗口截断。"""

    @patch('reconciler.handler.get_reconcile_by_date')
    @patch('reconciler.handler.get_reconcile_dates')
    def test_full_month_not_truncated_by_date_window(self, mock_dates, mock_by_date):
        """历史重跑较早月份时，必须汇总目标月全部日期，而非最近窗口的少数几天。"""
        from reconciler.handler import _get_month_summary
        # 保留窗口内既有目标月（1月）全月，也有更晚月份
        all_dates = ['2026-03-31', '2026-03-30'] + [f'2026-01-{d:02d}' for d in range(1, 32)]
        mock_dates.return_value = sorted(all_dates, reverse=True)
        mock_by_date.side_effect = lambda d: {'_summary': {'total_actual': '1'}}

        # 重跑 1-20，本轮以确定性结果覆盖当天为 $9
        summary = _get_month_summary(
            '2026-01-20',
            {'2026-01-20': {'total_actual': 9.0, 'reconcile_diff_pct': 0.0, 'model_costs': {'m': 9.0}}},
        )

        # 1 月 31 天全部计入；30 天 * $1 + 覆盖日 $9 = $39
        assert len(summary['dates']) == 31
        assert summary['total_cost'] == pytest.approx(39.0)
        # 检索窗口必须足够大以覆盖 90 天 TTL
        assert mock_dates.call_args.kwargs.get('limit', 0) >= 90

    @patch('reconciler.handler.get_reconcile_by_date')
    @patch('reconciler.handler.get_reconcile_dates')
    def test_cross_month_dates_excluded(self, mock_dates, mock_by_date):
        """T-2/T-1 跨月时，只统计报告日期所在月。"""
        from reconciler.handler import _get_month_summary
        mock_dates.return_value = ['2026-08-01', '2026-07-31']
        mock_by_date.side_effect = lambda d: {'_summary': {'total_actual': '5'}}

        summary = _get_month_summary(
            '2026-08-01',
            {
                '2026-07-31': {'total_actual': 5.0, 'reconcile_diff_pct': 0.0, 'model_costs': {}},
                '2026-08-01': {'total_actual': 5.0, 'reconcile_diff_pct': 0.0, 'model_costs': {}},
            },
        )

        assert summary['dates'] == ['2026-08-01']
        assert summary['total_cost'] == pytest.approx(5.0)

    @patch('reconciler.handler.get_reconcile_by_date')
    @patch('reconciler.handler.get_reconcile_dates')
    def test_current_round_overrides_stale_ddb_read(self, mock_dates, mock_by_date):
        """DDB 最终一致可能返回旧值；本轮确定性结果必须覆盖同日金额。"""
        from reconciler.handler import _get_month_summary
        mock_dates.return_value = ['2026-08-02']
        # DDB 仍是旧的 $99
        mock_by_date.side_effect = lambda d: {'_summary': {'total_actual': '99'}}

        summary = _get_month_summary(
            '2026-08-02',
            {'2026-08-02': {'total_actual': 15.87, 'reconcile_diff_pct': 0.0, 'model_costs': {'opus': 15.87}}},
        )

        assert summary['total_cost'] == pytest.approx(15.87)


class TestMobileSummary:
    """_build_mobile_summary: 固定行数，月汇总缺失时不冒充金额。"""

    def test_five_lines_with_month_summary(self):
        from reconciler.handler import _build_mobile_summary
        current = {'total_actual': 15.87, 'reconcile_diff_pct': -0.08}
        previous = {'total_actual': 49.23, 'reconcile_diff_pct': 0.1}
        month = {
            'total_cost': 65.10,
            'top_model': ('anthropic.claude-opus-5-mantle-global', 60.75),
        }
        msg = _build_mobile_summary('2026-08-02', current, previous=previous, month_summary=month)
        lines = msg.splitlines()
        assert len(lines) == 5
        assert '↓67.8%' in lines[1]
        assert '$65.10' in lines[2]
        assert 'Opus' in lines[3] and '93.3%' in lines[3]

    def test_month_unavailable_does_not_fabricate_total(self):
        from reconciler.handler import _build_mobile_summary
        current = {'total_actual': 15.87, 'reconcile_diff_pct': -0.08}
        previous = {'total_actual': 49.23, 'reconcile_diff_pct': 0.1}
        msg = _build_mobile_summary('2026-08-02', current, previous=previous, month_summary=None)
        assert '本月累计：暂不可用' in msg
        # 绝不能把两日相加冒充月累计
        assert '$65.10' not in msg
        assert '费用最高：暂无数据' in msg

    def test_ce_error_shows_failure_not_normal(self):
        from reconciler.handler import _build_mobile_summary
        current = {'ce_error': 'AccessDenied'}
        previous = {'total_actual': 49.23, 'reconcile_diff_pct': 0.1}
        msg = _build_mobile_summary('2026-08-02', current, previous=previous, month_summary=None)
        assert '获取失败' in msg
        assert '正常' not in msg
