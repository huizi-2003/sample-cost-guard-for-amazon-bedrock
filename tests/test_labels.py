"""Unit tests for common/labels.py — the single source of truth for
CloudWatch metric label parsing shared by monitor and web."""
from common.labels import extract_model_name, extract_token_type


class TestExtractModelName:
    def test_strips_bedrock_namespace(self):
        assert 'AWS/Bedrock' not in extract_model_name('AWS/Bedrock claude-sonnet-4 InputTokenCount')

    def test_strips_mantle_namespace(self):
        assert 'AWS/BedrockMantle' not in extract_model_name('AWS/BedrockMantle claude-opus-4 TotalInputTokens')

    def test_strips_input_suffix(self):
        assert extract_model_name('AWS/Bedrock claude-sonnet-4 InputTokenCount') == 'claude-sonnet-4'

    def test_strips_output_suffix(self):
        assert extract_model_name('AWS/Bedrock claude-sonnet-4 OutputTokenCount') == 'claude-sonnet-4'

    def test_strips_cache_read_suffix(self):
        assert extract_model_name('AWS/Bedrock claude-sonnet-4 CacheReadInputTokenCount') == 'claude-sonnet-4'

    def test_strips_cache_write_suffix(self):
        assert extract_model_name('AWS/Bedrock claude-sonnet-4 CacheWriteInputTokenCount') == 'claude-sonnet-4'

    def test_strips_global_anthropic_prefix(self):
        assert extract_model_name('AWS/Bedrock global.anthropic.claude-sonnet-4 InputTokenCount') == 'claude-sonnet-4'

    def test_strips_anthropic_prefix(self):
        assert extract_model_name('AWS/Bedrock anthropic.claude-sonnet-4 InputTokenCount') == 'claude-sonnet-4'

    def test_unknown_suffix_left_intact(self):
        assert extract_model_name('AWS/Bedrock some-model UnknownMetric') == 'some-model UnknownMetric'


class TestExtractTokenType:
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

    def test_default_is_input(self):
        assert extract_token_type('AWS/Bedrock some-model Tokens') == 'input'
