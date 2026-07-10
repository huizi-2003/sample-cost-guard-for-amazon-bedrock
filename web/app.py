import os
import re
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config as BotoConfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from common.config import (
    get_thresholds, get_regions, get_reconcile_by_date,
    get_reconcile_dates, put_item, get_item, query_by_pk,
    get_webhook_config, save_webhook_config
)

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
    if not full_path.startswith(os.path.realpath(STATIC_DIR)):
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


def _clean_label(label):
    """与 monitor/handler.py 的 clean_label 一致"""
    label = label.replace('AWS/Bedrock ', '').replace('AWS/BedrockMantle ', '')
    label = label.replace('global.anthropic.', '').replace('anthropic.', '')
    for suffix in (' CacheReadInputTokenCount', ' CacheWriteInputTokenCount',
                   ' InputTokenCount', ' OutputTokenCount',
                   ' TotalInputTokens', ' TotalOutputTokens', ' Tokens'):
        if label.endswith(suffix):
            label = label[:-len(suffix)]
            break
    return label.strip()


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
        label = _clean_label(r['Label'])
        if not label:
            continue
        for ts, val in zip(r['Timestamps'], r['Values']):
            if val > 0:
                ts_str = ts.astimezone(timezone.utc).strftime('%H:%M')
                models.setdefault(label, []).append((ts_str, val))
    return models


@app.get('/api/monitor/{date}/models')
async def monitor_models(date: str):
    """返回当日各模型的时间序列。优先从 DDB 读取缓存，无模型数据时 fallback 到实时 CW 查询。"""
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
            try:
                region_models = future.result()
                for model, points in region_models.items():
                    if model not in all_models:
                        all_models[model] = {}
                    for ts_str, val in points:
                        all_models[model][ts_str] = all_models[model].get(ts_str, 0) + val
            except Exception:
                pass

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


@app.get('/api/monitor/{date}/yesterday')
async def monitor_yesterday(date: str):
    """返回前一天的模型级监控数据，用于对比线展示。"""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return JSONResponse({'error': 'Invalid date format'}, status_code=400)

    try:
        parsed = datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return JSONResponse({'error': 'Invalid date'}, status_code=400)

    yesterday = (parsed - timedelta(days=1)).strftime('%Y-%m-%d')
    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if yesterday > utc_today:
        return {}

    # 复用 monitor_models 的逻辑
    return await monitor_models(yesterday)


# ===== 今日成本估算 =====

# $/MTok — Bedrock cross-region 基准价格（direct in-region ≈ ×1.1，差异忽略）
# cache_write 统一用 5min 标准价格
PRICING = {
    'opus':   {'input': 5,   'output': 25,  'cache_read': 0.5,  'cache_write': 6.25},
    'fable':  {'input': 10,  'output': 50,  'cache_read': 1.0,  'cache_write': 12.5},
    'sonnet': {'input': 3,   'output': 15,  'cache_read': 0.3,  'cache_write': 3.75},
    'haiku':  {'input': 1,   'output': 5,   'cache_read': 0.1,  'cache_write': 1.25},
}


def _match_pricing(model_name):
    """按模型名模糊匹配到价格系列，返回价格 dict 或 None。"""
    lower = model_name.lower()
    for series, prices in PRICING.items():
        if series in lower:
            return prices
    return None


@app.get('/api/today-cost')
async def today_cost():
    """从 DDB 读取今日监控数据，按价格常量计算估算费用。

    若 DDB 中无新格式（按类型拆分）数据，fallback 到实时查 CW。

    返回:
      - total_cost: 今日预估总费用 ($)
      - models: {model: {cost, input, output, cache_read, cache_write, tokens}}
      - timeline: [{time, cost}] 累计费用趋势
      - unpriced_models: 无法匹配价格的模型列表
    """
    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    items = query_by_pk(f'MONITOR#{utc_today}')

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
    """从 DDB 新格式 models 数据计算费用。"""
    model_totals = {}
    timeline_points = {}
    unpriced = set()

    for item in items:
        time_str = item['SK'].replace('T#', '')
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
    """Fallback: 实时从 CW 查按类型拆分的 token 数据，计算估算费用。"""
    utc_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    day_start = datetime.strptime(utc_today, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)

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
        resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=day_start, EndTime=end)
        results = []
        for r in resp['MetricDataResults']:
            label = r['Label']
            model_name = _extract_model_name(label)
            token_type = _extract_token_type(label)
            for ts, val in zip(r['Timestamps'], r['Values']):
                if val > 0:
                    ts_str = ts.astimezone(timezone.utc).strftime('%H:%M')
                    results.append((model_name, token_type, ts_str, val))
        return results

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_region_typed, r): r for r in regions}
        for future in as_completed(futures):
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
            except Exception:
                pass

    return _build_cost_response(model_totals, timeline_points, unpriced)


