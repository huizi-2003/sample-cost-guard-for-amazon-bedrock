import os
import re
import sys
import time
import json
import math
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config as BotoConfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from common.config import (
    get_cost_thresholds, get_regions, get_reconcile_by_date,
    get_reconcile_dates, put_item, get_item, query_by_pk,
    get_webhook_config, save_webhook_config,
    get_notify_policy, save_notify_policy,
    get_monitor_enabled, save_monitor_enabled
)
from common.pricing import PRICING, match_pricing as _match_pricing
from common.labels import (
    extract_model_name as _extract_model_name,
    extract_token_type as _extract_token_type,
)
from common.iam_scanner import execute_iam_scan

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CW_TIMEOUT = BotoConfig(connect_timeout=10, read_timeout=30, retries={'max_attempts': 1})

RECONCILER_FUNCTION_NAME = os.environ.get('RECONCILER_FUNCTION_NAME', 'bedrock-cost-guard-reconciler')

app = FastAPI()

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


@app.get('/')
async def index():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))


@app.get('/static/{file_path:path}')
async def static_files(file_path: str):
    full_path = os.path.realpath(os.path.join(STATIC_DIR, file_path))
    if not full_path.startswith(os.path.realpath(STATIC_DIR) + os.sep):
        return JSONResponse({'error': 'Not found'}, status_code=404)
    if os.path.isfile(full_path):
        return FileResponse(full_path)
    return JSONResponse({'error': 'Not found'}, status_code=404)


# ===== 对账数据 =====

@app.get('/api/reconcile/summary')
async def reconcile_summary():
    """返回本月费用总览（本月1号至今）"""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1).strftime('%Y-%m-%d')

    dates = get_reconcile_dates(limit=365)
    # 只保留本月的日期
    dates = [d for d in dates if d >= month_start]

    if not dates:
        return {'period': {}, 'totals': {}, 'daily_costs': [], 'model_totals': [], 'routing_breakdown': []}

    daily_costs = []
    model_agg = {}  # {model: total_cost}

    for date in dates:
        data = get_reconcile_by_date(date)
        day_total = 0
        day_models = {}
        for sk, record in data.items():
            if sk.startswith('_'):
                continue
            cost = float(record.get('actual_cost', 0))
            day_total += cost
            day_models[sk] = day_models.get(sk, 0) + cost
            model_agg[sk] = model_agg.get(sk, 0) + cost
        daily_costs.append({'date': date, 'cost': round(day_total, 4), 'models': {k: round(v, 4) for k, v in day_models.items()}})

    # 排序：日期正序
    daily_costs.sort(key=lambda x: x['date'])

    # 计算 totals
    total_cost = sum(d['cost'] for d in daily_costs)
    days_with_data = len(daily_costs)
    daily_avg = total_cost / days_with_data if days_with_data else 0

    # 模型排名
    model_totals_sorted = sorted(model_agg.items(), key=lambda x: x[1], reverse=True)
    model_totals = []
    for model, cost in model_totals_sorted:
        pct = cost / total_cost * 100 if total_cost > 0 else 0
        model_totals.append({'model': model, 'cost': round(cost, 4), 'pct': round(pct, 1)})

    # 路由方式拆分
    routing_agg = {}
    for model, cost in model_agg.items():
        routing = _extract_routing(model)
        routing_agg[routing] = routing_agg.get(routing, 0) + cost
    routing_breakdown = []
    for routing, cost in sorted(routing_agg.items(), key=lambda x: x[1], reverse=True):
        pct = cost / total_cost * 100 if total_cost > 0 else 0
        routing_breakdown.append({'routing': routing, 'cost': round(cost, 4), 'pct': round(pct, 1)})

    return {
        'period': {
            'start': dates[-1] if dates else '',
            'end': dates[0] if dates else '',
            'days_with_data': days_with_data,
            'month': now.strftime('%Y-%m'),
        },
        'totals': {
            'total_cost': round(total_cost, 2),
            'daily_avg': round(daily_avg, 2),
        },
        'daily_costs': daily_costs,
        'model_totals': model_totals,
        'routing_breakdown': routing_breakdown,
    }


