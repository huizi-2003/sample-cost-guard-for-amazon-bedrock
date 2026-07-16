"""Unit tests for common/webhook.py

Covers:
- _build_payload: payload format for feishu/dingtalk/wecom/unknown
- send_webhook: retry logic, empty URL skip, timeout/error handling
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call
from urllib.error import URLError

from common.webhook import send_webhook, send_webhook_all, _build_payload, _check_response, WebhookError


# === _build_payload tests ===


class TestBuildPayload:
    """Payload construction for each webhook channel."""

    def test_feishu_format(self):
        payload = _build_payload("hello", "feishu")
        assert payload == {"msg_type": "text", "content": {"text": "hello"}}

    def test_dingtalk_format(self):
        payload = _build_payload("hello", "dingtalk")
        assert payload == {"msgtype": "text", "text": {"content": "hello"}}

    def test_wecom_format(self):
        payload = _build_payload("hello", "wecom")
        assert payload == {"msgtype": "text", "text": {"content": "hello"}}

    def test_unknown_type_falls_back_to_feishu(self):
        payload = _build_payload("hello", "slack")
        assert payload == {"msg_type": "text", "content": {"text": "hello"}}

    def test_empty_type_falls_back_to_feishu(self):
        payload = _build_payload("hello", "")
        assert payload == {"msg_type": "text", "content": {"text": "hello"}}

    def test_message_with_special_chars(self):
        msg = "[告警] 5min: 1,000,000 > 500,000\nTop Region:\n  us-east-1"
        payload = _build_payload(msg, "feishu")
        assert payload["content"]["text"] == msg


# === send_webhook tests ===


class TestSendWebhook:
    """Webhook sending behavior: retries, skip on empty URL, error handling."""

    @patch('common.webhook.urlopen')
    def test_successful_send_first_attempt(self, mock_urlopen):
        """Single attempt success - no retry."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_urlopen.return_value = mock_resp

        send_webhook("test", "https://hook.example.com/abc", "feishu")

        assert mock_urlopen.call_count == 1

    @patch('common.webhook.urlopen')
    def test_empty_url_skips_send(self, mock_urlopen):
        """Empty URL should not make any HTTP request."""
        send_webhook("test", "", "feishu")
        mock_urlopen.assert_not_called()

    @patch('common.webhook.urlopen')
    def test_none_url_skips_send(self, mock_urlopen):
        """None URL should not make any HTTP request."""
        send_webhook("test", None, "feishu")
        mock_urlopen.assert_not_called()

    @patch('common.webhook.time.sleep')
    @patch('common.webhook.urlopen')
    def test_retry_on_first_failure(self, mock_urlopen, mock_sleep):
        """First attempt fails, second attempt succeeds."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_urlopen.side_effect = [URLError("timeout"), mock_resp]

        send_webhook("test", "https://hook.example.com/abc", "feishu")

        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch('common.webhook.time.sleep')
    @patch('common.webhook.urlopen')
    def test_max_two_attempts_on_persistent_failure(self, mock_urlopen, mock_sleep):
        """Both attempts fail - should raise WebhookError after two attempts."""
        mock_urlopen.side_effect = [URLError("timeout"), URLError("timeout")]

        with pytest.raises(WebhookError):
            send_webhook("test", "https://hook.example.com/abc", "feishu")

        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch('common.webhook.urlopen')
    def test_request_contains_correct_payload_feishu(self, mock_urlopen):
        """Request body matches feishu format."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_urlopen.return_value = mock_resp

        send_webhook("hello", "https://hook.example.com/abc", "feishu")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body == {"msg_type": "text", "content": {"text": "hello"}}
        assert req.get_header('Content-type') == 'application/json'

    @patch('common.webhook.urlopen')
    def test_request_contains_correct_payload_dingtalk(self, mock_urlopen):
        """Request body matches dingtalk format."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"errcode": 0}).encode()
        mock_urlopen.return_value = mock_resp

        send_webhook("hello", "https://hook.example.com/abc", "dingtalk")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body == {"msgtype": "text", "text": {"content": "hello"}}

    @patch('common.webhook.urlopen')
    def test_timeout_set_to_10s(self, mock_urlopen):
        """Request uses 10s timeout."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_urlopen.return_value = mock_resp

        send_webhook("test", "https://hook.example.com/abc", "feishu")

        _, kwargs = mock_urlopen.call_args
        assert kwargs.get('timeout') == 10


# === _check_response tests ===


class TestCheckResponse:
    """Response checking for each channel."""

    def test_feishu_success(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"code": 0}, "feishu")
        assert "error" not in caplog.text.lower()

    def test_feishu_error(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"code": 10001, "msg": "invalid token"}, "feishu")
        assert "Feishu webhook error" in caplog.text

    def test_dingtalk_success(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"errcode": 0}, "dingtalk")
        assert "error" not in caplog.text.lower()

    def test_dingtalk_error(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"errcode": 300001, "errmsg": "invalid token"}, "dingtalk")
        assert "dingtalk webhook error" in caplog.text

    def test_wecom_success(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"errcode": 0}, "wecom")
        assert "error" not in caplog.text.lower()

    def test_wecom_error(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _check_response({"errcode": 93000, "errmsg": "invalid webhook url"}, "wecom")
        assert "wecom webhook error" in caplog.text
