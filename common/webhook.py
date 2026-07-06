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
    """发送 webhook 通知，根据 DDB 中配置的渠道类型选择对应格式。

    Args:
        message: 通知文本内容
        webhook_url: webhook 地址（来自 DDB CONFIG#webhook 的 url 字段）
        webhook_type: 渠道类型（来自 DDB CONFIG#webhook 的 type 字段），
                      支持 feishu / dingtalk / wecom
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