def _extract_routing(model_identity):
    """从模型身份字符串中提取路由方式"""
    lower = model_identity.lower()
    if 'cross-region' in lower:
        return 'cross-region'
    elif 'mantle' in lower:
        return 'mantle'
    else:
        return 'direct'


@app.get('/api/reconcile/dates')
async def reconcile_dates():
    dates = get_reconcile_dates(limit=30)
    return dates


@app.get('/api/reconcile/{date}')
async def reconcile_detail(date: str):
    data = get_reconcile_by_date(date)
    return data


# ===== 监控数据 =====

@app.get('/api/monitor/{date}')
async def monitor_data(date: str):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return JSONResponse({'error': 'Invalid date format, expected YYYY-MM-DD'}, status_code=400)

    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return JSONResponse({'error': 'Invalid date'}, status_code=400)

    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if date > utc_today:
        return JSONResponse({'error': 'Future date not allowed'}, status_code=400)

    items = query_by_pk(f'MONITOR#{date}')
    records = []
    for item in items:
        records.append({
            'time': item['SK'].replace('T#', ''),
            'total_5min': int(item.get('total_5min', 0)),
            'total_daily': int(item.get('total_daily', 0)),
            'timestamp': item.get('timestamp', ''),
            'region_count': int(item.get('region_count', 0)),
        })
    records.sort(key=lambda r: r['time'])
    return records


# ===== 按模型实时查 CloudWatch =====


