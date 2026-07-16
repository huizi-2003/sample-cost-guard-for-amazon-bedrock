"""Unit tests for common/iam_scanner.py.

Covers the pure policy-parsing logic (_is_dangerous_action, _extract_bedrock_actions,
_pattern_to_regex, _match_dangerous) and the managed-policy read path
(_check_managed_policy) with a mocked IAM client.
Full scan_iam_identities / execute_iam_scan involve heavy boto pagination + DDB;
here we focus on the decision logic that determines what counts as a billable action.
"""
from unittest.mock import MagicMock

from common.iam_scanner import (
    _is_dangerous_action,
    _extract_bedrock_actions,
    _check_managed_policy,
    _check_group_policies,
    _pattern_to_regex,
    _match_dangerous,
    DANGEROUS_ACTIONS,
)


class TestIsDangerousAction:
    def test_wildcard_all(self):
        assert _is_dangerous_action('*') is True

    def test_bedrock_wildcard(self):
        assert _is_dangerous_action('bedrock:*') is True

    def test_invoke_is_dangerous(self):
        assert _is_dangerous_action('bedrock:InvokeModel') is True

    def test_converse_is_dangerous(self):
        assert _is_dangerous_action('bedrock:Converse') is True

    def test_converse_stream_is_dangerous(self):
        assert _is_dangerous_action('bedrock:ConverseStream') is True

    def test_case_insensitive(self):
        assert _is_dangerous_action('BEDROCK:INVOKEMODEL') is True

    def test_readonly_list_not_dangerous(self):
        assert _is_dangerous_action('bedrock:ListFoundationModels') is False

    def test_readonly_get_not_dangerous(self):
        assert _is_dangerous_action('bedrock:GetModelInvocationLoggingConfiguration') is False

    def test_unrelated_service_not_dangerous(self):
        assert _is_dangerous_action('s3:GetObject') is False

    # --- Wildcard pattern tests (the core fix) ---
    def test_bedrock_in_star(self):
        """bedrock:In* matches InvokeModel, InvokeModelWithResponseStream, InvokeAgent, etc."""
        assert _is_dangerous_action('bedrock:In*') is True

    def test_bedrock_conv_star(self):
        """bedrock:Conv* matches Converse and ConverseStream."""
        assert _is_dangerous_action('bedrock:Conv*') is True

    def test_star_colon_star(self):
        """*:* grants all actions on all services."""
        assert _is_dangerous_action('*:*') is True

    def test_bedrock_invoke_star(self):
        """bedrock:Invoke* matches all Invoke variants."""
        assert _is_dangerous_action('bedrock:Invoke*') is True

    def test_question_mark_wildcard(self):
        """? matches single character — bedrock:Convers?Stream matches ConverseStream."""
        assert _is_dangerous_action('bedrock:Convers?Stream') is True

    def test_question_mark_no_match(self):
        """? must match exactly one char — bedrock:Convers? doesn't match Converse (e needs one more)."""
        # bedrock:Convers? → pattern is 'bedrock:convers.' → matches 'bedrock:converse' (7 chars after colon)
        assert _is_dangerous_action('bedrock:Convers?') is True  # matches 'converse'

    def test_bedrock_get_star_not_dangerous(self):
        """bedrock:Get* should not match any dangerous action."""
        assert _is_dangerous_action('bedrock:Get*') is False

    def test_bedrock_list_star_not_dangerous(self):
        """bedrock:List* should not match any dangerous action."""
        assert _is_dangerous_action('bedrock:List*') is False

    def test_bedrock_describe_star_not_dangerous(self):
        """bedrock:Describe* should not match any dangerous action."""
        assert _is_dangerous_action('bedrock:Describe*') is False


