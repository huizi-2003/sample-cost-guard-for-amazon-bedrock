import json
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config
from common.config import get_thresholds, get_regions, get_alert_state, set_alert_state, get_webhook_config, put_item
from common.webhook import send_webhook_all

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_TIMEOUT = Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 0})

QUERIES_TOTAL = [
    {'Id': 'search_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 300)", 'ReturnData': False},
    {'Id': 'bedrock_total', 'Expression': 'SUM(search_bedrock)', 'ReturnData': True},
    {'Id': 'mantle_in', 'MetricStat': {'Metric': {'Namespace': 'AWS/BedrockMantle', 'MetricName': 'TotalInputTokens', 'Dimensions': []}, 'Period': 300, 'Stat': 'Sum'}, 'ReturnData': False},
    {'Id': 'mantle_out', 'MetricStat': {'Metric': {'Namespace': 'AWS/BedrockMantle', 'MetricName': 'TotalOutputTokens', 'Dimensions': []}, 'Period': 300, 'Stat': 'Sum'}, 'ReturnData': False},
    {'Id': 'mantle_total', 'Expression': 'FILL(mantle_in,0) + FILL(mantle_out,0)', 'ReturnData': True},
]

QUERIES_DETAIL = [
    {'Id': 'detail_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 300)", 'ReturnData': True},
    {'Id': 'detail_mantle', 'Expression': "SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 300)", 'ReturnData': True},
]


def fetch_region(region, start_daily, start_15min, start_5min, end):
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=API_TIMEOUT)
    resp = cw.get_metric_data(MetricDataQueries=QUERIES_TOTAL, StartTime=start_daily, EndTime=end)
    total_5min = 0
    total_15min = 0
    total_daily = 0
    for r in resp['MetricDataResults']:
        for ts, val in zip(r['Timestamps'], r['Values']):
            total_daily += val
            if ts >= start_15min:
                total_15min += val
            if ts >= start_5min:
                total_5min += val
    return {'region': region, '5min': total_5min, '15min': total_15min, 'daily': total_daily}


def extract_model_name(label):
    """从 CW SEARCH label 中提取模型名（去掉 namespace 前缀和 token 类型后缀）。"""
    label = label.replace('AWS/Bedrock ', '').replace('AWS/BedrockMantle ', '')
    label = label.replace('global.anthropic.', '').replace('anthropic.', '')
    for suffix in (' CacheReadInputTokenCount', ' CacheWriteInputTokenCount',
                   ' InputTokenCount', ' OutputTokenCount',
                   ' TotalInputTokens', ' TotalOutputTokens', ' Tokens'):
        if label.endswith(suffix):
            label = label[:-len(suffix)]
            break
    return label.strip()


def extract_token_type(label):
    """从 CW SEARCH label 中提取 token 类型：input/output/cache_read/cache_write。"""
    if 'CacheRead' in label or 'cacheread' in label.lower():
        return 'cache_read'
    if 'CacheWrite' in label or 'cachewrite' in label.lower():
        return 'cache_write'
    if 'Output' in label:
        return 'output'
    # 默认归为 input（InputTokenCount, TotalInputTokens, 或无法识别的）
    return 'input'


DETAIL_PERIOD = 300  # 明细查询的 Period（秒），与 QUERIES_DETAIL 中的 300 对齐


def fetch_detail(region, start, end):
    """返回 {model_name: {input:x, output:y, cache_read:z, cache_write:w}} 按类型拆分的明细。"""
    # CloudWatch Period 桶按整 5 分钟边界对齐。提醒窗口（如 now-5min ~ now）
    # 又窄又不对齐，且指标有发布延迟，直接查常常拿不到完整的桶。
    # 这里把 start 向下对齐到 Period 边界、再往前多取一个 Period，保证至少覆盖
    # 一个完整的桶；实际归属仍用 ts >= start 过滤。
    aligned_start = datetime.fromtimestamp(
        (int(start.timestamp()) // DETAIL_PERIOD - 1) * DETAIL_PERIOD, tz=timezone.utc
    )
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=API_TIMEOUT)
    resp = cw.get_metric_data(MetricDataQueries=QUERIES_DETAIL, StartTime=aligned_start, EndTime=end)
    models = {}
    for r in resp['MetricDataResults']:
        model_name = extract_model_name(r['Label'])
        token_type = extract_token_type(r['Label'])
        for ts, val in zip(r['Timestamps'], r['Values']):
            if ts >= start and val > 0:
                if model_name not in models:
                    models[model_name] = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
                models[model_name][token_type] += val
    return models


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
        send_webhook_all(f"[Bedrock 监控] {window} 的提醒状态数据损坏，请检查。", webhooks)
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
        thresholds = get_thresholds()
    except Exception as e:
        logger.error(f"Failed to read thresholds from DDB: {e}")
        send_webhook_all("[Bedrock 监控] 读取阈值失败，监控未运行。", webhooks)
        return {'statusCode': 500, 'error': 'threshold_read_failed'}

    regions = get_regions()
    if not regions:
        send_webhook_all("[Bedrock 监控] DDB 中未配置监控 Region。", webhooks)
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

    if len(failed_regions) > 3:
        send_webhook_all(f"[Bedrock 监控] 异常：{len(failed_regions)} 个 Region 查询失败: {', '.join(failed_regions[:10])}", webhooks)

    total_5min = sum(r['5min'] for r in region_results)
    total_15min = sum(r['15min'] for r in region_results)
    total_daily = sum(r['daily'] for r in region_results)

    logger.info(json.dumps({'5min': total_5min, '15min': total_15min, 'daily': total_daily}))

    # === 持久化 Monitor 记录（含模型明细）===
    if region_results:
        try:
            utc_date = now.strftime('%Y-%m-%d')
            utc_time = now.strftime('%H:%M')
            expire_at = int((now + timedelta(days=2)).timestamp())

            # 查询所有 region 的模型明细（5min 窗口）
            all_models_5min = {}
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_detail, r['region'], start_5min, now): r['region'] for r in region_results}
                for future in as_completed(futures):
                    try:
                        models = future.result()
                        for model_name, type_counts in models.items():
                            if model_name not in all_models_5min:
                                all_models_5min[model_name] = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
                            for token_type, val in type_counts.items():
                                all_models_5min[model_name][token_type] += int(val)
                    except Exception:
                        pass

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

    alerts = []
    if total_5min > thresholds['5min']:
        alerts.append({'window': '5min', 'total': total_5min, 'threshold': thresholds['5min']})
    if total_15min > thresholds['15min']:
        alerts.append({'window': '15min', 'total': total_15min, 'threshold': thresholds['15min']})
    if total_daily > thresholds['daily']:
        alerts.append({'window': 'daily', 'total': total_daily, 'threshold': thresholds['daily']})

    if alerts:
        alerts = [a for a in alerts if not should_suppress(a['window'], now, webhooks)]

    if alerts:
        alert = alerts[0]
        detail_start = {'5min': start_5min, '15min': start_15min, 'daily': start_daily}[alert['window']]
        top_regions = sorted(region_results, key=lambda r: r[alert['window']], reverse=True)[:5]

        all_models = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_detail, r['region'], detail_start, now): r['region'] for r in top_regions if r[alert['window']] > 0}
            for future in as_completed(futures):
                try:
                    models = future.result()
                    for model_name, type_counts in models.items():
                        all_models[model_name] = all_models.get(model_name, 0) + sum(type_counts.values())
                except Exception as e:
                    logger.warning(f"Detail fetch failed: {e}")

        top_models = sorted(all_models.items(), key=lambda x: x[1], reverse=True)[:5]

        msg = "[Bedrock 用量提醒]\n"
        for a in alerts:
            msg += f"  {a['window']}: {a['total']:,.0f} > {a['threshold']:,.0f}\n"
        msg += f"\nTop Region（{alert['window']}）:\n"
        for r in top_regions:
            if r[alert['window']] > 0:
                msg += f"  {r['region']}: {r[alert['window']]:,.0f}\n"
        if top_models:
            msg += "\nTop 模型:\n"
            for label, val in top_models:
                msg += f"  {label}: {val:,.0f}\n"

        send_webhook_all(msg, webhooks)
        for a in alerts:
            mark_alerted(a['window'], now)
        logger.warning(f"ALERT: {msg}")

    return {'statusCode': 200, 'alerts': [a['window'] for a in alerts]}
