"""Property-based tests for the Reconciler USAGE_TYPE parsing and cost aggregation.

Uses pytest + hypothesis for property-based testing.

Covers:
  - Requirement 1: Model_Identity + Token_Type extraction (routing-aware)
  - Requirement 2: per-token-type cost/token aggregation + conservation law
  - Requirement 3: DynamoDB record schema (fields present, sort key = identity)
  - Requirement 6: concise webhook report (one subtotal line per model)
  - Requirement 7: backfill / date-override handling
"""
import pytest
from hypothesis import given, settings
from hypothesis.strategies import (
    floats, sampled_from, lists, composite, booleans,
)
from unittest.mock import patch, MagicMock

from reconciler.handler import (
    extract_model_identity, get_token_type, TOKEN_TYPE_FIELDS,
)


# --- Strategies ---

# Model base names, including a mantle endpoint (routing embedded in the name).
MODEL_BASES = ['claude4.6opus', 'claude-sonnet-4', 'claude-haiku',
               'anthropic.claude-opus-4-8-mantle']
REGIONS = ['USE1', 'USW2', 'EUW1', 'APS1']
# Routing markers that ARE a price dimension (or none).
ROUTINGS = ['', '-cross-region-global', '-global']
# (Token_Type_Segment, expected Token_Type)
TOKEN_SEGMENTS = [
    ('-input-tokens', 'input'),
    ('-output-tokens', 'output'),
    ('-input-token-count', 'input'),
    ('-output-token-count', 'output'),
    ('-cache-read-input-token-count', 'cache_read'),
    ('-cache-write-input-token-count', 'cache_write'),
    ('-cache-read-tokens', 'cache_read'),
    ('-cache-write-tokens', 'cache_write'),
    ('-cacheread-tokens', 'cache_read'),
    ('-cachewrite-tokens', 'cache_write'),
]

positive_cost = floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
positive_qty = floats(min_value=0.001, max_value=1e7, allow_nan=False, allow_infinity=False)


@composite
def usage_type_st(draw):
    """Generate a realistic token-bearing USAGE_TYPE and its expected token_type."""
    region = draw(sampled_from(REGIONS))
    base = draw(sampled_from(MODEL_BASES))
    seg, ttype = draw(sampled_from(TOKEN_SEGMENTS))
    routing = draw(sampled_from(ROUTINGS))
    tier = '-standard' if draw(booleans()) else ''
    usage_type = f"{region}-{base}{seg}{routing}{tier}"
    return usage_type, ttype


@composite
def ce_results_st(draw):
    """Generate a list of CE line items with costs and quantities."""
    n = draw(lists(usage_type_st(), min_size=1, max_size=12))
    results = []
    for usage_type, _ in n:
        results.append({
            'usage_type': usage_type,
            'cost': draw(positive_cost),
            'quantity': draw(positive_qty),
            'unit': '1K tokens',
        })
    return results


# --- Property 1: Model_Identity extraction (Requirement 1) ---