def _fetch_region_models(region, start, end):
    """查单个 region 的模型明细时间序列，返回 {model: [(timestamp_str, value), ...]}"""
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=CW_TIMEOUT)
    queries = [
        {'Id': 'detail_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 3600)", 'ReturnData': True},
        {'Id': 'detail_mantle', 'Expression': "SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 3600)", 'ReturnData': True},
    ]
    resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    models = {}
    for r in resp['MetricDataResults']:
        label = _extract_model_name(r['Label'])
        if not label:
            continue
        for ts, val in zip(r['Timestamps'], r['Values']):
            if val > 0:
                ts_str = ts.astimezone(timezone.utc).strftime('%H:%M')
                models.setdefault(label, []).append((ts_str, val))
    return models


@app.get('/api/monitor/{date}/models')
async def monitor_models(date: str):
    """返回各模型的时间序列。支持 last24h（滚动24小时）或指定日期。
    优先从 DDB 读取缓存，无模型数据时 fallback 到实时 CW 查询。"""
    if date == 'last24h':
        return _monitor_models_last24h()

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return JSONResponse({'error': 'Invalid date format'}, status_code=400)

    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if date > utc_today:
        return JSONResponse({'error': 'Future date not allowed'}, status_code=400)

    # 尝试从 DDB 读取（带 models 字段的缓存数据）
    items = query_by_pk(f'MONITOR#{date}')
    has_models = any('models' in item and item['models'] for item in items)

    if has_models:
        all_models = {}  # {model: {time: tokens}}
        for item in items:
            time_str = item['SK'].replace('T#', '')
            models = item.get('models')
            if not models:
                continue
            for model, tokens in models.items():
                if model not in all_models:
                    all_models[model] = {}
                # 兼容新格式（dict with type counts）和旧格式（int）
                if isinstance(tokens, dict):
                    total = sum(int(v) for v in tokens.values())
                else:
                    total = int(tokens)
                all_models[model][time_str] = all_models[model].get(time_str, 0) + total

        result = {}
        for model, time_map in all_models.items():
            points = sorted(time_map.items(), key=lambda x: x[0])
            cumulative = 0
            series = []
            for t, v in points:
                cumulative += v
                series.append({'time': t, 'tokens': cumulative})
            result[model] = series
        return result

    # Fallback: 无模型缓存，实时查 CW
    return _fetch_models_from_cw(date)


def _monitor_models_last24h():
    """滚动 24h 窗口的模型时间序列。从 DDB 查两天数据，按 timestamp 过滤。"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

    today_str = now.strftime('%Y-%m-%d')
    yesterday_str = cutoff.strftime('%Y-%m-%d')

    items = query_by_pk(f'MONITOR#{today_str}')
    if yesterday_str != today_str:
        items += query_by_pk(f'MONITOR#{yesterday_str}')

    # 按 timestamp 过滤 24h 内
    items = [i for i in items if i.get('timestamp', '') >= cutoff_str]

    has_models = any('models' in item and item['models'] for item in items)

    if has_models:
        all_models = {}
        for item in items:
            # 内部 key 使用完整可排序格式 "YYYY-MM-DD HH:MM"（跨年安全）
            ts_raw = item.get('timestamp', '')
            if ts_raw:
                time_str = ts_raw[:16].replace('T', ' ')  # '2026-07-16T14:05:00Z' → '2026-07-16 14:05'
            else:
                time_str = item['PK'].replace('MONITOR#', '') + ' ' + item['SK'].replace('T#', '')
            models = item.get('models')
            if not models:
                continue
            for model, tokens in models.items():
                if model not in all_models:
                    all_models[model] = {}
                if isinstance(tokens, dict):
                    total = sum(int(v) for v in tokens.values())
                else:
                    total = int(tokens)
                all_models[model][time_str] = all_models[model].get(time_str, 0) + total

        result = {}
        for model, time_map in all_models.items():
            points = sorted(time_map.items(), key=lambda x: x[0])
            cumulative = 0
            series = []
            for t, v in points:
                cumulative += v
                series.append({'time': t[5:].replace('-', '/'), 'tokens': cumulative})
            result[model] = series
        return result

    # Fallback: 实时查 CW（24h 窗口）
    return _fetch_models_from_cw_last24h()


def _fetch_models_from_cw_last24h():
    """实时从 CloudWatch 拉取最近 24h 各模型时间序列。"""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    end = now

    regions = get_regions()
    if not regions:
        return {}

    all_models = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_region_models_24h, r, start, end): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                region_models = future.result()
                for model, points in region_models.items():
                    if model not in all_models:
                        all_models[model] = {}
                    for ts_str, val in points:
                        all_models[model][ts_str] = all_models[model].get(ts_str, 0) + val
            except Exception as e:
                logger.warning(f"Region {region} CW model fetch failed (last24h): {e}")

    result = {}
    for model, time_map in all_models.items():
        points = sorted(time_map.items(), key=lambda x: x[0])
        cumulative = 0
        series = []
        for t, v in points:
            cumulative += v
            series.append({'time': t[5:].replace('-', '/'), 'tokens': int(cumulative)})
        result[model] = series
    return result


def _fetch_region_models_24h(region, start, end):
    """查单个 region 的模型明细时间序列（24h 窗口），时间 key 带日期前缀。"""
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=CW_TIMEOUT)
    queries = [
        {'Id': 'detail_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 3600)", 'ReturnData': True},
        {'Id': 'detail_mantle', 'Expression': "SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 3600)", 'ReturnData': True},
    ]
    resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    models = {}
    for r in resp['MetricDataResults']:
        label = _extract_model_name(r['Label'])
        if not label:
            continue
        for ts, val in zip(r['Timestamps'], r['Values']):
            if val > 0:
                ts_str = ts.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')
                models.setdefault(label, []).append((ts_str, val))
    return models


def _fetch_models_from_cw(date):
    """实时从 CloudWatch 拉取各模型时间序列（慢路径）"""
    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    day_start = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    if date == utc_today:
        end = datetime.now(timezone.utc)
    else:
        end = day_start + timedelta(days=1)
    start = day_start

    regions = get_regions()
    if not regions:
        return {}

    all_models = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_region_models, r, start, end): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                region_models = future.result()
                for model, points in region_models.items():
                    if model not in all_models:
                        all_models[model] = {}
                    for ts_str, val in points:
                        all_models[model][ts_str] = all_models[model].get(ts_str, 0) + val
            except Exception as e:
                logger.warning(f"Region {region} CW model fetch failed, series may be understated: {e}")

    result = {}
    for model, time_map in all_models.items():
        points = sorted(time_map.items(), key=lambda x: x[0])
        cumulative = 0
        series = []
        for t, v in points:
            cumulative += v
            series.append({'time': t, 'tokens': int(cumulative)})
        result[model] = series

    return result




# ===== 今日成本估算 =====
# 价目表与匹配逻辑集中在 common/pricing.py（monitor / web 共用同一份）


@app.get('/api/today-cost')
async def today_cost():
    """滚动 24h 窗口的预估费用。

    从 DDB 读取最近 24 小时的监控数据，按价格常量计算估算费用。
    若 DDB 中无新格式（按类型拆分）数据，fallback 到实时查 CW。

    返回:
      - total_cost: 最近 24h 预估总费用 ($)
      - models: {model: {cost, input, output, cache_read, cache_write, tokens}}
      - timeline: [{time, cost}] 累计费用趋势
      - unpriced_models: 无法匹配价格的模型列表
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

    # 滚动 24h 可能跨两个 UTC 日，查两个 PK
    today_str = now.strftime('%Y-%m-%d')
    yesterday_str = cutoff.strftime('%Y-%m-%d')

    items = query_by_pk(f'MONITOR#{today_str}')
    if yesterday_str != today_str:
        items += query_by_pk(f'MONITOR#{yesterday_str}')

    # 按 timestamp 过滤只保留 24h 内的记录
    items = [i for i in items if i.get('timestamp', '') >= cutoff_str]

    # 检查是否有新格式数据（models 值为 dict）
    has_new_format = False
    for item in items:
        models = item.get('models')
        if models and isinstance(models, dict):
            for v in models.values():
                if isinstance(v, dict):
                    has_new_format = True
                    break
        if has_new_format:
            break

    if has_new_format:
        return _calc_cost_from_ddb(items)

    # Fallback: 实时查 CW 拿按类型拆分的数据
    return await _calc_cost_from_cw()


def _calc_cost_from_ddb(items):
    """从 DDB 新格式 models 数据计算费用（支持跨天 24h 窗口）。"""
    model_totals = {}
    timeline_points = {}
    unpriced = set()

    for item in items:
        # 内部 key 使用完整可排序格式 "YYYY-MM-DD HH:MM"（字典序==时间序，跨年安全）
        ts_raw = item.get('timestamp', '')
        if ts_raw:
            time_str = ts_raw[:16].replace('T', ' ')  # '2026-07-16T14:05:00Z' → '2026-07-16 14:05'
        else:
            # fallback: 从 PK + SK 拼出完整 key
            time_str = item['PK'].replace('MONITOR#', '') + ' ' + item['SK'].replace('T#', '')
        models = item.get('models')
        if not models:
            continue

        point_cost = 0
        for model_name, type_counts in models.items():
            # 跳过旧格式（非 dict）
            if not isinstance(type_counts, dict):
                continue

            prices = _match_pricing(model_name)
            if not prices:
                unpriced.add(model_name)
                continue

            if model_name not in model_totals:
                model_totals[model_name] = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}

            for token_type in ('input', 'output', 'cache_read', 'cache_write'):
                tokens = int(type_counts.get(token_type, 0))
                model_totals[model_name][token_type] += tokens
                point_cost += tokens / 1_000_000 * prices[token_type]

        if point_cost > 0:
            timeline_points[time_str] = timeline_points.get(time_str, 0) + point_cost

    return _build_cost_response(model_totals, timeline_points, unpriced)


