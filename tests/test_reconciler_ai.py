"""Unit tests for the AI billing-summary path in reconciler/handler.py.

Covers:
- _get_ai_summary:
    * returns None when AGENTCORE_ENDPOINT_ARN is not set
    * derives the AgentCore client region from the endpoint ARN (not hardcoded us-east-1)
    * falls back to AWS_REGION when the ARN region segment is empty
    * splits the endpoint ARN into runtime ARN + qualifier and sends model_id/prompt
    * parses plain text / JSON {"result": ...} / JSON string responses
    * returns None (does not crash the daily report) on any exception
- handler AI gating:
    * notify_policy 'never'  -> AI is NOT called and nothing is pushed
    * notify_policy 'workday' + non-workday -> AI is NOT called and nothing is pushed
    * notify_policy 'always' + AI enabled  -> AI called once, summary appended, pushed
    * notify_policy 'always' + AI disabled -> AI NOT called, report still pushed
"""
import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest


EP_ARN = (
    "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:"
    "runtime/bedrock_cost_guard_summarizer-abc/"
    "runtime-endpoint/bedrock_cost_guard_summarizer_ep"
)


def _mock_invoke_response(raw_bytes):
    """Build a fake invoke_agent_runtime response whose body.read() returns raw_bytes."""
    body = MagicMock()
    body.read.return_value = raw_bytes
    return {'response': body}


# ===== _get_ai_summary =====


class TestGetAiSummary:
    def test_returns_none_when_endpoint_missing(self):
        from reconciler.handler import _get_ai_summary
        with patch.dict(os.environ, {}, clear=True):
            assert _get_ai_summary("report", "2026-07-20", {'model_id': 'm'}) is None

    @patch('reconciler.handler.boto3')
    def test_derives_region_from_arn(self, mock_boto3):
        """Region must come from the endpoint ARN, not a hardcoded us-east-1."""
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(b"summary text")
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            out = _get_ai_summary("report", "2026-07-20", {'model_id': 'us.amazon.nova-2-lite-v1:0'})

        assert out == "summary text"
        mock_boto3.client.assert_called_once_with('bedrock-agentcore', region_name='ap-northeast-1')

    @patch('reconciler.handler.boto3')
    def test_falls_back_to_aws_region_env(self, mock_boto3):
        """When the ARN region segment is empty, fall back to the Lambda AWS_REGION."""
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(b"x")
        mock_boto3.client.return_value = mock_client

        bad_arn = "arn:aws:bedrock-agentcore::123456789012:runtime/x/runtime-endpoint/y"
        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': bad_arn, 'AWS_REGION': 'eu-west-1'}, clear=True):
            _get_ai_summary("report", "d", {'model_id': 'm'})

        mock_boto3.client.assert_called_once_with('bedrock-agentcore', region_name='eu-west-1')

    @patch('reconciler.handler.boto3')
    def test_passes_runtime_arn_qualifier_and_payload(self, mock_boto3):
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(b"ok")
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            _get_ai_summary("REPORT-BODY", "2026-07-20", {'model_id': 'us.amazon.nova-2-lite-v1:0'})

        kwargs = mock_client.invoke_agent_runtime.call_args[1]
        assert kwargs['agentRuntimeArn'] == (
            "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:"
            "runtime/bedrock_cost_guard_summarizer-abc"
        )
        assert kwargs['qualifier'] == 'bedrock_cost_guard_summarizer_ep'
        payload = json.loads(kwargs['payload'].decode('utf-8'))
        assert payload['model_id'] == 'us.amazon.nova-2-lite-v1:0'
        assert 'REPORT-BODY' in payload['prompt']

    @patch('reconciler.handler.boto3')
    def test_parses_json_result_field(self, mock_boto3):
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(
            json.dumps({'result': '中文摘要'}).encode('utf-8')
        )
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            out = _get_ai_summary("r", "d", {'model_id': 'm'})
        assert out == '中文摘要'

    @patch('reconciler.handler.boto3')
    def test_parses_json_string(self, mock_boto3):
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(
            json.dumps("直接字符串").encode('utf-8')
        )
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            out = _get_ai_summary("r", "d", {'model_id': 'm'})
        assert out == '直接字符串'

    @patch('reconciler.handler.boto3')
    def test_returns_raw_text_when_not_json(self, mock_boto3):
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = _mock_invoke_response(b"plain non-json text")
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            out = _get_ai_summary("r", "d", {'model_id': 'm'})
        assert out == "plain non-json text"

    @patch('reconciler.handler.boto3')
    def test_returns_none_on_exception(self, mock_boto3):
        """AI failure must not crash the daily report — return None."""
        from reconciler.handler import _get_ai_summary
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.side_effect = Exception("boom")
        mock_boto3.client.return_value = mock_client

        with patch.dict(os.environ, {'AGENTCORE_ENDPOINT_ARN': EP_ARN}, clear=True):
            out = _get_ai_summary("r", "d", {'model_id': 'm'})
        assert out is None


