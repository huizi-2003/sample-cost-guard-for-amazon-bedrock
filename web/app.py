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
    get_cost_thresholds, get_regions, get_reconcile_by_date,
    get_reconcile_dates, put_item, get_item, query_by_pk,
    get_webhook_config, save_webhook_config,
    get_notify_policy, save_notify_policy
)
from common.pricing import PRICING, match_pricing as _match_pricing

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




# ===== 今日成本估算 =====
# 价目表与匹配逻辑集中在 common/pricing.py（monitor / web 共用同一份）


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


@app.get('/api/config/notify-policy')
async def get_config_notify_policy():
    """获取日报推送策略"""
    return {'policy': get_notify_policy()}


@app.put('/api/config/notify-policy')
async def put_config_notify_policy(request: Request):
    """设置日报推送策略：always（每天）或 workday（仅工作日）"""
    data = await request.json()
    policy = data.get('policy', '').strip()
    if policy not in ('always', 'workday'):
        return JSONResponse({'error': "policy must be 'always' or 'workday'"}, status_code=400)
    save_notify_policy(policy)
    return {'ok': True, 'policy': policy}


# ===== IAM Bedrock 权限扫描 =====

def _extract_bedrock_actions(policy_doc):
    """从策略文档提取 Bedrock 相关 Action。返回 set of action strings。"""
    actions = set()
    if not isinstance(policy_doc, dict):
        return actions
    for stmt in policy_doc.get('Statement', []):
        if stmt.get('Effect') != 'Allow':
            continue
        stmt_actions = stmt.get('Action', [])
        if isinstance(stmt_actions, str):
            stmt_actions = [stmt_actions]
        for action in stmt_actions:
            lower = action.lower()
            # 匹配 bedrock:* 或 bedrock:InvokeModel 等，也匹配 *（全权限）
            if lower.startswith('bedrock:') or lower == '*':
                actions.add(action)
    return actions


