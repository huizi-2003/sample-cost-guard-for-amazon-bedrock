import json
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config
from common.config import get_cost_thresholds, get_regions, get_alert_state, set_alert_state, get_webhook_config, put_item, get_account_id
from common.pricing import estimate_cost
from common.webhook import send_webhook_all
from common.labels import extract_model_name, extract_token_type

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_TIMEOUT = Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 0})

# 单次拉取：直接读两个 namespace 的原始 per-model SEARCH（都 ReturnData=True）。
# 一条查询（start_daily→now, Period=300）即可同时算出 5min/15min/daily 总量与每模型明细，
# 取代旧的 QUERIES_TOTAL + QUERIES_DETAIL 两遍扫描（每 region 每周期 2 次 → 1 次，约省 50%）。
# 口径已用真实数据验证与旧实现等价：
#   - bedrock 总量 = 各 per-model TokenCount 之和（与旧 SUM(SEARCH) 同一表达式，恒等）
#   - mantle 总量 = 各 per-model (TotalInputTokens+TotalOutputTokens) 之和，
#     与旧的账户级空维度聚合口径在真实数据上精确相等
QUERIES = [
    {'Id': 'bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 300)", 'ReturnData': True},
    {'Id': 'mantle', 'Expression': "SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 300)", 'ReturnData': True},
]

TOKEN_TYPES = ('input', 'output', 'cache_read', 'cache_write')


def _add_model(bucket, model_name, token_type, val):
    """把一个数据点累加进 {model: {input/output/cache_read/cache_write}} 明细桶。"""
    if model_name not in bucket:
        bucket[model_name] = {t: 0 for t in TOKEN_TYPES}
    bucket[model_name][token_type] += val


def fetch_region(region, start_daily, start_15min, start_5min, end):
    """单次拉取（含 NextToken 分页）算出该 region 的 5min/15min/daily 总量，
    以及各窗口的每模型每类型明细。取代旧的 fetch_region + fetch_detail 两遍扫描。

    - 查询窗口固定为 start_daily→now：它是旧明细查询窗口的超集，因此按 ts>=阈值
      过滤即可精确复现旧的 daily/15min/5min 总量及 5min 每模型明细，无数值漂移
      （旧 fetch_detail 里向前对齐一个 Period 的 hack 因此不再需要）。
    - Period=300 下 CloudWatch 只返回有数据的桶，缺失桶不返回；token 计数恒非负，
      跳过 0 值不影响总量，且避免为 0 创建空模型项（与旧 fetch_detail 行为一致）。
    - NextToken 循环：改读原始 per-model SEARCH 后单页可能超过 10.08 万数据点上限
      （当前规模远未触及），加分页循环做防御。
    """
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=API_TIMEOUT)

    total = {'5min': 0, '15min': 0, 'daily': 0}
    models = {'5min': {}, '15min': {}, 'daily': {}}

    next_token = None
    while True:
        kwargs = {'MetricDataQueries': QUERIES, 'StartTime': start_daily, 'EndTime': end}
        if next_token:
            kwargs['NextToken'] = next_token
        resp = cw.get_metric_data(**kwargs)
        for r in resp['MetricDataResults']:
            model_name = extract_model_name(r['Label'])
            token_type = extract_token_type(r['Label'])
            for ts, val in zip(r['Timestamps'], r['Values']):
                if val <= 0:
                    continue
                total['daily'] += val
                _add_model(models['daily'], model_name, token_type, val)
                if ts >= start_15min:
                    total['15min'] += val
                    _add_model(models['15min'], model_name, token_type, val)
                if ts >= start_5min:
                    total['5min'] += val
                    _add_model(models['5min'], model_name, token_type, val)
        next_token = resp.get('NextToken')
        if not next_token:
            break

    return {
        'region': region,
        '5min': total['5min'], '15min': total['15min'], 'daily': total['daily'],
        'models': models,
    }