def _extract_model_name(label):
    """与 monitor/handler.py 的 extract_model_name 一致。"""
    label = label.replace('AWS/Bedrock ', '').replace('AWS/BedrockMantle ', '')
    label = label.replace('global.anthropic.', '').replace('anthropic.', '')
    for suffix in (' CacheReadInputTokenCount', ' CacheWriteInputTokenCount',
                   ' InputTokenCount', ' OutputTokenCount',
                   ' TotalInputTokens', ' TotalOutputTokens', ' Tokens'):
        if label.endswith(suffix):
            label = label[:-len(suffix)]
            break
    return label.strip()


def _extract_token_type(label):
    """与 monitor/handler.py 的 extract_token_type 一致。"""
    if 'CacheRead' in label or 'cacheread' in label.lower():
        return 'cache_read'
    if 'CacheWrite' in label or 'cachewrite' in label.lower():
        return 'cache_write'
    if 'Output' in label:
        return 'output'
    return 'input'


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

    # 累计费用趋势线
    timeline = []
    cumulative = 0
    for time_str in sorted(timeline_points.keys()):
        cumulative += timeline_points[time_str]
        timeline.append({'time': time_str, 'cost': round(cumulative, 6)})

    return {
        'total_cost': round(total_cost, 4),
        'models': model_costs,
        'timeline': timeline,
        'unpriced_models': sorted(unpriced),
    }


# ===== 配置管理 =====

@app.get('/api/config/regions')
async def get_config_regions():
    return get_regions()


@app.put('/api/config/regions')
async def put_config_regions(request: Request):
    body = await request.json()
    regions = body.get('regions', [])
    put_item('CONFIG', 'regions', value=','.join(regions))
    return {'ok': True}


@app.get('/api/config/thresholds')
async def get_config_thresholds():
    return get_thresholds()


@app.put('/api/config/thresholds')
async def put_config_thresholds(request: Request):
    data = await request.json()
    for key, val in data.items():
        put_item('THRESHOLD', key, value=int(val))
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
    # 校验每项必须有 url 和 type
    cleaned = []
    for item in items:
        url = item.get('url', '').strip()
        wh_type = item.get('type', 'feishu')
        name = item.get('name', wh_type).strip() or wh_type
        if url:  # 忽略空 URL 的条目
            cleaned.append({'name': name, 'url': url, 'type': wh_type})
    if len(cleaned) > 3:
        return JSONResponse({'error': '最多配置 3 个 Webhook 渠道'}, status_code=400)
    save_webhook_config(cleaned)
    return {'ok': True, 'count': len(cleaned)}


# ===== 回填 =====

@app.post('/api/backfill')
async def backfill(request: Request):
    data = await request.json()
    days = data.get('days', 0)

    if not isinstance(days, int) or days < 1 or days > 365:
        return JSONResponse({'error': 'days must be between 1 and 365'}, status_code=400)

    lambda_client = boto3.client('lambda')
    now = datetime.now(timezone.utc)
    triggered = []

    for i in range(days):
        target_date = (now - timedelta(days=i + 2)).strftime('%Y-%m-%d')  # 从 T-2 开始
        try:
            lambda_client.invoke(
                FunctionName=RECONCILER_FUNCTION_NAME,
                InvocationType='Event',
                Payload=json.dumps({'date': target_date}),
            )
            triggered.append(target_date)
        except Exception as e:
            pass  # async fire-and-forget

    return {
        'total': days,
        'triggered': len(triggered),
        'message': f'已异步触发 {len(triggered)} 天对账，结果将陆续写入数据库',
    }


# ===== Lambda handler (API Gateway) =====
from mangum import Mangum

handler = Mangum(app)

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('WEB_PORT', '80'))
    uvicorn.run(app, host='0.0.0.0', port=port)