class TestModelIdentityExtraction:
    """Requirement 1: routing-aware Model_Identity extraction."""

    @given(data=usage_type_st())
    @settings(max_examples=300)
    def test_region_prefix_removed(self, data):
        """**Validates: Requirements 1.1**

        The region prefix (first '-'-delimited segment) is not present in the identity.
        """
        usage_type, _ = data
        region_prefix = usage_type.split('-')[0].lower()
        identity = extract_model_identity(usage_type)
        # The identity must not start with the region prefix segment.
        assert not identity.startswith(region_prefix + '-')
        assert identity != region_prefix

    @given(data=usage_type_st())
    @settings(max_examples=300)
    def test_no_token_segment_in_identity(self, data):
        """**Validates: Requirements 1.2**

        No Token_Type_Segment token words remain in the identity.
        """
        usage_type, _ = data
        identity = extract_model_identity(usage_type)
        assert 'tokens' not in identity
        assert 'token-count' not in identity

    @given(data=usage_type_st())
    @settings(max_examples=300)
    def test_standard_tier_removed(self, data):
        """**Validates: Requirements 1.3**

        The -standard tier suffix never appears in the identity.
        """
        usage_type, _ = data
        identity = extract_model_identity(usage_type)
        assert 'standard' not in identity

    @given(region=sampled_from(REGIONS), base=sampled_from(MODEL_BASES),
           seg=sampled_from([s for s, _ in TOKEN_SEGMENTS]))
    @settings(max_examples=200)
    def test_routing_marker_retained_and_distinct(self, region, base, seg):
        """**Validates: Requirements 1.4, 1.5**

        A cross-region-global routing marker is retained, and two usage types that
        differ only by their routing marker derive different identities.
        """
        direct = f"{region}-{base}{seg}"
        routed = f"{region}-{base}{seg}-cross-region-global"
        id_direct = extract_model_identity(direct)
        id_routed = extract_model_identity(routed)
        assert 'cross-region-global' in id_routed
        assert id_direct != id_routed

    @given(region=sampled_from(REGIONS), base=sampled_from(MODEL_BASES),
           seg=sampled_from([s for s, _ in TOKEN_SEGMENTS]),
           routing=sampled_from(ROUTINGS))
    @settings(max_examples=200)
    def test_standard_suffix_does_not_change_identity(self, region, base, seg, routing):
        """**Validates: Requirements 1.6**

        Two usage types identical except for a trailing -standard derive the same identity.
        """
        without = f"{region}-{base}{seg}{routing}"
        with_std = f"{region}-{base}{seg}{routing}-standard"
        assert extract_model_identity(without) == extract_model_identity(with_std)

    @given(data=usage_type_st())
    @settings(max_examples=200)
    def test_deterministic(self, data):
        """**Validates: Requirements 1.7, 1.12**

        The same usage type always yields the same single (identity, token_type) pair.
        """
        usage_type, _ = data
        pair1 = (extract_model_identity(usage_type), get_token_type(usage_type))
        pair2 = (extract_model_identity(usage_type), get_token_type(usage_type))
        assert pair1 == pair2
        assert isinstance(pair1[0], str) and isinstance(pair1[1], str)

    @given(data=usage_type_st())
    @settings(max_examples=300)
    def test_token_type_classification(self, data):
        """**Validates: Requirements 1.8, 1.9, 1.10, 1.11**

        The token type matches the metering category of the usage type.
        """
        usage_type, expected = data
        assert get_token_type(usage_type) == expected

    def test_glossary_examples(self):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

        The three canonical glossary examples parse to the documented identities.
        """
        assert extract_model_identity('USE1-Claude4.6Opus-input-tokens') == 'claude4.6opus'
        assert extract_model_identity(
            'USE1-Claude4.6Opus-input-tokens-cross-region-global'
        ) == 'claude4.6opus-cross-region-global'
        assert extract_model_identity(
            'USE1-Claude4.6Opus-cache-read-input-token-count-cross-region-global'
        ) == 'claude4.6opus-cross-region-global'
        assert extract_model_identity(
            'USW2-anthropic.claude-opus-4-8-mantle-input-tokens-global-standard'
        ) == 'anthropic.claude-opus-4-8-mantle-global'


# --- Reconciler aggregation simulation (mirrors handler.reconcile_one) ---


def simulate_aggregation(ce_results):
    """Reproduce the per-model, per-token-type aggregation from handler.reconcile_one."""
    model_details = {}
    for item in ce_results:
        model = extract_model_identity(item['usage_type'])
        token_type = get_token_type(item['usage_type'])
        if model not in model_details:
            model_details[model] = {
                'cost_input': 0, 'cost_output': 0,
                'cost_cache_read': 0, 'cost_cache_write': 0,
                'tokens_input_1k': 0, 'tokens_output_1k': 0,
                'tokens_cache_read_1k': 0, 'tokens_cache_write_1k': 0,
            }
        cost_field, tokens_field = TOKEN_TYPE_FIELDS[token_type]
        model_details[model][cost_field] += item['cost']
        model_details[model][tokens_field] += item['quantity']

    for detail in model_details.values():
        detail['actual_cost'] = (
            detail['cost_input'] + detail['cost_output']
            + detail['cost_cache_read'] + detail['cost_cache_write']
        )
    return model_details


# --- Property 2: Per-token-type aggregation + conservation (Requirement 2) ---


class TestAggregation:
    """Requirement 2: per-token-type cost/token aggregation and conservation."""

    @given(ce_results=ce_results_st())
    @settings(max_examples=200)
    def test_actual_cost_is_sum_of_buckets(self, ce_results):
        """**Validates: Requirements 2.1, 2.3**

        Each model's actual_cost equals the sum of its four per-token-type costs.
        """
        model_details = simulate_aggregation(ce_results)
        for detail in model_details.values():
            expected = (detail['cost_input'] + detail['cost_output']
                        + detail['cost_cache_read'] + detail['cost_cache_write'])
            assert detail['actual_cost'] == pytest.approx(expected, rel=1e-9)

    @given(ce_results=ce_results_st())
    @settings(max_examples=200)
    def test_conservation_no_double_count_no_drop(self, ce_results):
        """**Validates: Requirements 2.4**

        The sum of actual_cost across all models equals the sum of every CE line
        item's cost — no line item is counted twice or dropped.
        """
        model_details = simulate_aggregation(ce_results)
        total_from_models = sum(d['actual_cost'] for d in model_details.values())
        total_from_line_items = sum(item['cost'] for item in ce_results)
        assert total_from_models == pytest.approx(total_from_line_items, rel=1e-9)

    @given(ce_results=ce_results_st())
    @settings(max_examples=200)
    def test_token_quantity_conservation(self, ce_results):
        """**Validates: Requirements 2.2**

        The sum of all per-token-type token counts equals the sum of every line
        item's usage quantity.
        """
        model_details = simulate_aggregation(ce_results)
        total_tokens = sum(
            d['tokens_input_1k'] + d['tokens_output_1k']
            + d['tokens_cache_read_1k'] + d['tokens_cache_write_1k']
            for d in model_details.values()
        )
        expected = sum(item['quantity'] for item in ce_results)
        assert total_tokens == pytest.approx(expected, rel=1e-9)


# --- Property 3: DDB record schema (Requirement 3) ---


def build_records(ce_results):
    """Reproduce the DDB per-model record construction from handler.reconcile_one."""
    model_details = simulate_aggregation(ce_results)
    records = {}
    for model, detail in model_details.items():
        if detail['actual_cost'] < 0.01:
            continue
        records[model] = {
            'actual_cost': str(round(detail['actual_cost'], 4)),
            'cost_input': str(round(detail['cost_input'], 4)),
            'cost_output': str(round(detail['cost_output'], 4)),
            'cost_cache_read': str(round(detail['cost_cache_read'], 4)),
            'cost_cache_write': str(round(detail['cost_cache_write'], 4)),
            'tokens_input_1k': str(round(detail['tokens_input_1k'], 3)),
            'tokens_output_1k': str(round(detail['tokens_output_1k'], 3)),
            'tokens_cache_read_1k': str(round(detail['tokens_cache_read_1k'], 3)),
            'tokens_cache_write_1k': str(round(detail['tokens_cache_write_1k'], 3)),
        }
    return records


REQUIRED_FIELDS = {
    'cost_input', 'cost_output', 'cost_cache_read', 'cost_cache_write',
    'tokens_input_1k', 'tokens_output_1k', 'tokens_cache_read_1k',
    'tokens_cache_write_1k', 'actual_cost',
}
# Fields from the removed price-table feature that must never appear.
FORBIDDEN_FIELDS = {
    'estimated_cost', 'estimated_input_cost', 'estimated_output_cost',
    'diff_pct', 'total_estimated',
}


class TestRecordSchema:
    """Requirement 3: DynamoDB per-model record schema."""

    @given(ce_results=ce_results_st())
    @settings(max_examples=200)
    def test_records_have_required_fields_and_no_forbidden(self, ce_results):
        """**Validates: Requirements 3.2, 4.2**

        Every per-model record carries the nine schema fields and none of the
        removed price-table fields.
        """
        records = build_records(ce_results)
        for model, rec in records.items():
            assert REQUIRED_FIELDS.issubset(rec.keys()), f"{model} missing fields"
            assert not (FORBIDDEN_FIELDS & rec.keys()), f"{model} has forbidden fields"

    @given(ce_results=ce_results_st())
    @settings(max_examples=100)
    def test_sort_key_is_model_identity(self, ce_results):
        """**Validates: Requirements 3.1**

        Each record is keyed by a Model_Identity produced by extract_model_identity.
        """
        records = build_records(ce_results)
        valid_identities = {extract_model_identity(i['usage_type']) for i in ce_results}
        for model in records:
            assert model in valid_identities


# --- Property 4: Webhook report content (Requirement 6) ---


def build_webhook_breakdown(model_details):
    """Reproduce the per-model breakdown section of the webhook report."""
    msg = "--- 各模型明细 ---\n"
    shown = False
    for model in sorted(model_details.keys(),
                        key=lambda m: model_details[m]['actual_cost'], reverse=True):
        detail = model_details[model]
        if detail['actual_cost'] < 0.01:
            continue
        msg += f"  {model}: ${detail['actual_cost']:.2f}\n"
        shown = True
    if not shown:
        msg += "  未发现 Bedrock 用量\n"
    return msg


class TestWebhookReport:
    """Requirement 6: concise webhook report."""

    @given(ce_results=ce_results_st())
    @settings(max_examples=100)
    def test_one_subtotal_line_per_model_no_estimate(self, ce_results):
        """**Validates: Requirements 6.1, 6.2, 6.3**

        Each model appears as a single subtotal line; no per-token detail rows and
        no estimate strings are present.
        """
        model_details = simulate_aggregation(ce_results)
        msg = build_webhook_breakdown(model_details)
        for model, detail in model_details.items():
            if detail['actual_cost'] < 0.01:
                continue
            lines = [ln for ln in msg.split('\n') if ln.strip().startswith(model + ':')]
            assert len(lines) == 1, f"{model} should have exactly one subtotal line"
        # No estimate/detail vocabulary leaks into the report.
        assert '估算' not in msg
        assert '输入' not in msg and '输出' not in msg


# --- Unit Tests: Reconciler Date Override (Requirement 7) ---

from datetime import datetime, timezone, timedelta
from reconciler.handler import handler


@patch('reconciler.handler._get_table', new=MagicMock())
@patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
@patch('reconciler.handler.send_webhook_all')
@patch('reconciler.handler.save_reconcile_record')
@patch('reconciler.handler.get_cloudwatch_token_total', return_value=(0, [], {}))
@patch('reconciler.handler.get_cost_explorer_data', return_value=[])
@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_valid_historical_date_sets_correct_dates(
    mock_webhook_config, mock_ce, mock_cw, mock_save, mock_send
):
    """**Validates: Requirements 7.1**

    A valid historical date regenerates that date's records.
    """
    result = handler({'date': '2024-06-15'}, None)
    assert result['statusCode'] == 200
    assert result['date'] == '2024-06-15'


@patch('reconciler.handler._get_table', new=MagicMock())
@patch('reconciler.handler.query_by_pk', new=MagicMock(return_value=[]))
@patch('reconciler.handler.get_account_id', return_value='123456789012')
@patch('reconciler.handler.get_notify_policy', return_value='always')
@patch('reconciler.handler.send_webhook_all')
@patch('reconciler.handler.save_reconcile_record')
@patch('reconciler.handler.get_cloudwatch_token_total', return_value=(0, [], {}))
@patch('reconciler.handler.get_cost_explorer_data', return_value=[])
@patch('reconciler.handler.get_webhook_config', return_value=[])
@patch('reconciler.handler.get_ai_summary_config', return_value={'enabled': False, 'model_id': 'us.amazon.nova-2-lite-v1:0'})
def test_missing_date_falls_back_to_t_minus_2(
    mock_ai_summary, mock_webhook_config, mock_ce, mock_cw, mock_save, mock_send, mock_policy, mock_account_id
):
    """**Validates: Requirements 7.1**

    With no 'date' field, handler runs the default T-2 / T-1 jobs.
    """
    result = handler({}, None)
    assert result['statusCode'] == 200
    now = datetime.now(timezone.utc)
    expected_date = (now - timedelta(days=2)).strftime('%Y-%m-%d')
    assert result['dates'][0] == expected_date


@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_invalid_format_month_13_returns_400(mock_webhook_config):
    """**Validates: Requirements 7.2**

    An invalid date format returns statusCode 400 and writes nothing.
    """
    result = handler({'date': '2024-13-01'}, None)
    assert result['statusCode'] == 400
    assert 'Invalid date format' in result['error']


@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_invalid_format_not_a_date_returns_400(mock_webhook_config):
    """**Validates: Requirements 7.2**

    A non-date string returns statusCode 400.
    """
    result = handler({'date': 'not-a-date'}, None)
    assert result['statusCode'] == 400
    assert 'Invalid date format' in result['error']


@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_future_date_returns_400(mock_webhook_config):
    """**Validates: Requirements 7.2**

    A future date returns statusCode 400 and writes no records.
    """
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d')
    result = handler({'date': tomorrow}, None)
    assert result['statusCode'] == 400
    assert 'Date must be before today' in result['error']


@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_today_date_returns_400(mock_webhook_config):
    """**Validates: Requirements 7.2**

    Today's date returns statusCode 400 (CE data needs T+1).
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    result = handler({'date': today}, None)
    assert result['statusCode'] == 400
    assert 'Date must be before today' in result['error']


@patch('reconciler.handler.get_webhook_config', return_value=[])
def test_nonexistent_calendar_date_returns_400(mock_webhook_config):
    """**Validates: Requirements 7.2**

    A non-existent calendar date returns statusCode 400.
    """
    result = handler({'date': '2024-02-30'}, None)
    assert result['statusCode'] == 400
    assert 'Invalid date format' in result['error']
