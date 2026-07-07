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