async def _calc_cost_from_cw():
    """Fallback: 实时从 CW 查按类型拆分的 token 数据（滚动 24h 窗口），计算估算费用。"""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    end = now

    regions = get_regions()
    if not regions:
        return {'total_cost': 0, 'models': {}, 'timeline': [], 'unpriced_models': []}

    model_totals = {}
    timeline_points = {}
    unpriced = set()

    def fetch_region_typed(region):
        """查单个 region，返回按模型+类型拆分的时间序列。"""
        session = boto3.session.Session()
        cw = session.client('cloudwatch', region_name=region, config=CW_TIMEOUT)
        queries = [
            {'Id': 'detail_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 3600)", 'ReturnData': True},
            {'Id': 'detail_mantle', 'Expression': "SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 3600)", 'ReturnData': True},
        ]
        resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        results = []
        for r in resp['MetricDataResults']:
            label = r['Label']
            model_name = _extract_model_name(label)
            if not model_name:  # 裸 metric 序列（无 ModelId），跳过避免污染/重复
                continue
            token_type = _extract_token_type(label)
            for ts, val in zip(r['Timestamps'], r['Values']):
                if val > 0:
                    ts_str = ts.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')
                    results.append((model_name, token_type, ts_str, val))
        return results

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_region_typed, r): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                for model_name, token_type, ts_str, val in future.result():
                    prices = _match_pricing(model_name)
                    if not prices:
                        unpriced.add(model_name)
                        continue
                    if model_name not in model_totals:
                        model_totals[model_name] = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
                    model_totals[model_name][token_type] += int(val)
                    cost_delta = val / 1_000_000 * prices[token_type]
                    timeline_points[ts_str] = timeline_points.get(ts_str, 0) + cost_delta
            except Exception as e:
                logger.warning(f"Region {region} CW cost fetch failed, today-cost may be understated: {e}")

    return _build_cost_response(model_totals, timeline_points, unpriced)


