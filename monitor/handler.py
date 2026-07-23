import json
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config
from common.config import get_cost_thresholds, get_regions, get_alert_state, set_alert_state, get_webhook_config, put_item, query_by_pk, get_account_id, get_monitor_enabled
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


def fetch_region(region, start_daily, start_15min, start_5min, end, bucket_end=None):
    """单次拉取（含 NextToken 分页）算出该 region 的 5min/15min/daily 总量，
    以及各窗口的每模型每类型明细。取代旧的 fetch_region + fetch_detail 两遍扫描。

    - 查询窗口 = 日历日窗口与滚动窗口的并集：query_start = min(start_daily, start_15min)。
      午夜后（00:00~00:15）滚动窗口伸到前一天，query_start 会早于 start_daily，
      确保 5min/15min 桶不因跨日而丢失；白天 start_daily 更早，行为不变。
    - 每个统计窗口自己做显式 ts 过滤：daily 只累加 ts >= start_daily 的数据点，
      避免查询窗口扩大后把前一天尾部数据错误计入当天。
    - 5min/15min 窗口额外加上界 bucket_end（最近已关闭桶的右边界），确保每轮只统计
      已完整关闭的桶，避免相邻两轮重复计同一个未关闭桶。daily 不受限（累计到当前）。
    - Period=300 下 CloudWatch 只返回有数据的桶，缺失桶不返回；token 计数恒非负，
      跳过 0 值不影响总量，且避免为 0 创建空模型项（与旧 fetch_detail 行为一致）。
    - NextToken 循环：改读原始 per-model SEARCH 后单页可能超过 10.08 万数据点上限
      （当前规模远未触及），加分页循环做防御。
    """
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=API_TIMEOUT)

    total = {'5min': 0, '15min': 0, 'daily': 0}
    models = {'5min': {}, '15min': {}, 'daily': {}}

    # 滚动窗口在 00:00~00:15 会伸到前一天，查询起点取两种口径的并集
    query_start = min(start_daily, start_15min)

    next_token = None
    while True:
        kwargs = {'MetricDataQueries': QUERIES, 'StartTime': query_start, 'EndTime': end}
        if next_token:
            kwargs['NextToken'] = next_token
        resp = cw.get_metric_data(**kwargs)
        for r in resp['MetricDataResults']:
            model_name = extract_model_name(r['Label'])
            # 无 ModelId 的裸 metric 序列（CloudWatch 元数据延迟的瞬时产物）：整条跳过。
            # 它是同批数据的无维度聚合视图，计入会重复总量并污染模型明细。
            if not model_name:
                continue
            token_type = extract_token_type(r['Label'])
            for ts, val in zip(r['Timestamps'], r['Values']):
                if val <= 0:
                    continue
                # 查询窗口已比日历日宽（午夜场景），每个统计窗口显式过滤
                if ts >= start_daily:
                    total['daily'] += val
                    _add_model(models['daily'], model_name, token_type, val)
                if ts >= start_15min and (bucket_end is None or ts < bucket_end):
                    total['15min'] += val
                    _add_model(models['15min'], model_name, token_type, val)
                if ts >= start_5min and (bucket_end is None or ts < bucket_end):
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


