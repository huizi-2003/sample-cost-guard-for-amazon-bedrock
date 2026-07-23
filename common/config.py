import os
import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get('DDB_TABLE', 'bedrock-cost-guard')

_table = None
_account_id = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(TABLE_NAME)
    return _table


def get_account_id():
    """获取当前 AWS 账号 ID（模块级缓存，整个 Lambda 生命周期只调一次 STS）。"""
    global _account_id
    if _account_id is None:
        _account_id = boto3.client('sts').get_caller_identity()['Account']
    return _account_id


def get_item(pk, sk):
    resp = _get_table().get_item(Key={'PK': pk, 'SK': sk})
    return resp.get('Item')


def put_item(pk, sk, **attrs):
    item = {'PK': pk, 'SK': sk}
    for k, v in attrs.items():
        if v is not None:
            item[k] = v
    _get_table().put_item(Item=item)


def query_by_pk(pk):
    table = _get_table()
    all_items = []
    kwargs = {'KeyConditionExpression': Key('PK').eq(pk)}
    while True:
        resp = table.query(**kwargs)
        all_items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return all_items


def get_cost_thresholds():
    """费用告警阈值（单位：美元 $）。

    存于 PK=COST_THRESHOLD。返回 {window: float}，仅包含已配置的窗口；
    未配置任何窗口时返回空 dict，供监控据此判断"未配置"并直接通知用户。
    """
    items = query_by_pk('COST_THRESHOLD')
    result = {}
    for item in items:
        try:
            result[item['SK']] = float(item['value'])
        except (ValueError, TypeError):
            pass
    return result


DEFAULT_REGIONS = 'us-east-1,us-east-2,us-west-1,us-west-2,eu-central-1,eu-west-1,eu-west-3,ap-northeast-1,ap-southeast-1,ap-southeast-2'


def get_regions():
    item = get_item('CONFIG', 'regions')
    if not item:
        put_item('CONFIG', 'regions', value=DEFAULT_REGIONS)
        return [r.strip() for r in DEFAULT_REGIONS.split(',')]
    return [r.strip() for r in item['value'].split(',') if r.strip()]


def get_alert_state(window):
    item = get_item('ALERT_STATE', f'last-alert-{window}')
    return item.get('value') if item else None


def set_alert_state(window, value):
    put_item('ALERT_STATE', f'last-alert-{window}', value=value)


def save_reconcile_record(date, model, data):
    from datetime import datetime, timedelta, timezone
    # 注意：90 天 TTL 是 get_reconcile_dates 全表 Scan 成本可接受的前提，
    # 若要延长保留期，需同步把该函数的 Scan 改为日期索引 Query。
    expire_at = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())
    put_item(f'RECONCILE#{date}', model, expire_at=expire_at, **data)


def get_reconcile_by_date(date):
    items = query_by_pk(f'RECONCILE#{date}')
    return {item['SK']: {k: v for k, v in item.items() if k not in ('PK', 'SK')} for item in items}


def get_webhook_config():
    """从 DDB 读取 webhook 配置（兼容旧格式）。

    返回 list[dict]，每个 dict 含 name/url/type 字段。
    兼容逻辑：
      - 新格式 (SK=webhooks): 直接返回 items 列表
      - 旧格式 (SK=webhook): 迁移为新格式并返回
      - 无配置: 返回空列表
    """
    # 尝试读新格式
    item = get_item('CONFIG', 'webhooks')
    if item:
        return item.get('items', [])

    # 兼容旧格式：单条 webhook
    old = get_item('CONFIG', 'webhook')
    if old and old.get('url'):
        migrated = [{'name': old.get('type', 'feishu'), 'url': old['url'], 'type': old.get('type', 'feishu')}]
        # 自动迁移到新格式
        put_item('CONFIG', 'webhooks', items=migrated)
        return migrated

    return []


def save_webhook_config(items):
    """保存多 webhook 配置到 DDB。

    Args:
        items: list[dict]，每个 dict 含 name/url/type 字段
    """
    put_item('CONFIG', 'webhooks', items=items)


def get_monitor_enabled():
    """用量监控总开关。True=开启（默认），False=关闭。

    key 不存在视为开启（首次部署无需手动写配置），DDB 异常向上抛出。
    """
    item = get_item('CONFIG', 'monitor_enabled')
    if item and item.get('value') == 'false':
        return False
    return True


def save_monitor_enabled(enabled: bool):
    """保存用量监控总开关状态。"""
    put_item('CONFIG', 'monitor_enabled', value='true' if enabled else 'false')


def get_notify_policy():
    """获取日报推送策略。

    返回值:
        'always'  — 每天推送
        'workday' — 仅工作日推送（基于中国法定节假日）
        'never'   — 不推送
    """
    item = get_item('CONFIG', 'notify_policy')
    if item and item.get('value') in ('always', 'workday', 'never'):
        return item['value']
    return 'always'


def save_notify_policy(policy):
    """保存日报推送策略。

    Args:
        policy: 'always'、'workday' 或 'never'
    """
    if policy not in ('always', 'workday', 'never'):
        raise ValueError(f"Invalid notify_policy: {policy}, must be 'always', 'workday' or 'never'")
    put_item('CONFIG', 'notify_policy', value=policy)


def get_reconcile_dates(limit=30):
    """获取最近有对账数据的日期列表（带分页）。

    实现是全表 Scan + Filter，成本可接受的前提是表处于 TTL 稳态（几 MB 量级）：
      - RECONCILE#* 记录 90 天 TTL（见 save_reconcile_record）
      - MONITOR#* 记录 2 天 TTL
    若将来延长对账 TTL 到年级别、或往本表新增无 TTL 的大体量记录类型，
    此 Scan 会随之退化，届时应改为写入时维护日期索引项（固定 PK + Query）。
    """
    table = _get_table()
    all_items = []
    scan_kwargs = {
        'FilterExpression': 'begins_with(PK, :prefix) AND SK = :sk',
        'ExpressionAttributeValues': {':prefix': 'RECONCILE#', ':sk': '_summary'},
        'ProjectionExpression': 'PK',
    }
    while True:
        resp = table.scan(**scan_kwargs)
        all_items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            break
        scan_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    dates = sorted([item['PK'].replace('RECONCILE#', '') for item in all_items], reverse=True)
    return dates[:limit]


def get_ai_summary_config():
    """获取 AI 账单总结配置。

    返回 dict: {'enabled': bool, 'model_id': str}
    默认关闭，model_id 默认为 Nova 2 Lite。
    """
    item = get_item('CONFIG', 'ai_summary')
    if not item:
        return {'enabled': False, 'model_id': 'us.amazon.nova-2-lite-v1:0'}
    return {
        'enabled': item.get('enabled', 'false') == 'true',
        'model_id': item.get('model_id', 'us.amazon.nova-2-lite-v1:0'),
    }


def save_ai_summary_config(enabled: bool, model_id: str):
    """保存 AI 账单总结配置。

    Args:
        enabled: 是否开启 AI 总结
        model_id: Bedrock 模型 ID
    """
    put_item('CONFIG', 'ai_summary',
             enabled='true' if enabled else 'false',
             model_id=model_id)
