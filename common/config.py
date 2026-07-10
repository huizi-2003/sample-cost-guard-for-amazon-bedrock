import os
import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get('DDB_TABLE', 'bedrock-cost-guard')

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(TABLE_NAME)
    return _table


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


def get_thresholds():
    items = query_by_pk('THRESHOLD')
    defaults = {'5min': 999999999, '15min': 999999999, 'daily': 999999999}
    if not items:
        return defaults
    result = {item['SK']: int(item['value']) for item in items}
    # 确保所有 key 都存在，缺失的用默认值补齐
    for key, default_val in defaults.items():
        if key not in result:
            result[key] = default_val
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


def get_reconcile_dates(limit=30):
    """获取最近有对账数据的日期列表（带分页）"""
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
