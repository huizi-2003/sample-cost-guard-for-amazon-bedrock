"""Bedrock Cost Guard — 每日对账 Lambda

对账逻辑：
  1. 从 Cost Explorer 获取 T-2 完整 UTC 日的 "Amazon Bedrock Service" 账单
     - 按 USAGE_TYPE 分组，获取每条的 UnblendedCost 和 UsageQuantity
     - 仅累加含 "token" 的条目到 ce_token_total（排除 searchunits 等非 token 项）
  2. 从 CloudWatch 跨所有配置 Region 查询同一 UTC 日的 token 总量
     - region 从 CE 账单的 USAGE_TYPE 前缀自动推导（账单里有哪些区域就查哪些）
     - AWS/Bedrock namespace: TokenCount (SEARCH 所有 ModelId)
     - AWS/BedrockMantle namespace: TotalInputTokens + TotalOutputTokens
  3. 对比两边 token 总量: diff% = (CE - CW) / CW × 100
  4. 按"模型身份"（含路由标记，如 cross-region-global / mantle）聚合费用明细，
     并按 5 种 token 类型分开累计 cost 与 token 量（input/output/cache_read/cache_write/cache_write_1h）
  5. 全部结果存入 DynamoDB，推送报告

为什么查 T-2：
  CE 的 DAILY 粒度按 UTC 日计，且账单数据 T+1 才完整。
  Lambda 在 UTC 01:00 跑时，T-2 已完整结束 25+ 小时，数据确保可用。

为什么用 UTC 日而非北京日：
  CE 的 TimePeriod 不可配时区，固定按 UTC 解释日期。
  CW 查询时间窗口必须与 CE 对齐，否则 diff% 失去意义。
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config
from common.config import save_reconcile_record, get_webhook_config, get_notify_policy
from common.holiday import is_workday
from common.webhook import send_webhook_all

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BEIJING_TZ = timezone(timedelta(hours=8))
API_TIMEOUT = Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 2})


def get_cost_explorer_data(start_date, end_date):
    """查询 Cost Explorer 获取 Bedrock 按 USAGE_TYPE 的每日费用和用量。

    注意 SERVICE 筛选的是 "Amazon Bedrock Service"（Claude 等模型），
    不是 "Amazon Bedrock"（Nova 等模型），两者是不同的 CE Service。
    """
    ce = boto3.client('ce', region_name='us-east-1')
    resp = ce.get_cost_and_usage(
        TimePeriod={'Start': start_date, 'End': end_date},
        Granularity='DAILY',
        Filter={'Dimensions': {'Key': 'SERVICE', 'Values': ['Amazon Bedrock Service']}},
        GroupBy=[{'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}],
        Metrics=['UnblendedCost', 'UsageQuantity'],
    )
    results = []
    for result in resp.get('ResultsByTime', []):
        for group in result.get('Groups', []):
            usage_type = group['Keys'][0]
            cost = float(group['Metrics']['UnblendedCost']['Amount'])
            qty_amount = float(group['Metrics']['UsageQuantity']['Amount'])
            qty_unit = group['Metrics']['UsageQuantity'].get('Unit', '')
            if cost > 0 or qty_amount > 0:
                results.append({
                    'usage_type': usage_type,
                    'cost': cost,
                    'quantity': qty_amount,
                    'unit': qty_unit,
                })
    return results


def fetch_cw_region_total(region, start, end):
    """查询单个 region 的 Bedrock token 总量（CloudWatch）。

    覆盖两个 namespace：
      - AWS/Bedrock: 通过 SEARCH 聚合所有 ModelId 的 TokenCount
      - AWS/BedrockMantle: TotalInputTokens + TotalOutputTokens
    Period 用 3600s（1小时），对整天聚合足够且避免数据点过多。
    """
    session = boto3.session.Session()
    cw = session.client('cloudwatch', region_name=region, config=API_TIMEOUT)

    queries = [
        {'Id': 'search_bedrock', 'Expression': "SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 3600)", 'ReturnData': False},
        {'Id': 'bedrock_total', 'Expression': 'SUM(search_bedrock)', 'ReturnData': True},
        {'Id': 'mantle_in', 'MetricStat': {'Metric': {'Namespace': 'AWS/BedrockMantle', 'MetricName': 'TotalInputTokens', 'Dimensions': []}, 'Period': 3600, 'Stat': 'Sum'}, 'ReturnData': False},
        {'Id': 'mantle_out', 'MetricStat': {'Metric': {'Namespace': 'AWS/BedrockMantle', 'MetricName': 'TotalOutputTokens', 'Dimensions': []}, 'Period': 3600, 'Stat': 'Sum'}, 'ReturnData': False},
        {'Id': 'mantle_total', 'Expression': 'FILL(mantle_in,0) + FILL(mantle_out,0)', 'ReturnData': True},
    ]

    resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    total = 0
    for r in resp['MetricDataResults']:
        total += sum(r['Values'])
    return total


def get_cloudwatch_token_total(regions, start, end):
    """跨所有 region 聚合前一天的 CloudWatch token 总量，返回总量和各 region 明细"""
    total = 0
    failed_regions = []
    region_details = {}  # region -> token count
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_cw_region_total, r, start, end): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                count = future.result()
                total += count
                region_details[region] = count
            except Exception as e:
                logger.warning(f"CW query failed for {region}: {e}")
                failed_regions.append(region)
    return total, failed_regions, region_details


# Token 计量段（Token_Type_Segment）：从 USAGE_TYPE 中原地剔除，剩余部分即模型身份。
# 按长度降序排列，确保长段优先匹配（例如 -cache-read-input-token-count
# 不会被更短的 -input-token-count 先切走）。
TOKEN_TYPE_SEGMENTS = sorted(
    (
        '-cache-read-input-token-count',
        '-cache-write-1h-input-token-count',
        '-cache-write-input-token-count',
        '-cache-read-tokens',
        '-cache-write-1h-tokens',
        '-cache-write-tokens',
        '-cacheread-tokens',
        '-cachewrite-tokens',
        '-input-token-count',
        '-output-token-count',
        '-input-tokens',
        '-output-tokens',
    ),
    key=len,
    reverse=True,
)


def extract_model_identity(usage_type):
    """从 USAGE_TYPE 提取"模型身份"（Model_Identity）。

    模型身份包含路由标记（cross-region-global / mantle 等，AWS 据此差异定价），
    但剔除 region 前缀、token 计量段、以及不影响 on-demand 定价的 -standard 层级后缀。

    格式示例:
      USE1-Claude4.6Opus-input-tokens                                    → claude4.6opus
      USE1-Claude4.6Opus-input-tokens-cross-region-global                → claude4.6opus-cross-region-global
      USE1-Claude4.6Opus-cache-read-input-token-count-cross-region-global→ claude4.6opus-cross-region-global
      USW2-anthropic.claude-opus-4-8-mantle-input-tokens-global-standard → anthropic.claude-opus-4-8-mantle-global

    关键：token 段是"原地剔除"而非"当作尾缀删掉"，因此段之后的路由标记会被保留，
    这正是旧实现（把 -input-tokens-cross-region-global 整段当尾缀）丢失路由的原因。
    """
    s = usage_type.lower()

    # 1. 去掉 region 前缀（第一个 '-' 之前的段，如 USE1-、USW2-、EUW1-）
    dash_idx = s.find('-')
    if dash_idx > 0:
        s = s[dash_idx + 1:]

    # 2. 去掉层级后缀 -standard（仅当出现在末尾时，不是价格维度）
    if s.endswith('-standard'):
        s = s[:-len('-standard')]

    # 3. 原地剔除 token 计量段（长段优先），拼回前后两截
    for seg in TOKEN_TYPE_SEGMENTS:
        idx = s.find(seg)
        if idx >= 0:
            s = s[:idx] + s[idx + len(seg):]
            break

    # 4. 清理剔除后可能出现的重复/首尾连字符
    while '--' in s:
        s = s.replace('--', '-')
    return s.strip('-')


def get_token_type(usage_type):
    """从 USAGE_TYPE 判断 token 类型（5 种）。

    cache-write 区分 5min 和 1h：CE 中 1h 变体含 'cache-write-1h' 标识。
    cache-read 不区分时长（官方定价一致）。
    """
    lower = usage_type.lower()
    if 'cache-read' in lower or 'cacheread' in lower:
        return 'cache_read'
    elif ('cache-write' in lower or 'cachewrite' in lower) and '1h' in lower:
        return 'cache_write_1h'
    elif 'cache-write' in lower or 'cachewrite' in lower:
        return 'cache_write'
    elif 'output' in lower:
        return 'output'
    else:
        return 'input'


# Token_Type → (cost 字段, token 量字段)。每个 CE line item 恰好命中一个 token_type，
# 其 cost/quantity 只累加到对应字段，保证费用不重不漏（Req 2）。
TOKEN_TYPE_FIELDS = {
    'input':          ('cost_input',          'tokens_input_1k'),
    'output':         ('cost_output',         'tokens_output_1k'),
    'cache_read':     ('cost_cache_read',     'tokens_cache_read_1k'),
    'cache_write':    ('cost_cache_write',    'tokens_cache_write_1k'),
    'cache_write_1h': ('cost_cache_write_1h', 'tokens_cache_write_1h_1k'),
}


# CE USAGE_TYPE 区域前缀 → AWS region 映射
# CE 用 4 字母缩写代码作为 usage_type 前缀（如 USE1-xxx）
USAGE_PREFIX_TO_REGION = {
    'USE1': 'us-east-1', 'USE2': 'us-east-2',
    'USW1': 'us-west-1', 'USW2': 'us-west-2',
    'UGW1': 'us-gov-west-1', 'UGE1': 'us-gov-east-1',
    'CAN1': 'ca-central-1',
    'EU': 'eu-west-1', 'EUW1': 'eu-west-1', 'EUW2': 'eu-west-2', 'EUW3': 'eu-west-3',
    'EUC1': 'eu-central-1', 'EUC2': 'eu-central-2',
    'EUN1': 'eu-north-1', 'EUS1': 'eu-south-1', 'EUS2': 'eu-south-2',
    'APN1': 'ap-northeast-1', 'APN2': 'ap-northeast-2', 'APN3': 'ap-northeast-3',
    'APS1': 'ap-southeast-1', 'APS2': 'ap-southeast-2', 'APS3': 'ap-southeast-3',
    'APS4': 'ap-southeast-4', 'APS5': 'ap-southeast-5',
    'APE1': 'ap-east-1', 'API1': 'ap-south-1', 'API2': 'ap-south-2',
    'SAE1': 'sa-east-1',
    'MES1': 'me-south-1', 'MEC1': 'me-central-1',
    'AFS1': 'af-south-1',
}


def extract_region_from_usage_type(usage_type):
    """从 USAGE_TYPE 前缀提取 AWS region。
    前缀是第一个 '-' 之前的部分（如 USE1-Claude... → USE1 → us-east-1）。
    无法识别的前缀返回 None。
    """
    dash_idx = usage_type.find('-')
    prefix = usage_type[:dash_idx] if dash_idx > 0 else usage_type
    return USAGE_PREFIX_TO_REGION.get(prefix.upper())


def get_regions_from_ce(ce_results):
    """从 CE 账单数据中推导出涉及的 region 列表（去重）"""
    regions = set()
    for item in ce_results:
        region = extract_region_from_usage_type(item['usage_type'])
        if region:
            regions.add(region)
    return sorted(regions)


def is_token_usage(usage_type):
    """判断 USAGE_TYPE 是否为 token 类用量（用于对账）。
    token 类包含 'tokens' 或 'token-count'，排除 searchunits 等。
    """
    lower = usage_type.lower()
    return 'token' in lower and 'searchunits' not in lower


def reconcile_one(start_date, end_date, now):
    """对单个日期执行对账并写入 DDB，返回报告正文（不发送 webhook）。

    成功返回 {'msg', 'total_actual', 'reconcile_diff_pct'}；
    CE 查询失败返回 {'ce_error': <str>}。
    """
    # CW 时间窗口对齐到同一个 UTC 日
    cw_start = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    cw_end = datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)

    # 1. Cost Explorer 真实费用 + 用量
    try:
        ce_results = get_cost_explorer_data(start_date, end_date)
    except Exception as e:
        logger.error(f"Cost Explorer query failed for {start_date}: {e}")
        return {'ce_error': str(e)}

    # 2. CloudWatch token 总量（跨 region）
    # region 从 CE 账单的 USAGE_TYPE 前缀自动推导（账单里有哪些区域就查哪些）
    # 无需手动配置 region
    regions = get_regions_from_ce(ce_results)
    cw_total = 0
    cw_failed_regions = []
    cw_region_details = {}
    if regions:
        cw_total, cw_failed_regions, cw_region_details = get_cloudwatch_token_total(regions, cw_start, cw_end)

    # 3. 按"模型身份"聚合 CE 数据，费用/用量按 5 种 token 类型分开累计
    model_details = {}
    ce_token_total = 0  # CE 侧 token 总量（单位：个），仅计 token 类用量

    for item in ce_results:
        usage_type = item['usage_type']
        cost = item['cost']
        quantity = item['quantity']  # 单位：1K tokens（token 类）或其他单位（非 token 类）
        model = extract_model_identity(usage_type)
        token_type = get_token_type(usage_type)

        # 只有 token 类用量才纳入对账
        if is_token_usage(usage_type):
            ce_token_total += quantity * 1000

        if model not in model_details:
            model_details[model] = {
                'cost_input': 0, 'cost_output': 0,
                'cost_cache_read': 0, 'cost_cache_write': 0,
                'cost_cache_write_1h': 0,
                'tokens_input_1k': 0, 'tokens_output_1k': 0,
                'tokens_cache_read_1k': 0, 'tokens_cache_write_1k': 0,
                'tokens_cache_write_1h_1k': 0,
            }

        # 每个 line item 恰好命中一个 token_type，cost/quantity 只落入对应桶
        cost_field, tokens_field = TOKEN_TYPE_FIELDS[token_type]
        model_details[model][cost_field] += cost
        model_details[model][tokens_field] += quantity

    # actual_cost = 该模型 5 个 token 类型 cost 之和
    for detail in model_details.values():
        detail['actual_cost'] = (
            detail['cost_input'] + detail['cost_output']
            + detail['cost_cache_read'] + detail['cost_cache_write']
            + detail['cost_cache_write_1h']
        )

    # 4. 对账：CE token 总量 vs CloudWatch token 总量
    # 公式: diff% = (CE - CW) / CW × 100
    # 预期 diff ≈ 0%，差异大说明某侧数据有缺失
    reconcile_diff_pct = None
    if ce_token_total > 0 and cw_total > 0:
        reconcile_diff_pct = (ce_token_total - cw_total) / cw_total * 100

    # 5. 存入 DDB
    total_actual = sum(d['actual_cost'] for d in model_details.values())

    for model, detail in model_details.items():
        if detail['actual_cost'] < 0.01:
            continue
        record = {
            'actual_cost': str(round(detail['actual_cost'], 4)),
            'cost_input': str(round(detail['cost_input'], 4)),
            'cost_output': str(round(detail['cost_output'], 4)),
            'cost_cache_read': str(round(detail['cost_cache_read'], 4)),
            'cost_cache_write': str(round(detail['cost_cache_write'], 4)),
            'cost_cache_write_1h': str(round(detail['cost_cache_write_1h'], 4)),
            'tokens_input_1k': str(round(detail['tokens_input_1k'], 3)),
            'tokens_output_1k': str(round(detail['tokens_output_1k'], 3)),
            'tokens_cache_read_1k': str(round(detail['tokens_cache_read_1k'], 3)),
            'tokens_cache_write_1k': str(round(detail['tokens_cache_write_1k'], 3)),
            'tokens_cache_write_1h_1k': str(round(detail['tokens_cache_write_1h_1k'], 3)),
        }
        save_reconcile_record(start_date, model, record)

    summary = {
        'total_actual': str(round(total_actual, 4)),
        'model_count': str(len([m for m in model_details if model_details[m]['actual_cost'] >= 0.01])),
        'ce_token_total': str(round(ce_token_total)),
        'cw_token_total': str(round(cw_total)),
    }
    if reconcile_diff_pct is not None:
        summary['reconcile_diff_pct'] = str(round(reconcile_diff_pct, 2))
    save_reconcile_record(start_date, '_summary', summary)

    # 存 CE 原始明细
    ce_detail_records = []
    for item in ce_results:
        ce_detail_records.append({
            'usage_type': item['usage_type'],
            'cost': str(round(item['cost'], 6)),
            'quantity': str(round(item['quantity'], 3)),
            'unit': item.get('unit', ''),
        })
    save_reconcile_record(start_date, '_ce_detail', {'data': json.dumps(ce_detail_records)})

    # 存 CW 各 region 明细
    cw_detail = {r: str(round(v)) for r, v in cw_region_details.items()}
    if cw_failed_regions:
        cw_detail['_failed'] = ','.join(cw_failed_regions)
    save_reconcile_record(start_date, '_cw_detail', {'data': json.dumps(cw_detail)})

    # 7. 构造报告正文（不含顶部标题，由调用方决定）
    msg = ""

    # 对账结果
    msg += f"--- Token 对账（账单 vs 监控）---\n"
    msg += f"  账单 Token 总量: {ce_token_total:,.0f}\n"
    msg += f"  监控 Token 总量: {cw_total:,.0f}\n"
    if reconcile_diff_pct is not None:
        msg += f"  差异: {reconcile_diff_pct:+.2f}%\n"
    elif ce_token_total == 0 and cw_total == 0:
        msg += f"  无 token 用量\n"
    else:
        msg += f"  差异: 无法计算（一侧为 0）\n"
    if cw_failed_regions:
        msg += f"  ⚠ 监控查询失败的 Region: {', '.join(cw_failed_regions)}\n"

    # 费用汇总
    msg += f"\n--- 费用汇总 ---\n"
    msg += f"  实际总费用: ${total_actual:.2f}\n"

    # 按模型小计（每个模型身份仅一行，明细留给 Web 控制台）
    msg += f"\n--- 各模型明细 ---\n"
    shown = False
    for model in sorted(model_details.keys(), key=lambda m: model_details[m]['actual_cost'], reverse=True):
        detail = model_details[model]
        if detail['actual_cost'] < 0.01:
            continue
        msg += f"  {model}: ${detail['actual_cost']:.2f}\n"
        shown = True
    if not shown:
        msg += "  未发现 Bedrock 用量\n"

    return {'msg': msg, 'total_actual': total_actual, 'reconcile_diff_pct': reconcile_diff_pct}


def handler(event, context):
    now = datetime.now(timezone.utc)
    webhooks = get_webhook_config()

    override_date = event.get('date')
    if override_date:
        # 回填 / 重跑指定日期（单日，单独推送）
        try:
            parsed = datetime.strptime(override_date, '%Y-%m-%d')
        except ValueError:
            return {'statusCode': 400, 'error': f'Invalid date format: {override_date}, expected YYYY-MM-DD'}
        if parsed.date() >= now.date():
            return {'statusCode': 400, 'error': f'Date must be before today: {override_date}'}
        start_date = override_date
        end_date = (parsed + timedelta(days=1)).strftime('%Y-%m-%d')
        r = reconcile_one(start_date, end_date, now)
        if r.get('ce_error'):
            send_webhook_all(f"[Bedrock 对账] Cost Explorer 查询失败: {r['ce_error']}", webhooks)
            return {'statusCode': 500, 'error': 'ce_failed'}
        send_webhook_all(f"[Bedrock 日报] {start_date}\n\n{r['msg']}", webhooks)
        logger.info(r['msg'])
        return {'statusCode': 200, 'date': start_date, 'total_actual': r['total_actual'], 'reconcile_diff_pct': r['reconcile_diff_pct']}

    # 默认每日运行：同时对账 T-2（已结算）和 T-1（临时，账单可能未结算完）。
    # 每个日期会被跑两次：次日先以 T-1 跑出临时值，后天再以 T-2 跑出最终值覆盖，
    # 既保证第二天就有数据可看，又保证最终被修正为准确值。一条合并报告推送。
    jobs = [
        ('T-2 (已结算)', (now - timedelta(days=2)).strftime('%Y-%m-%d'), (now - timedelta(days=1)).strftime('%Y-%m-%d')),
        ('T-1 (临时·账单可能未结算完)', (now - timedelta(days=1)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')),
    ]

    combined = "[Bedrock 日报]\n"
    dates = []
    for label, s, e in jobs:
        dates.append(s)
        combined += f"\n========== {label}  {s} ==========\n"
        r = reconcile_one(s, e, now)
        if r.get('ce_error'):
            combined += f"  ⚠ Cost Explorer 查询失败: {r['ce_error']}\n"
        else:
            combined += r['msg']

    # 推送策略判断：workday 模式下非工作日跳过推送（对账照常执行，数据不丢）
    notify_policy = get_notify_policy()
    beijing_now = now.astimezone(BEIJING_TZ)
    should_notify = True

    if notify_policy == 'workday':
        should_notify = is_workday(beijing_now.date())
        if not should_notify:
            logger.info(f"Notify policy is 'workday' and today ({beijing_now.strftime('%Y-%m-%d')}) is not a workday, skipping notification")

    if should_notify:
        send_webhook_all(combined, webhooks)

    logger.info(combined)
    return {'statusCode': 200, 'dates': dates}
