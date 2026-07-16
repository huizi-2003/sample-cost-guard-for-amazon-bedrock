import json
import logging
import time
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class WebhookError(Exception):
    """Webhook 发送最终失败（网络错误或渠道 API 返回错误码，重试后仍失败）。"""


def _build_payload(message, webhook_type):
    """根据渠道类型构造对应的 payload 格式。

    支持三种渠道:
      - feishu (飞书): msg_type + content.text
      - dingtalk (钉钉): msgtype + text.content
      - wecom (企业微信): msgtype + text.content
    """
    if webhook_type == 'feishu':
        return {"msg_type": "text", "content": {"text": message}}
    elif webhook_type == 'dingtalk':
        return {"msgtype": "text", "text": {"content": message}}
    elif webhook_type == 'wecom':
        return {"msgtype": "text", "text": {"content": message}}
    else:
        # 未知类型，降级使用飞书格式
        logger.warning(f"Unknown webhook type '{webhook_type}', falling back to feishu format")
        return {"msg_type": "text", "content": {"text": message}}


def _check_response(body, webhook_type):
    """检查各渠道的响应是否表示发送成功，返回 True/False"""
    if webhook_type == 'feishu':
        if body.get('code', 0) != 0:
            logger.error(f"Feishu webhook error: {body}")
            return False
    elif webhook_type in ('dingtalk', 'wecom'):
        if body.get('errcode', 0) != 0:
            logger.error(f"{webhook_type} webhook error: {body}")
            return False
    return True


def send_webhook(message, webhook_url, webhook_type='feishu'):
    """发送单个 webhook 通知，根据渠道类型选择对应格式。

    Args:
        message: 通知文本内容
        webhook_url: webhook 地址
        webhook_type: 渠道类型，支持 feishu / dingtalk / wecom

    Raises:
        WebhookError: 两次尝试均失败（网络异常或渠道 API 返回错误码）
    """
    if not webhook_url:
        logger.warning("No webhook URL configured, skipping notification")
        return

    payload = _build_payload(message, webhook_type)
    req = Request(webhook_url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})

    last_error = None
    for attempt in range(2):
        try:
            resp = urlopen(req, timeout=10)
            body = json.loads(resp.read().decode())
            if _check_response(body, webhook_type):
                return
            last_error = WebhookError(f"{webhook_type} API error: {body}")
        except Exception as e:
            logger.error(f"Webhook attempt {attempt + 1} failed: {e}")
            last_error = e
        if attempt == 0:
            time.sleep(1)

    raise WebhookError(f"Webhook delivery failed after 2 attempts: {last_error}") from last_error


def send_webhook_all(message, webhooks):
    """向所有已配置的 webhook 渠道发送通知。

    兼容三种场景：
      - 无配置（空列表）：跳过，仅打日志
      - 单配置：发一个
      - 多配置：逐个发送，某个失败不影响其他

    Args:
        message: 通知文本内容
        webhooks: list[dict]，每个 dict 含 url/type 字段（来自 get_webhook_config()）

    Returns:
        list[str]: 发送失败的渠道 name 列表（全部成功时为空列表）
    """
    if not webhooks:
        logger.warning("No webhook configured, skipping notification")
        return []

    failed = []
    for wh in webhooks:
        url = wh.get('url', '')
        wh_type = wh.get('type', 'feishu')
        name = wh.get('name', wh_type)
        if not url:
            logger.warning(f"Webhook '{name}' has no URL, skipping")
            continue
        try:
            send_webhook(message, url, wh_type)
            logger.info(f"Webhook '{name}' ({wh_type}) sent successfully")
        except Exception as e:
            logger.error(f"Webhook '{name}' ({wh_type}) failed: {e}")
            failed.append(name)

    if failed:
        logger.error(f"Notification NOT delivered to {len(failed)} channel(s): {', '.join(failed)}")
    return failed
