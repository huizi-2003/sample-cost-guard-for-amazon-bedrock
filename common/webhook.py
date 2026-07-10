import json
import logging
import time
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


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
    """检查各渠道的响应是否表示发送成功"""
    if webhook_type == 'feishu':
        if body.get('code', 0) != 0:
            logger.error(f"Feishu webhook error: {body}")
    elif webhook_type == 'dingtalk':
        if body.get('errcode', 0) != 0:
            logger.error(f"DingTalk webhook error: {body}")
    elif webhook_type == 'wecom':
        if body.get('errcode', 0) != 0:
            logger.error(f"WeCom webhook error: {body}")


def send_webhook(message, webhook_url, webhook_type='feishu'):
    """发送单个 webhook 通知，根据渠道类型选择对应格式。

    Args:
        message: 通知文本内容
        webhook_url: webhook 地址
        webhook_type: 渠道类型，支持 feishu / dingtalk / wecom
    """
    if not webhook_url:
        logger.warning("No webhook URL configured, skipping notification")
        return

    payload = _build_payload(message, webhook_type)
    req = Request(webhook_url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})

    for attempt in range(2):
        try:
            resp = urlopen(req, timeout=10)
            body = json.loads(resp.read().decode())
            _check_response(body, webhook_type)
            return
        except Exception as e:
            logger.error(f"Webhook attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                time.sleep(1)


def send_webhook_all(message, webhooks):
    """向所有已配置的 webhook 渠道发送通知。

    兼容三种场景：
      - 无配置（空列表）：跳过，仅打日志
      - 单配置：发一个
      - 多配置：逐个发送，某个失败不影响其他

    Args:
        message: 通知文本内容
        webhooks: list[dict]，每个 dict 含 url/type 字段（来自 get_webhook_config()）
    """
    if not webhooks:
        logger.warning("No webhook configured, skipping notification")
        return

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