def _build_cost_response(model_totals, timeline_points, unpriced):
    """从聚合数据构建 today-cost 响应。"""
    model_costs = {}
    total_cost = 0
    for model_name, type_counts in model_totals.items():
        prices = _match_pricing(model_name)
        if not prices:
            continue
        cost = 0
        for token_type in ('input', 'output', 'cache_read', 'cache_write'):
            cost += type_counts[token_type] / 1_000_000 * prices[token_type]
        total_tokens = sum(type_counts.values())
        model_costs[model_name] = {
            'cost': round(cost, 6),
            'tokens': total_tokens,
            **{k: v for k, v in type_counts.items()},
        }
        total_cost += cost

    # 累计费用趋势线（内部 key 为 YYYY-MM-DD HH:MM，排序后格式化为前端 MM/DD HH:MM）
    timeline = []
    cumulative = 0
    for time_str in sorted(timeline_points.keys()):
        cumulative += timeline_points[time_str]
        timeline.append({'time': time_str[5:].replace('-', '/'), 'cost': round(cumulative, 6)})

    return {
        'total_cost': round(total_cost, 4),
        'total_tokens': sum(m['tokens'] for m in model_costs.values()),
        'models': model_costs,
        'timeline': timeline,
        'unpriced_models': sorted(unpriced),
    }


# ===== 配置管理 =====

# AWS region 格式校验：兼容标准区（us-east-1）和 gov/iso 区（us-gov-east-1）。
# 白名单式格式校验，杜绝 XSS 载荷和乱码值污染监控配置。
REGION_RE = re.compile(r'^[a-z]{2}(-[a-z]+){1,2}-\d+$')

@app.get('/api/config/regions')
async def get_config_regions():
    return get_regions()


