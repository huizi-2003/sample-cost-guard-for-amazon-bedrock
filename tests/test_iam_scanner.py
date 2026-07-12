"""Unit tests for common/iam_scanner.py.

Covers the pure policy-parsing logic (_is_dangerous_action, _extract_bedrock_actions)
and the managed-policy read path (_check_managed_policy) with a mocked IAM client.
Full scan_iam_identities / execute_iam_scan involve heavy boto pagination + DDB;
here we focus on the decision logic that determines what counts as a billable action.
"""
from unittest.mock import MagicMock

from common.iam_scanner import (
    _is_dangerous_action,
    _extract_bedrock_actions,
    _check_managed_policy,
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


class TestCheckManagedPolicy:
    def test_reads_default_version_and_extracts(self):
        iam = MagicMock()
        iam.get_policy.return_value = {'Policy': {'DefaultVersionId': 'v2'}}
        iam.get_policy_version.return_value = {
            'PolicyVersion': {'Document': {'Statement': [
                {'Effect': 'Allow', 'Action': 'bedrock:InvokeModel'},
            ]}}
        }
        assert _check_managed_policy(iam, 'arn:aws:iam::aws:policy/Foo') == {'bedrock:InvokeModel'}
        iam.get_policy_version.assert_called_once_with(
            PolicyArn='arn:aws:iam::aws:policy/Foo', VersionId='v2')

    def test_unreadable_policy_returns_empty_set(self):
        iam = MagicMock()
        iam.get_policy.side_effect = Exception('AccessDenied')
        # 读不到时应吞掉异常、当作无权限（现在会 logger.warning）
        assert _check_managed_policy(iam, 'arn:aws:iam::aws:policy/Bar') == set()