def should_suppress(window, now, webhooks):
    if window == '5min':
        return False
    val = get_alert_state(window)
    if not val:
        return False
    try:
        if window == 'daily':
            today = now.strftime('%Y-%m-%d')
            return val == today
        elif window == '15min':
            return (now.timestamp() - float(val)) < 900
    except (ValueError, TypeError):
        send_webhook_all(f"[Bedrock 监控] 账号 {get_account_id()} | {window} 的提醒状态数据损坏，请检查。", webhooks)
        return False
    return False


def mark_alerted(window, now):
    if window == '5min':
        return
    val = now.strftime('%Y-%m-%d') if window == 'daily' else str(now.timestamp())
    set_alert_state(window, val)


def handler(event, context):
    now = datetime.now(timezone.utc)
    start_daily = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_15min = now - timedelta(minutes=15)
    start_5min = now - timedelta(minutes=5)

    webhooks = get_webhook_config()

    try:
        cost_thresholds = get_cost_thresholds()
    except Exception as e:
        logger.error(f"Failed to read cost thresholds from DDB: {e}")
        send_webhook_all(f"[Bedrock 费用监控] 账号 {get_account_id()} | 读取费用阈值失败，监控未运行。", webhooks)
        return {'statusCode': 500, 'error': 'threshold_read_failed'}

    regions = get_regions()
    if not regions:
        send_webhook_all(f"[Bedrock 监控] 账号 {get_account_id()} | DDB 中未配置监控 Region。", webhooks)
        return {'statusCode': 500, 'error': 'no_regions'}

    region_results = []
    failed_regions = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_region, r, start_daily, start_15min, start_5min, now): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                region_results.append(future.result())
            except Exception as e:
                logger.warning(f"Region {region} failed: {e}")
                failed_regions.append(region)

    # region 查询失败会让其 token 从总量中缺失，导致成本被低估、真实超阈值可能不告警。
    # >3 个失败视为紧急，每轮都发；1~3 个失败每日去重发一次低调提醒，避免抖动刷屏。
    if len(failed_regions) > 3:
        send_webhook_all(f"[Bedrock 监控] 账号 {get_account_id()} | 异常：{len(failed_regions)} 个 Region 查询失败: {', '.join(failed_regions[:10])}", webhooks)
    elif failed_regions:
        today = now.strftime('%Y-%m-%d')
        if get_alert_state('region_fetch_failed') != today:
            send_webhook_all(f"[Bedrock 监控] 账号 {get_account_id()} | 提醒：{len(failed_regions)} 个 Region 查询失败: {', '.join(failed_regions)}，今日预估费用可能偏低", webhooks)
            set_alert_state('region_fetch_failed', today)

    total_5min = sum(r['5min'] for r in region_results)
    total_15min = sum(r['15min'] for r in region_results)
    total_daily = sum(r['daily'] for r in region_results)

    logger.info(json.dumps({'5min': total_5min, '15min': total_15min, 'daily': total_daily}))

    # 按窗口聚合各 region 的每模型每类型明细（供持久化、成本估算、告警共用）
    agg_models = {'5min': {}, '15min': {}, 'daily': {}}
    for r in region_results:
        rm = r.get('models') or {}
        for window in ('5min', '15min', 'daily'):
            for model_name, type_counts in rm.get(window, {}).items():
                dest = agg_models[window].setdefault(model_name, {t: 0 for t in TOKEN_TYPES})
                for token_type in TOKEN_TYPES:
                    dest[token_type] += type_counts.get(token_type, 0)

    # 预估费用（$）：token 明细 × 价目表。unpriced 为有量但无价的模型（费用被低估）
    cost = {}
    unpriced = {}
    for window in ('5min', '15min', 'daily'):
        cost[window], unpriced[window] = estimate_cost(agg_models[window])
    logger.info(json.dumps({'cost_5min': round(cost['5min'], 4),
                            'cost_15min': round(cost['15min'], 4),
                            'cost_daily': round(cost['daily'], 4)}))

    # === 持久化 Monitor 记录（含模型明细）===
    if region_results:
        try:
            utc_date = now.strftime('%Y-%m-%d')
            utc_time = now.strftime('%H:%M')
            expire_at = int((now + timedelta(days=2)).timestamp())

            # 模型明细（5min 窗口）取本轮已聚合的数据，类型量转 int 落库
            all_models_5min = {
                m: {t: int(tc.get(t, 0)) for t in TOKEN_TYPES}
                for m, tc in agg_models['5min'].items()
            }

            put_item(
                f'MONITOR#{utc_date}',
                f'T#{utc_time}',
                total_5min=int(total_5min),
                total_daily=int(total_daily),
                timestamp=now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                region_count=len(region_results),
                expire_at=expire_at,
                models=all_models_5min if all_models_5min else None,
            )
        except Exception as e:
            logger.error(f"Failed to persist monitor record: {e}")
    else:
        logger.warning("No region data available, skipping monitor record persistence")

    # 费用阈值未配置：直接通知用户（每日去重），不做阈值比较
    if not cost_thresholds:
        today = now.strftime('%Y-%m-%d')
        if get_alert_state('cost_unconfigured') != today:
            send_webhook_all(
                f"[Bedrock 费用监控] 账号 {get_account_id()} | 尚未配置费用告警阈值($)，费用红线未生效。"
                f"今日累计预估 ${cost['daily']:,.2f}。请在 Web Console 配置阈值。",
                webhooks,
            )
            set_alert_state('cost_unconfigured', today)
        return {'statusCode': 200, 'alerts': [], 'cost_thresholds_configured': False}

    alerts = []
    for window in ('5min', '15min', 'daily'):
        if window in cost_thresholds and cost[window] > cost_thresholds[window]:
            alerts.append({'window': window, 'cost': cost[window], 'threshold': cost_thresholds[window]})

    if alerts:
        alerts = [a for a in alerts if not should_suppress(a['window'], now, webhooks)]

    if alerts:
        alert = alerts[0]
        window = alert['window']

        # 按预估 $ 排序 Top Region / Top 模型（数据取本轮已聚合结果，不再查 CloudWatch）
        region_costs = []
        for r in region_results:
            rc, _ = estimate_cost((r.get('models') or {}).get(window, {}))
            if rc > 0:
                region_costs.append((r['region'], rc))
        region_costs.sort(key=lambda x: x[1], reverse=True)
        top_regions = region_costs[:5]

        model_costs = []
        for model_name, type_counts in agg_models[window].items():
            mc, _ = estimate_cost({model_name: type_counts})
            if mc > 0:
                model_costs.append((model_name, mc))
        model_costs.sort(key=lambda x: x[1], reverse=True)
        top_models = model_costs[:5]

        msg = f"[Bedrock 费用提醒] 账号 {get_account_id()}\n"
        for a in alerts:
            msg += f"  {a['window']}: 预估 ${a['cost']:,.2f} > ${a['threshold']:,.2f}\n"
        if top_regions:
            msg += f"\nTop Region（{window}，按预估 $）:\n"
            for region, rc in top_regions:
                msg += f"  {region}: ${rc:,.2f}\n"
        if top_models:
            msg += "\nTop 模型（按预估 $）:\n"
            for label, mc in top_models:
                msg += f"  {label}: ${mc:,.2f}\n"
        if unpriced[window]:
            msg += f"\n⚠ 未定价模型（预估已低估，请在 common/pricing.py 补价）: {', '.join(sorted(unpriced[window]))}\n"
        if failed_regions:
            msg += f"\n⚠ 本次有 {len(failed_regions)} 个 Region 查询失败（{', '.join(failed_regions[:10])}），实际费用可能高于上述预估\n"

        send_webhook_all(msg, webhooks)
        for a in alerts:
            mark_alerted(a['window'], now)
        logger.warning(f"ALERT: {msg}")

    return {'statusCode': 200, 'alerts': [a['window'] for a in alerts]}