@app.put('/api/config/regions')
async def put_config_regions(request: Request):
    body = await request.json()
    regions = body.get('regions', [])
    if not isinstance(regions, list):
        return JSONResponse({'error': 'regions must be a list'}, status_code=400)
    if len(regions) > 50:
        return JSONResponse({'error': 'too many regions (max 50)'}, status_code=400)
    cleaned = []
    seen = set()
    for r in regions:
        if not isinstance(r, str):
            return JSONResponse({'error': 'each region must be a string'}, status_code=400)
        r = r.strip().lower()
        if not REGION_RE.match(r):
            return JSONResponse({'error': f'invalid region format: {r!r}'}, status_code=400)
        if r not in seen:
            seen.add(r)
            cleaned.append(r)
    if not cleaned:
        return JSONResponse({'error': 'regions must not be empty'}, status_code=400)
    put_item('CONFIG', 'regions', value=','.join(cleaned))
    return {'ok': True}


@app.get('/api/config/cost-thresholds')
async def get_config_cost_thresholds():
    """费用告警阈值（$）。"""
    return get_cost_thresholds()


@app.put('/api/config/cost-thresholds')
async def put_config_cost_thresholds(request: Request):
    data = await request.json()
    valid_keys = {'5min', '15min', 'daily'}
    for key, val in data.items():
        if key not in valid_keys:
            return JSONResponse({'error': f'Invalid key: {key}, must be one of {valid_keys}'}, status_code=400)
        try:
            f_val = float(val)
        except (ValueError, TypeError):
            return JSONResponse({'error': f'Invalid value for {key}: must be a number'}, status_code=400)
        if not math.isfinite(f_val):
            # nan/inf 能通过 float() 但会让 "cost > threshold" 恒为 False，静默关闭告警
            return JSONResponse({'error': f'Invalid value for {key}: must be a finite number'}, status_code=400)
        if f_val < 0:
            return JSONResponse({'error': f'Invalid value for {key}: must be non-negative'}, status_code=400)
    for key, val in data.items():
        # DynamoDB(resource) 不接受 Python float，统一以字符串存储；读取端 float() 解析
        put_item('COST_THRESHOLD', key, value=str(float(val)))
    return {'ok': True}


@app.get('/api/config/webhook')
async def get_config_webhook():
    """获取所有 webhook 配置（兼容旧单条格式）"""
    return get_webhook_config()


@app.put('/api/config/webhook')
async def put_config_webhook(request: Request):
    """保存全部 webhook 配置（接收列表，最多 3 个）"""
    data = await request.json()
    items = data if isinstance(data, list) else data.get('items', [])
    if not isinstance(items, list):
        return JSONResponse({'error': 'items must be a list'}, status_code=400)
    ALLOWED_TYPES = ('feishu', 'dingtalk', 'wecom')
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            return JSONResponse({'error': 'each item must be an object with url and type'}, status_code=400)
        url = item.get('url', '').strip() if isinstance(item.get('url'), str) else ''
        wh_type = item.get('type', 'feishu')
        if wh_type not in ALLOWED_TYPES:
            return JSONResponse({'error': f"type must be one of: {', '.join(ALLOWED_TYPES)}"}, status_code=400)
        if url and not url.startswith(('https://', 'http://')):
            return JSONResponse({'error': f"url must start with https:// or http://"}, status_code=400)
        name = item.get('name', wh_type)
        name = name.strip() if isinstance(name, str) else wh_type
        name = name or wh_type
        if url:  # 忽略空 URL 的条目
            cleaned.append({'name': name, 'url': url, 'type': wh_type})
    if len(cleaned) > 3:
        return JSONResponse({'error': '最多配置 3 个 Webhook 渠道'}, status_code=400)
    save_webhook_config(cleaned)
    return {'ok': True, 'count': len(cleaned)}


@app.get('/api/config/monitor-enabled')
async def get_config_monitor_enabled():
    """获取用量监控总开关状态"""
    return {'enabled': get_monitor_enabled()}


@app.put('/api/config/monitor-enabled')
async def put_config_monitor_enabled(request: Request):
    """设置用量监控总开关"""
    data = await request.json()
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return JSONResponse({'error': "enabled must be a boolean"}, status_code=400)
    save_monitor_enabled(enabled)
    return {'ok': True, 'enabled': enabled}