# ===== handler: AI gating (must not incur cost when not pushing) =====


class TestHandlerAiGating:
    @patch('reconciler.handler._get_ai_summary')
    @patch('reconciler.handler.get_ai_summary_config')
    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_account_id')
    @patch('reconciler.handler.get_notify_policy')
    def test_never_policy_skips_ai_and_push(
        self, mock_policy, mock_acct, mock_webhooks, mock_reconcile, mock_send,
        mock_ai_cfg, mock_get_ai,
    ):
        mock_policy.return_value = 'never'
        mock_acct.return_value = '123456789012'
        mock_webhooks.return_value = [{'url': 'http://t', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}
        mock_ai_cfg.return_value = {'enabled': True, 'model_id': 'us.amazon.nova-2-lite-v1:0'}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_get_ai.assert_not_called()   # 不推送时绝不调用 AI（否则白白花钱）
        mock_ai_cfg.assert_not_called()   # 甚至不必读取 AI 配置
        mock_send.assert_not_called()

    @patch('reconciler.handler._get_ai_summary')
    @patch('reconciler.handler.get_ai_summary_config')
    @patch('reconciler.handler.is_workday')
    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_account_id')
    @patch('reconciler.handler.get_notify_policy')
    def test_workday_off_skips_ai_and_push(
        self, mock_policy, mock_acct, mock_webhooks, mock_reconcile, mock_send,
        mock_is_workday, mock_ai_cfg, mock_get_ai,
    ):
        mock_policy.return_value = 'workday'
        mock_is_workday.return_value = False   # 今天不是工作日
        mock_acct.return_value = '123456789012'
        mock_webhooks.return_value = [{'url': 'http://t', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}
        mock_ai_cfg.return_value = {'enabled': True, 'model_id': 'us.amazon.nova-2-lite-v1:0'}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_get_ai.assert_not_called()
        mock_send.assert_not_called()

    @patch('reconciler.handler._get_ai_summary')
    @patch('reconciler.handler.get_ai_summary_config')
    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_account_id')
    @patch('reconciler.handler.get_notify_policy')
    def test_always_enabled_calls_ai_and_appends(
        self, mock_policy, mock_acct, mock_webhooks, mock_reconcile, mock_send,
        mock_ai_cfg, mock_get_ai,
    ):
        mock_policy.return_value = 'always'
        mock_acct.return_value = '123456789012'
        mock_webhooks.return_value = [{'url': 'http://t', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}
        mock_ai_cfg.return_value = {'enabled': True, 'model_id': 'us.amazon.nova-2-lite-v1:0'}
        mock_get_ai.return_value = 'AI摘要内容'

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_get_ai.assert_called_once()
        mock_send.assert_called_once()
        pushed_text = mock_send.call_args[0][0]
        assert '📊 AI 总结' in pushed_text
        assert 'AI摘要内容' in pushed_text

    @patch('reconciler.handler._get_ai_summary')
    @patch('reconciler.handler.get_ai_summary_config')
    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_account_id')
    @patch('reconciler.handler.get_notify_policy')
    def test_always_enabled_ai_failure_appends_warning(
        self, mock_policy, mock_acct, mock_webhooks, mock_reconcile, mock_send,
        mock_ai_cfg, mock_get_ai,
    ):
        mock_policy.return_value = 'always'
        mock_acct.return_value = '123456789012'
        mock_webhooks.return_value = [{'url': 'http://t', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}
        mock_ai_cfg.return_value = {'enabled': True, 'model_id': 'us.amazon.nova-2-lite-v1:0'}
        mock_get_ai.return_value = None

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_get_ai.assert_called_once()
        mock_send.assert_called_once()
        pushed_text = mock_send.call_args[0][0]
        assert '⚠ AI 总结生成失败' in pushed_text

    @patch('reconciler.handler._get_ai_summary')
    @patch('reconciler.handler.get_ai_summary_config')
    @patch('reconciler.handler.send_webhook_all')
    @patch('reconciler.handler.reconcile_one')
    @patch('reconciler.handler.get_webhook_config')
    @patch('reconciler.handler.get_account_id')
    @patch('reconciler.handler.get_notify_policy')
    def test_always_disabled_skips_ai_but_pushes(
        self, mock_policy, mock_acct, mock_webhooks, mock_reconcile, mock_send,
        mock_ai_cfg, mock_get_ai,
    ):
        mock_policy.return_value = 'always'
        mock_acct.return_value = '123456789012'
        mock_webhooks.return_value = [{'url': 'http://t', 'type': 'feishu'}]
        mock_reconcile.return_value = {'msg': 'report', 'total_actual': 1.0, 'reconcile_diff_pct': 0.0}
        mock_ai_cfg.return_value = {'enabled': False, 'model_id': 'us.amazon.nova-2-lite-v1:0'}

        from reconciler.handler import handler
        result = handler({}, None)

        assert result['statusCode'] == 200
        mock_get_ai.assert_not_called()
        mock_send.assert_called_once()
