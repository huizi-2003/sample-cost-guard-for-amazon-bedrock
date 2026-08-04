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


DEFAULT_AI_MODEL_ID = 'global.amazon.nova-2-lite-v1:0'


def get_ai_summary_config():
    """获取 AI 账单总结配置。

    返回 dict: {'enabled': bool, 'model_id': str}
    默认关闭，model_id 默认为 Nova 2 Lite。
    """
    item = get_item('CONFIG', 'ai_summary')
    if not item:
        return {'enabled': False, 'model_id': DEFAULT_AI_MODEL_ID}
    return {
        'enabled': item.get('enabled', 'false') == 'true',
        'model_id': item.get('model_id', DEFAULT_AI_MODEL_ID),
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



# ===== 自动升级配置与历史 =====
# 放在 common/ 是因为 Web Lambda 和 Updater Lambda 都要读它，而两者的 zip
# 内容不同：web.zip 只含 web/ + common/，lambda.zip 含 common/monitor/
# reconciler/updater。common/ 是唯一的共同部分。

AUTO_UPGRADE_SK = 'auto_upgrade'
UPGRADE_HISTORY_PK = 'UPGRADE'

# 升级记录保留 90 天（与 RECONCILE#* 一致），由 DDB TTL 自动清理。
#
# 周检即使"无更新"也会记一条（见 updater.check_and_upgrade），所以这个 TTL
# 同时决定了记录条数的稳态上限：90 天 ≈ 13 条周检记录，页面固定只取最近
# 20 条，剩下的余量才够放真正的升级 / 失败记录。TTL 放到年级别时，周检记录
# 会把那 20 条窗口占满，真实记录虽然还在表里却翻不到了。
UPGRADE_HISTORY_TTL_DAYS = 90

# current_upgrade_id 这把"升级进行中"的锁多久后视为失效。
#
# 为什么必须有失效机制：Updater 被 Lambda 超时掐死时不会抛出可捕获的异常，
# 它的顶层兜底清理跑不到；而 save_auto_upgrade_config 是合并语义，周检也不会
# 清这个字段。没有失效判定，一次意外就让「立即升级」永久返回 409、前端永久
# 显示"正在更新"，用户无法自愈。
#
# 阈值取 6 小时：合法最长路径是升级 8 跳 + 回退 8 跳（updater.MAX_WATCH_HOPS
# × 每跳 15 分钟）≈ 4 小时，留 2 小时余量。
UPGRADE_LOCK_MAX_AGE_SECONDS = 6 * 3600


def _upgrade_lock_active(upgrade_id):
    """判断"升级进行中"标记是否仍然有效。

    upgrade_id 本身就是 ISO 时间戳（见 updater 的 upgrade_id = now_iso），
    直接按它判龄即可，不需要新增字段。只在读取时判断、不回写 DDB，保证读
    路径无副作用。

    解析失败一律视为失效：读不出时间的锁没法判断是否卡死，宁可放开让用户
    能重试——重试侧还有 CloudFormation 的 _IN_PROGRESS 检查兜着，不会叠加。
    """
    if not upgrade_id:
        return False
    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(str(upgrade_id).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - started).total_seconds()
    return age < UPGRADE_LOCK_MAX_AGE_SECONDS


def get_auto_upgrade_config():
    """读自动升级配置。

    自动更新默认开启：DDB 无记录即视为开启，用户在页面上关闭后才落库。
    这里不读任何部署参数——开关的唯一真相来源就是 DDB，避免栈参数和页面
    配置各说一套。

    current_upgrade_id 会做失效判定，超龄的锁按空返回（见 _upgrade_lock_active）。
    """
    item = get_item('CONFIG', AUTO_UPGRADE_SK) or {}
    raw = item.get('enabled')
    enabled = True if raw is None else str(raw).lower() == 'true'
    upgrade_id = item.get('current_upgrade_id') or ''
    return {
        'enabled': enabled,
        'last_check_at': item.get('last_check_at') or '',
        'last_status': item.get('last_status') or '',
        'last_error': item.get('last_error') or '',
        'last_known_good_sha': item.get('last_known_good_sha') or '',
        'current_upgrade_id': upgrade_id if _upgrade_lock_active(upgrade_id) else '',
    }


def save_auto_upgrade_config(**attrs):
    """局部更新自动升级配置（读-改-写；字段少、写入频率极低，够用）。"""
    current = get_item('CONFIG', AUTO_UPGRADE_SK) or {}
    merged = {k: v for k, v in current.items() if k not in ('PK', 'SK')}
    merged.update(attrs)
    put_item('CONFIG', AUTO_UPGRADE_SK, **merged)


def record_upgrade(upgrade_id, **attrs):
    """写入一条升级记录（PK=UPGRADE, SK=ISO 时间戳，便于按 PK 查全部历史）。

    整条覆盖。要在已有记录上补字段（例如只更新 status/error）请用
    merge_upgrade_record，否则 changelog、from_sha 等会被抹掉。
    """
    from datetime import datetime, timedelta, timezone
    expire_at = int((datetime.now(timezone.utc)
                     + timedelta(days=UPGRADE_HISTORY_TTL_DAYS)).timestamp())
    put_item(UPGRADE_HISTORY_PK, upgrade_id, expire_at=expire_at, **attrs)


def merge_upgrade_record(upgrade_id, **attrs):
    """在已有升级记录上局部更新（record_upgrade 是整条覆盖）。

    用于只知道 upgrade_id、拿不到原始上下文的收尾场景——典型是异常兜底：
    只想把状态改成 FAILED，但不能把 changelog 和版本信息一起丢掉。
    """
    current = get_item(UPGRADE_HISTORY_PK, upgrade_id) or {}
    merged = {k: v for k, v in current.items() if k not in ('PK', 'SK')}
    merged.update(attrs)
    record_upgrade(upgrade_id, **merged)


def get_upgrade_history(limit=20):
    """返回最近的升级记录，按时间倒序。"""
    items = query_by_pk(UPGRADE_HISTORY_PK)
    items.sort(key=lambda i: i.get('SK', ''), reverse=True)
    return items[:limit]
