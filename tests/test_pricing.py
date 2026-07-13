"""Unit tests for common/pricing.py (shared price table + cost estimation)."""
from common.pricing import match_pricing, estimate_cost, PRICING


class TestMatchPricing:
    def test_matches_series_by_substring(self):
        assert match_pricing('global.anthropic.claude-opus-4-8') == PRICING['opus']
        assert match_pricing('claude-sonnet-4-5-20250929-v1:0') == PRICING['sonnet']
        assert match_pricing('us.anthropic.claude-haiku-4-5') == PRICING['haiku']
        assert match_pricing('claude-fable-5') == PRICING['fable']

    def test_matches_openai_mantle_models(self):
        # bedrock-mantle 端点的 OpenAI 模型（label 形如 'openai.gpt-5.5'）也须命中价目表
        assert match_pricing('openai.gpt-5.5') == PRICING['gpt-5.5']
        assert match_pricing('openai.gpt-5.4') == PRICING['gpt-5.4']

    def test_unknown_returns_none(self):
        # 未收录的 mantle 模型仍返回 None（费用按 0，计入 unpriced 提示）
        assert match_pricing('openai.gpt-6') is None


class TestEstimateCost:
    def test_input_output_priced(self):
        # sonnet: input $3/M, output $15/M
        models = {'claude-sonnet-4': {'input': 1_000_000, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0}}
        cost, unpriced = estimate_cost(models)
        assert abs(cost - 18.0) < 1e-9
        assert unpriced == set()

    def test_cache_read_is_cheap(self):
        # 10M cache_read on sonnet ($0.3/M) = $3  (proves cache-heavy volume ≠ big cost)
        models = {'sonnet': {'input': 0, 'output': 0, 'cache_read': 10_000_000, 'cache_write': 0}}
        cost, _ = estimate_cost(models)
        assert abs(cost - 3.0) < 1e-9

    def test_unpriced_model_tracked(self):
        models = {'openai.gpt-5': {'input': 5_000_000, 'output': 0, 'cache_read': 0, 'cache_write': 0}}
        cost, unpriced = estimate_cost(models)
        assert cost == 0
        assert unpriced == {'openai.gpt-5'}

    def test_unpriced_with_zero_tokens_ignored(self):
        models = {'openai.gpt-5': {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}}
        cost, unpriced = estimate_cost(models)
        assert cost == 0
        assert unpriced == set()

    def test_multi_model_sum(self):
        models = {
            'claude-opus-4-8': {'input': 1_000_000, 'output': 0, 'cache_read': 0, 'cache_write': 0},   # $5
            'claude-haiku-4-5': {'input': 0, 'output': 1_000_000, 'cache_read': 0, 'cache_write': 0},  # $5
        }
        cost, unpriced = estimate_cost(models)
        assert abs(cost - 10.0) < 1e-9
        assert unpriced == set()

    def test_empty(self):
        assert estimate_cost({}) == (0.0, set())
        assert estimate_cost(None) == (0.0, set())