def _scan_iam_identities():
    """扫描所有 IAM Users/Roles/Groups，找出有 Bedrock 权限的身份。"""
    iam = boto3.client('iam')
    results = []

    # --- 扫描 Users ---
    paginator = iam.get_paginator('list_users')
    for page in paginator.paginate():
        for user in page['Users']:
            user_name = user['UserName']
            bedrock_actions = set()
            policy_sources = []

            # 用户附加的托管策略
            attached = iam.list_attached_user_policies(UserName=user_name)['AttachedPolicies']
            for p in attached:
                actions = _check_managed_policy(iam, p['PolicyArn'])
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

            # 用户内联策略
            inline_names = iam.list_user_policies(UserName=user_name)['PolicyNames']
            for pname in inline_names:
                doc = iam.get_user_policy(UserName=user_name, PolicyName=pname)['PolicyDocument']
                actions = _extract_bedrock_actions(doc)
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': pname, 'type': 'inline'})

            # 用户所属 Group 的策略
            groups = iam.list_groups_for_user(UserName=user_name)['Groups']
            for g in groups:
                g_actions, g_policies = _check_group_policies(iam, g['GroupName'])
                if g_actions:
                    bedrock_actions.update(g_actions)
                    for gp in g_policies:
                        gp['via_group'] = g['GroupName']
                    policy_sources.extend(g_policies)

            if bedrock_actions:
                results.append({
                    'identity_type': 'User',
                    'name': user_name,
                    'arn': user['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'create_date': user['CreateDate'].isoformat(),
                })

    # --- 扫描 Roles ---
    paginator = iam.get_paginator('list_roles')
    for page in paginator.paginate():
        for role in page['Roles']:
            role_name = role['RoleName']
            # 跳过 AWS Service-Linked Roles
            if role.get('Path', '').startswith('/aws-service-role/'):
                continue

            bedrock_actions = set()
            policy_sources = []

            # 角色附加的托管策略
            attached = iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']
            for p in attached:
                actions = _check_managed_policy(iam, p['PolicyArn'])
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

            # 角色内联策略
            inline_names = iam.list_role_policies(RoleName=role_name)['PolicyNames']
            for pname in inline_names:
                doc = iam.get_role_policy(RoleName=role_name, PolicyName=pname)['PolicyDocument']
                actions = _extract_bedrock_actions(doc)
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': pname, 'type': 'inline'})

            if bedrock_actions:
                # 提取信任关系（谁能 assume 这个 role）
                trust = role.get('AssumeRolePolicyDocument', {})
                trust_principals = []
                for stmt in trust.get('Statement', []):
                    if stmt.get('Effect') == 'Allow':
                        principal = stmt.get('Principal', {})
                        if isinstance(principal, str):
                            trust_principals.append(principal)
                        else:
                            for k, v in principal.items():
                                if isinstance(v, list):
                                    trust_principals.extend(v)
                                else:
                                    trust_principals.append(v)

                results.append({
                    'identity_type': 'Role',
                    'name': role_name,
                    'arn': role['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'trust_principals': trust_principals,
                    'create_date': role['CreateDate'].isoformat(),
                })

    # --- 扫描 Groups（独立列出有 Bedrock 权限的组）---
    paginator = iam.get_paginator('list_groups')
    for page in paginator.paginate():
        for group in page['Groups']:
            group_name = group['GroupName']
            bedrock_actions, policy_sources = _check_group_policies(iam, group_name)
            if bedrock_actions:
                # 获取组成员
                members = [u['UserName'] for u in iam.get_group(GroupName=group_name)['Users']]
                results.append({
                    'identity_type': 'Group',
                    'name': group_name,
                    'arn': group['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'members': members,
                    'create_date': group['CreateDate'].isoformat(),
                })

    return results


def _check_managed_policy(iam, policy_arn):
    """检查托管策略是否包含 Bedrock 权限。返回 actions set。"""
    try:
        policy = iam.get_policy(PolicyArn=policy_arn)['Policy']
        version_id = policy['DefaultVersionId']
        doc = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)['PolicyVersion']['Document']
        return _extract_bedrock_actions(doc)
    except Exception:
        return set()


def _check_group_policies(iam, group_name):
    """检查组的所有策略，返回 (actions_set, policy_sources_list)。"""
    bedrock_actions = set()
    policy_sources = []

    # 组附加的托管策略
    attached = iam.list_attached_group_policies(GroupName=group_name)['AttachedPolicies']
    for p in attached:
        actions = _check_managed_policy(iam, p['PolicyArn'])
        if actions:
            bedrock_actions.update(actions)
            policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

    # 组内联策略
    inline_names = iam.list_group_policies(GroupName=group_name)['PolicyNames']
    for pname in inline_names:
        doc = iam.get_group_policy(GroupName=group_name, PolicyName=pname)['PolicyDocument']
        actions = _extract_bedrock_actions(doc)
        if actions:
            bedrock_actions.update(actions)
            policy_sources.append({'name': pname, 'type': 'inline'})

    return bedrock_actions, policy_sources


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

_mangum_handler = Mangum(app)


def handler(event, context):
    """Lambda 入口：区分 API Gateway 事件和内部异步任务。"""
    # 异步 IAM 扫描任务（由 POST /api/iam-scan 触发）
    if event.get('_iam_scan_task'):
        return _execute_iam_scan(event.get('scan_time', ''))

    # 正常 API Gateway 请求
    return _mangum_handler(event, context)


def _execute_iam_scan(scan_time):
    """实际执行 IAM 扫描（异步 invoke 时调用，无超时限制）。"""
    try:
        results = _scan_iam_identities()

        # 先删除旧的扫描结果
        old_items = query_by_pk('IAM_SCAN')
        from common.config import _get_table
        table = _get_table()
        for item in old_items:
            if item['SK'] != '_meta':
                table.delete_item(Key={'PK': item['PK'], 'SK': item['SK']})

        # 写入新结果
        user_count = sum(1 for r in results if r['identity_type'] == 'User')
        role_count = sum(1 for r in results if r['identity_type'] == 'Role')
        group_count = sum(1 for r in results if r['identity_type'] == 'Group')

        for r in results:
            sk = f"{r['identity_type'].lower()}/{r['name']}"
            put_item('IAM_SCAN', sk,
                     identity_type=r['identity_type'],
                     name=r['name'],
                     arn=r['arn'],
                     actions=r['actions'],
                     policies=r['policies'],
                     trust_principals=r.get('trust_principals'),
                     members=r.get('members'),
                     create_date=r.get('create_date'))

        # 更新 _meta 为完成
        put_item('IAM_SCAN', '_meta',
                 scan_time=scan_time,
                 status='done',
                 total_identities=str(len(results)),
                 user_count=str(user_count),
                 role_count=str(role_count),
                 group_count=str(group_count))

        return {'statusCode': 200, 'total': len(results)}
    except Exception as e:
        put_item('IAM_SCAN', '_meta',
                 scan_time=scan_time,
                 status='error',
                 error=str(e),
                 total_identities='0',
                 user_count='0', role_count='0', group_count='0')
        return {'statusCode': 500, 'error': str(e)}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('WEB_PORT', '80'))
    uvicorn.run(app, host='0.0.0.0', port=port)