@app.get('/api/config/notify-policy')
async def get_config_notify_policy():
    """获取日报推送策略"""
    return {'policy': get_notify_policy()}


@app.put('/api/config/notify-policy')
async def put_config_notify_policy(request: Request):
    """设置日报推送策略：always（每天）、workday（仅工作日）或 never（不推送）"""
    data = await request.json()
    policy = data.get('policy', '').strip()
    if policy not in ('always', 'workday', 'never'):
        return JSONResponse({'error': "policy must be 'always', 'workday' or 'never'"}, status_code=400)
    save_notify_policy(policy)
    return {'ok': True, 'policy': policy}


# ===== IAM Bedrock 权限扫描 =====
# 扫描逻辑集中在 common/iam_scanner.py，这里只保留 route handler。


@app.post('/api/iam-scan')
async def trigger_iam_scan():
    """触发 IAM Bedrock 权限扫描（异步）。

    API Gateway 硬限制 29s，大型账号可能有几百个 IAM 身份，扫描耗时 30s+。
    方案：POST 立即返回 → 异步 invoke 自身 Lambda 执行扫描 → 前端轮询 GET 接口。
    """
    scan_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 立即标记"扫描中"
    put_item('IAM_SCAN', '_meta',
             scan_time=scan_time,
             status='scanning',
             total_identities='0',
             user_count='0',
             role_count='0',
             group_count='0')

    # 异步 invoke 自身 Lambda 执行实际扫描
    lambda_client = boto3.client('lambda')
    function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'bedrock-cost-guard-web')
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',  # 异步，立即返回
            Payload=json.dumps({'_iam_scan_task': True, 'scan_time': scan_time}),
        )
    except Exception as e:
        put_item('IAM_SCAN', '_meta',
                 scan_time=scan_time,
                 status='error',
                 error=str(e),
                 total_identities='0',
                 user_count='0', role_count='0', group_count='0')
        return JSONResponse({'error': f'触发扫描失败: {str(e)}'}, status_code=500)

    return {'ok': True, 'status': 'scanning', 'scan_time': scan_time}


@app.get('/api/iam-scan')
async def get_iam_scan():
    """获取上次 IAM 扫描结果。"""
    items = query_by_pk('IAM_SCAN')
    if not items:
        return {'scan_time': None, 'status': None, 'results': []}

    meta = None
    results = []
    for item in items:
        if item['SK'] == '_meta':
            meta = {k: v for k, v in item.items() if k not in ('PK', 'SK')}
        else:
            results.append({k: v for k, v in item.items() if k not in ('PK', 'SK')})

    # 按类型排序：User > Role > Group，同类型按名称
    type_order = {'User': 0, 'Role': 1, 'Group': 2}
    results.sort(key=lambda r: (type_order.get(r.get('identity_type'), 9), r.get('name', '')))

    status = meta.get('status', 'done') if meta else None

    return {
        'scan_time': meta.get('scan_time') if meta else None,
        'status': status,
        'error': meta.get('error') if meta else None,
        'total_identities': int(meta.get('total_identities', 0)) if meta else 0,
        'user_count': int(meta.get('user_count', 0)) if meta else 0,
        'role_count': int(meta.get('role_count', 0)) if meta else 0,
        'group_count': int(meta.get('group_count', 0)) if meta else 0,
        'unreadable_policies': meta.get('unreadable_policies', []) if meta else [],
        'results': results if status == 'done' else [],
    }


# ===== 回填 =====