class TestExtractBedrockActions:
    def test_extracts_only_billable_allow_actions(self):
        doc = {'Statement': [
            {'Effect': 'Allow', 'Action': ['bedrock:InvokeModel', 'bedrock:ListFoundationModels']},
        ]}
        assert _extract_bedrock_actions(doc) == {'bedrock:InvokeModel'}

    def test_string_action_normalized_to_list(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': 'bedrock:Converse'}]}
        assert _extract_bedrock_actions(doc) == {'bedrock:Converse'}

    def test_deny_statements_ignored(self):
        doc = {'Statement': [{'Effect': 'Deny', 'Action': 'bedrock:InvokeModel'}]}
        assert _extract_bedrock_actions(doc) == set()

    def test_non_bedrock_actions_skipped(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': ['s3:GetObject', 'ec2:RunInstances']}]}
        assert _extract_bedrock_actions(doc) == set()

    def test_wildcard_captured(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': '*'}]}
        assert _extract_bedrock_actions(doc) == {'*'}

    def test_non_dict_returns_empty(self):
        assert _extract_bedrock_actions(None) == set()
        assert _extract_bedrock_actions('not-a-dict') == set()

    def test_empty_statement(self):
        assert _extract_bedrock_actions({'Statement': []}) == set()

    # --- Wildcard patterns in Action ---
    def test_bedrock_in_star_captured(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': 'bedrock:In*'}]}
        result = _extract_bedrock_actions(doc)
        assert 'bedrock:In*' in result

    def test_bedrock_conv_star_captured(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': 'bedrock:Conv*'}]}
        result = _extract_bedrock_actions(doc)
        assert 'bedrock:Conv*' in result

    def test_star_colon_star_captured(self):
        doc = {'Statement': [{'Effect': 'Allow', 'Action': '*:*'}]}
        result = _extract_bedrock_actions(doc)
        assert '*:*' in result

    # --- Statement as dict (not array) ---
    def test_statement_as_single_dict(self):
        """IAM allows Statement to be a single dict instead of an array."""
        doc = {'Statement': {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'}}
        assert _extract_bedrock_actions(doc) == {'bedrock:InvokeModel'}

    def test_statement_as_single_dict_deny(self):
        doc = {'Statement': {'Effect': 'Deny', 'Action': 'bedrock:InvokeModel'}}
        assert _extract_bedrock_actions(doc) == set()

    # --- NotAction handling ---
    def test_notaction_s3_star_reports_all_dangerous(self):
        """Allow + NotAction: ['s3:*'] grants everything except s3 → all bedrock dangerous actions."""
        doc = {'Statement': [{'Effect': 'Allow', 'NotAction': ['s3:*'], 'Resource': '*'}]}
        result = _extract_bedrock_actions(doc)
        # All dangerous actions should be reported (with suffix)
        for action in DANGEROUS_ACTIONS:
            assert f'{action} (via NotAction)' in result

    def test_notaction_bedrock_star_reports_nothing(self):
        """Allow + NotAction: ['bedrock:*'] excludes all bedrock → nothing to report."""
        doc = {'Statement': [{'Effect': 'Allow', 'NotAction': ['bedrock:*'], 'Resource': '*'}]}
        result = _extract_bedrock_actions(doc)
        assert result == set()

    def test_notaction_excludes_single_action(self):
        """NotAction: ['bedrock:InvokeModel'] → all dangerous EXCEPT InvokeModel."""
        doc = {'Statement': [{'Effect': 'Allow', 'NotAction': 'bedrock:InvokeModel', 'Resource': '*'}]}
        result = _extract_bedrock_actions(doc)
        assert 'bedrock:invokemodel (via NotAction)' not in result
        # But other dangerous actions should be there
        assert 'bedrock:converse (via NotAction)' in result
        assert 'bedrock:conversestream (via NotAction)' in result

    def test_notaction_string_form(self):
        """NotAction as a single string (not array)."""
        doc = {'Statement': [{'Effect': 'Allow', 'NotAction': 's3:GetObject', 'Resource': '*'}]}
        result = _extract_bedrock_actions(doc)
        assert len(result) == len(DANGEROUS_ACTIONS)

    def test_notaction_deny_ignored(self):
        """Deny + NotAction should be skipped (only Allow matters for granting)."""
        doc = {'Statement': [{'Effect': 'Deny', 'NotAction': ['s3:*'], 'Resource': '*'}]}
        assert _extract_bedrock_actions(doc) == set()

    # --- Non-dict statements are skipped gracefully ---
    def test_non_dict_statement_in_list_skipped(self):
        """If a statement list contains a non-dict entry, skip it without crashing."""
        doc = {'Statement': [
            'invalid-entry',
            {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'},
        ]}
        assert _extract_bedrock_actions(doc) == {'bedrock:InvokeModel'}


class TestPatternToRegex:
    def test_exact_match(self):
        rx = _pattern_to_regex('bedrock:InvokeModel')
        assert rx.match('bedrock:invokemodel')
        assert not rx.match('bedrock:invokemodel2')

    def test_star_wildcard(self):
        rx = _pattern_to_regex('bedrock:In*')
        assert rx.match('bedrock:invokemodel')
        assert rx.match('bedrock:invokemodelwithresponsestream')
        assert rx.match('bedrock:invokeinlineagent')
        assert not rx.match('bedrock:converse')

    def test_question_mark_wildcard(self):
        rx = _pattern_to_regex('bedrock:Convers?')
        assert rx.match('bedrock:converse')
        assert not rx.match('bedrock:conversestream')

    def test_double_star(self):
        rx = _pattern_to_regex('*:*')
        assert rx.match('bedrock:invokemodel')
        assert rx.match('s3:getobject')

    def test_case_insensitive(self):
        rx = _pattern_to_regex('BEDROCK:INVOKE*')
        assert rx.match('bedrock:invokemodel')


class TestMatchDangerous:
    def test_exact_action(self):
        result = _match_dangerous('bedrock:InvokeModel')
        assert result == {'bedrock:invokemodel'}

    def test_bedrock_star(self):
        result = _match_dangerous('bedrock:*')
        assert result == DANGEROUS_ACTIONS

    def test_full_wildcard(self):
        result = _match_dangerous('*')
        assert result == DANGEROUS_ACTIONS

    def test_invoke_star(self):
        result = _match_dangerous('bedrock:Invoke*')
        expected = {a for a in DANGEROUS_ACTIONS if 'invoke' in a}
        assert result == expected
        assert 'bedrock:invokemodel' in result
        assert 'bedrock:invokeagent' in result

    def test_conv_star(self):
        result = _match_dangerous('bedrock:Conv*')
        assert result == {'bedrock:converse', 'bedrock:conversestream'}

    def test_readonly_no_match(self):
        assert _match_dangerous('bedrock:Get*') == set()
        assert _match_dangerous('bedrock:List*') == set()
        assert _match_dangerous('s3:*') == set()


class TestCheckManagedPolicy:
    def test_reads_default_version_and_extracts(self):
        iam = MagicMock()
        iam.get_policy.return_value = {'Policy': {'DefaultVersionId': 'v2'}}
        iam.get_policy_version.return_value = {
            'PolicyVersion': {'Document': {'Statement': [
                {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'},
            ]}}
        }
        cache, unreadable = {}, set()
        assert _check_managed_policy(iam, 'arn:aws:iam::aws:policy/Foo', cache, unreadable) == {'bedrock:InvokeModel'}
        iam.get_policy_version.assert_called_once_with(
            PolicyArn='arn:aws:iam::aws:policy/Foo', VersionId='v2')
        # 成功结果进缓存，未记盲区
        assert cache == {'arn:aws:iam::aws:policy/Foo': {'bedrock:InvokeModel'}}
        assert unreadable == set()

    def test_cache_hit_skips_api(self):
        iam = MagicMock()
        cache = {'arn:aws:iam::aws:policy/Cached': {'bedrock:Converse'}}
        unreadable = set()
        assert _check_managed_policy(iam, 'arn:aws:iam::aws:policy/Cached', cache, unreadable) == {'bedrock:Converse'}
        # 命中缓存不再打 API
        iam.get_policy.assert_not_called()
        iam.get_policy_version.assert_not_called()

    def test_unreadable_policy_returns_empty_and_records_blindspot(self):
        iam = MagicMock()
        iam.get_policy.side_effect = Exception('AccessDenied')
        cache, unreadable = {}, set()
        # 读不到时应吞掉异常、当作无权限（现在会 logger.warning）
        assert _check_managed_policy(iam, 'arn:aws:iam::aws:policy/Bar', cache, unreadable) == set()
        # 失败不缓存（后续遇到会重试），但记入盲区
        assert cache == {}
        assert unreadable == {'arn:aws:iam::aws:policy/Bar'}

    def test_success_after_failure_clears_blindspot(self):
        iam = MagicMock()
        arn = 'arn:aws:iam::aws:policy/Flaky'
        cache = {}
        unreadable = {arn}   # 上一次失败留下的盲区
        iam.get_policy.return_value = {'Policy': {'DefaultVersionId': 'v1'}}
        iam.get_policy_version.return_value = {
            'PolicyVersion': {'Document': {'Statement': [
                {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'},
            ]}}
        }
        assert _check_managed_policy(iam, arn, cache, unreadable) == {'bedrock:InvokeModel'}
        assert unreadable == set()               # 成功后消账
        assert cache == {arn: {'bedrock:InvokeModel'}}


class TestCheckGroupPolicies:
    @staticmethod
    def _iam_with_bedrock_group():
        iam = MagicMock()
        iam.list_attached_group_policies.return_value = {
            'AttachedPolicies': [{'PolicyName': 'BedrockFull', 'PolicyArn': 'arn:aws:iam::aws:policy/BedrockFull'}]
        }
        iam.get_policy.return_value = {'Policy': {'DefaultVersionId': 'v1'}}
        iam.get_policy_version.return_value = {
            'PolicyVersion': {'Document': {'Statement': [
                {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'},
            ]}}
        }
        iam.list_group_policies.return_value = {'PolicyNames': []}
        return iam

    def test_cache_populated_and_actions_correct(self):
        iam = self._iam_with_bedrock_group()
        policy_cache, group_cache, unreadable = {}, {}, set()
        actions, sources = _check_group_policies(iam, 'Devs', policy_cache, group_cache, unreadable)
        assert actions == {'bedrock:InvokeModel'}
        assert 'Devs' in group_cache
        assert sources[0]['name'] == 'BedrockFull'

    def test_returns_fresh_copies_isolated_from_cache(self):
        """缓存命中必须返回新副本：调用方就地写 via_group 不能污染缓存/串味其他身份。"""
        iam = self._iam_with_bedrock_group()
        policy_cache, group_cache, unreadable = {}, {}, set()
        _, s1 = _check_group_policies(iam, 'Devs', policy_cache, group_cache, unreadable)
        for gp in s1:              # 模拟 scan_iam_identities 里的就地写入
            gp['via_group'] = 'Devs'
        # 第二次命中缓存，应拿到不带上一轮 via_group 的干净副本
        _, s2 = _check_group_policies(iam, 'Devs', policy_cache, group_cache, unreadable)
        assert all('via_group' not in gp for gp in s2)
        # 且底层托管策略只被读一次（第二次命中组缓存）
        iam.list_attached_group_policies.assert_called_once()