def _pick_baselines(records, now):
    """从当天已有记录中选出 5min 和 15min 基线。

    只认全 region 成功（complete=True）且带 cost_daily 字段的记录。
    返回 (base_5min, base_15min)，无可用基线返回 None。
    """
    valid = [r for r in records
             if r.get('complete') and 'cost_daily' in r]
    valid.sort(key=lambda r: r['timestamp'])
    base_5 = valid[-1] if valid else None
    cutoff = (now - timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
    older = [r for r in valid if r['timestamp'] <= cutoff]
    base_15 = older[-1] if older else None
    return base_5, base_15


def _delta(cost_daily, base):
    """计算费用增量：当前累计 - 基线累计，负值钳 0。

    无基线时（午夜后第一轮）返回全天累计，即增量 = 本次全部费用。
    """
    if base is None:
        return cost_daily
    return max(cost_daily - float(base['cost_daily']), 0)


def _model_deltas(models_daily_now, base):
    """计算每模型 token 增量（当前 - 基线），供 Top 模型明细使用。

    返回 {model: {token_type: delta_count}}，只包含有增量的模型。
    """
    base_models = (base or {}).get('models_daily') or {}
    deltas = {}
    for m, tc in models_daily_now.items():
        base_tc = base_models.get(m, {})
        d = {t: max(int(tc.get(t, 0)) - int(base_tc.get(t, 0)), 0) for t in TOKEN_TYPES}
        if any(d.values()):
            deltas[m] = d
    return deltas


def handler(event, context):
    # 总开关检查：关闭时跳过全部监控逻辑（省 CloudWatch 费用）
    if not get_monitor_enabled():
        logger.info("Monitor disabled via config, skipping")
        return {'statusCode': 200, 'skipped': True, 'reason': 'monitor_disabled'}

    now = datetime.now(timezone.utc)
    start_daily = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # CloudWatch 以 Period=300 从 UTC 零点对齐桶边界（00:00, 00:05, 00:10, ...）。
    # 每轮只统计"已完整关闭"的桶（ts < bucket_end），避免：
    #   - 旧问题：start 不对齐导致桶被漏（系统性低估 ~50%）
    #   - 新问题：纳入当前未关闭桶导致相邻两轮重复计同一桶（系统性高估 ~50%）
    # bucket_end = floor(now) 即最近一个已关闭桶的右边界，start = bucket_end - window。
    # 午夜场景（00:00~00:15）：start_15min 会落到前一天（如 23:45），fetch_region 内
    # query_start = min(start_daily, start_15min) 自动扩展查询窗口，确保滚动桶不丢失。
    def _floor_to_period(ts, period=300):
        epoch = int(ts.timestamp())
        floored = epoch - (epoch % period)
        return datetime.fromtimestamp(floored, tz=timezone.utc)

    bucket_end = _floor_to_period(now)
    start_5min = bucket_end - timedelta(seconds=300)
    start_15min = bucket_end - timedelta(seconds=900)

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
        futures = {executor.submit(fetch_region, r, start_daily, start_15min, start_5min, now, bucket_end): r for r in regions}
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

    # === Delta 基线读取 ===
    # 必须在写入本轮记录之前读：否则查回的最新记录就是本轮刚写的自己
    # （complete=True、cost_daily=当前值），基线==当前值 → delta 恒为 0，
    # 5min 告警永久失效。且 DDB Query 最终一致，偶尔读不到自己时告警
    # 又"碰巧能用"，故障是非确定性的，更难排查。
    utc_date = now.strftime('%Y-%m-%d')
    try:
        records_today = query_by_pk(f'MONITOR#{utc_date}')
    except Exception as e:
        logger.error(f"Failed to read today's monitor records for delta: {e}")
        records_today = []

    base_5, base_15 = _pick_baselines(records_today, now)

    # === 持久化 Monitor 记录（含模型明细）===
    if region_results:
        try:
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
                # delta 告警所需基线字段
                cost_daily=str(round(cost['daily'], 6)),
                models_daily={m: {t: int(tc.get(t, 0)) for t in TOKEN_TYPES}
                              for m, tc in agg_models['daily'].items()} or None,
                complete=(not failed_regions),
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

    # === Delta 告警判定 ===
    cost_eval = {
        '5min':  _delta(cost['daily'], base_5),
        '15min': _delta(cost['daily'], base_15),
        'daily': cost['daily'],
    }
    logger.info(json.dumps({'cost_eval_5min': round(cost_eval['5min'], 4),
                            'cost_eval_15min': round(cost_eval['15min'], 4),
                            'cost_eval_daily': round(cost_eval['daily'], 4)}))

    alerts = []
    for window in ('5min', '15min', 'daily'):
        if window in cost_thresholds and cost_eval[window] > cost_thresholds[window]:
            alerts.append({'window': window, 'cost': cost_eval[window], 'threshold': cost_thresholds[window]})

    # warm-up 保护（按窗口分别判定）：当天已有记录但对应基线缺失时，跳过该窗口，
    # 避免把全天累计当作 5min/15min 增量误报。
    # records_today 为空（午夜首轮）时不跳过：此时全天累计本身就是自午夜以来的增量，
    # 用它判定是既有设计（见 test_first_run_of_day_no_records_uses_full_daily）。
    if records_today:
        skip = set()
        if base_5 is None:
            skip.add('5min')
        if base_15 is None:
            skip.add('15min')
        alerts = [a for a in alerts if a['window'] not in skip]

    if alerts:
        alerts = [a for a in alerts if not should_suppress(a['window'], now, webhooks)]

    if alerts:
        alert = alerts[0]
        window = alert['window']

        # 按预估 $ 排序 Top Region（仍用桶窗口口径，标注"近似"）
        region_costs = []
        for r in region_results:
            rc, _ = estimate_cost((r.get('models') or {}).get(window, {}))
            if rc > 0:
                region_costs.append((r['region'], rc))
        region_costs.sort(key=lambda x: x[1], reverse=True)
        top_regions = region_costs[:5]

        # Top 模型改用增量口径：展示新增费用来自哪个模型
        base_for_window = base_5 if window == '5min' else (base_15 if window == '15min' else None)
        model_delta_tokens = _model_deltas(agg_models['daily'], base_for_window)
        model_costs = []
        for model_name, type_counts in model_delta_tokens.items():
            mc, _ = estimate_cost({model_name: type_counts})
            if mc > 0:
                model_costs.append((model_name, mc))
        model_costs.sort(key=lambda x: x[1], reverse=True)
        top_models = model_costs[:5]

        msg = f"[Bedrock 费用提醒] 账号 {get_account_id()}\n"
        for a in alerts:
            msg += f"  {a['window']}: 预估增量 ${a['cost']:,.2f} > ${a['threshold']:,.2f}\n"
        if top_regions:
            msg += f"\nTop Region（{window}，近似）:\n"
            for region, rc in top_regions:
                msg += f"  {region}: ${rc:,.2f}\n"
        if top_models:
            msg += "\nTop 模型（增量口径，按预估 $）:\n"
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