@app.post('/api/backfill')
async def backfill(request: Request):
    data = await request.json()
    days = data.get('days', 0)

    if not isinstance(days, int) or days < 1 or days > 30:
        return JSONResponse({'error': 'days must be between 1 and 30'}, status_code=400)

    lambda_client = boto3.client('lambda')
    now = datetime.now(timezone.utc)
    triggered = []

    for i in range(days):
        target_date = (now - timedelta(days=i + 2)).strftime('%Y-%m-%d')  # 从 T-2 开始
        try:
            lambda_client.invoke(
                FunctionName=RECONCILER_FUNCTION_NAME,
                InvocationType='Event',
                Payload=json.dumps({'date': target_date, 'silent': True}),
            )
            triggered.append(target_date)
        except Exception as e:
            pass  # async fire-and-forget

    return {
        'total': days,
        'triggered': len(triggered),
        'message': f'已异步触发 {len(triggered)} 天对账，结果将陆续写入数据库',
    }


# ===== 版本信息 =====


@app.get('/api/version')
async def get_version_info():
    """返回版本信息：当前版本、堆栈名称、IP 白名单、最新版本。"""
    from common.version import VERSION

    # 环境变量优先，fallback 保证本地开发/测试可用
    stack_name = os.environ.get('STACK_NAME') or 'bedrock-cost-guard'
    upstream_owner = os.environ.get('GITHUB_OWNER', 'huizi-2003')
    upstream_repo = os.environ.get('GITHUB_REPO', 'sample-cost-guard-for-amazon-bedrock')
    upstream_branch = os.environ.get('GITHUB_BRANCH', 'main')

    result = {
        'current_version': VERSION,
        'latest_version': None,
        'has_update': None,
        'stack_name': stack_name,
        'allowed_cidrs': [],
        'last_updated': None,
    }

    # 查询 CloudFormation 栈信息（白名单、最后更新时间）
    try:
        cfn = boto3.client('cloudformation')
        resp = cfn.describe_stacks(StackName=stack_name)
        stacks = resp.get('Stacks', [])
        if stacks:
            stack = stacks[0]
            last_updated = stack.get('LastUpdatedTime') or stack.get('CreationTime')
            if last_updated:
                result['last_updated'] = last_updated.strftime('%Y-%m-%dT%H:%M:%SZ')

            params = {p['ParameterKey']: p['ParameterValue'] for p in stack.get('Parameters', [])}
            cidrs_str = params.get('AllowedCidrs', '')
            if cidrs_str:
                result['allowed_cidrs'] = [c.strip() for c in cidrs_str.split(',') if c.strip()]
    except Exception as e:
        logger.warning(f"Failed to describe CloudFormation stack: {e}")

    # 调用 GitHub 读取主仓库的 common/version.py 获取最新版本号
    try:
        url = f'https://raw.githubusercontent.com/{upstream_owner}/{upstream_repo}/{upstream_branch}/common/version.py'
        req = urllib.request.Request(url, headers={'User-Agent': 'bedrock-cost-guard'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode()
            match = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                result['latest_version'] = match.group(1)
                result['has_update'] = _compare_versions(VERSION, match.group(1))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.warning(f"Failed to fetch latest version from GitHub: {e}")

    return result


def _compare_versions(current: str, latest: str) -> bool:
    """比较版本号，返回 True 表示有更新可用。支持语义化版本（x.y.z）。
    非语义版本或解析失败时保守返回 False（拿不准就不提示更新）。"""
    try:
        def parse(v):
            return tuple(int(x) for x in v.split('.'))
        return parse(latest) > parse(current)
    except (ValueError, AttributeError):
        return False


# ===== Lambda handler (API Gateway) =====
from mangum import Mangum

_mangum_handler = Mangum(app)


def handler(event, context):
    """Lambda 入口：区分 API Gateway 事件和内部异步任务。"""
    # 异步 IAM 扫描任务（由 POST /api/iam-scan 触发）
    if event.get('_iam_scan_task'):
        return execute_iam_scan(event.get('scan_time', ''))

    # 正常 API Gateway 请求
    return _mangum_handler(event, context)


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('WEB_PORT', '80'))
    uvicorn.run(app, host='0.0.0.0', port=port)
