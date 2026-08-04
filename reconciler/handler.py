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
import os
import re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config
from common.config import save_reconcile_record, get_webhook_config, get_notify_policy, get_account_id, query_by_pk, _get_table, get_ai_summary_config, get_reconcile_dates, get_reconcile_by_date
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
    results = []
    next_token = None
    # CE 在分组结果超过一页时返回 NextPageToken，必须循环取完，
    # 否则大账号（模型×区域×token类型 组合数多）只会读到第一页，费用被低估。
    while True:
        kwargs = {
            'TimePeriod': {'Start': start_date, 'End': end_date},
            'Granularity': 'DAILY',
            'Filter': {'Dimensions': {'Key': 'SERVICE', 'Values': ['Amazon Bedrock Service']}},
            'GroupBy': [{'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}],
            'Metrics': ['UnblendedCost', 'UsageQuantity'],
        }
        if next_token:
            kwargs['NextPageToken'] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
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
        next_token = resp.get('NextPageToken')
        if not next_token:
            break
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
# 参考: https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-region-billing-codes.html
USAGE_PREFIX_TO_REGION = {
    # North America
    'USE1': 'us-east-1', 'USE2': 'us-east-2',
    'USW1': 'us-west-1', 'USW2': 'us-west-2',
    'UGW1': 'us-gov-west-1', 'UGE1': 'us-gov-east-1',
    'CAN1': 'ca-central-1', 'CAN2': 'ca-west-1',
    'MXC1': 'mx-central-1',
    # Europe
    'EU': 'eu-west-1', 'EUW1': 'eu-west-1', 'EUW2': 'eu-west-2', 'EUW3': 'eu-west-3',
    'EUC1': 'eu-central-1', 'EUC2': 'eu-central-2',
    'EUN1': 'eu-north-1', 'EUS1': 'eu-south-1', 'EUS2': 'eu-south-2',
    # Asia Pacific
    'APN1': 'ap-northeast-1', 'APN2': 'ap-northeast-2', 'APN3': 'ap-northeast-3',
    'APS1': 'ap-southeast-1', 'APS2': 'ap-southeast-2',
    'APS3': 'ap-south-1',       # Mumbai
    'APS4': 'ap-southeast-3',   # Jakarta
    'APS5': 'ap-south-2',       # Hyderabad
    'APS6': 'ap-southeast-4',   # Melbourne
    'APS7': 'ap-southeast-5',   # Malaysia
    'APS8': 'ap-southeast-6',   # New Zealand
    'APS9': 'ap-southeast-7',   # Thailand
    'APE1': 'ap-east-1', 'APE2': 'ap-east-2',
    # South America
    'SAE1': 'sa-east-1',
    # Middle East
    'MES1': 'me-south-1', 'MEC1': 'me-central-1',
    'ILC1': 'il-central-1',
    # Africa
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
        if is_token_usage(usage_type):
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
    # 有 region 查询失败时 cw_total 不完整，此时算出的 diff% 会假性偏正，
    # 误导为"监控丢数据"，故不计算，交由下方报告标注缺失。
    reconcile_diff_pct = None
    if ce_token_total > 0 and cw_total > 0 and not cw_failed_regions:
        reconcile_diff_pct = (ce_token_total - cw_total) / cw_total * 100

    # 5. 存入 DDB
    total_actual = sum(d['actual_cost'] for d in model_details.values())

    # 幂等：同一日期会被对账多次（T-1 临时 / T-2 最终 / backfill）。
    # save_reconcile_record 是 upsert，若某模型在旧一轮写入、这一轮却因跌破 0.01
    # 或身份串变化而不再写，旧的 per-model SK 会残留，被 web 月度累加重复计数。
    # 故写新记录前先删掉该日期所有 per-model 记录（保留 _summary/_ce_detail/_cw_detail，
    # 它们随后会被覆盖）。此处已在 CE 查询成功之后，不会误删好数据。
    old_items = query_by_pk(f'RECONCILE#{start_date}')
    table = _get_table()
    for it in old_items:
        if not it['SK'].startswith('_'):
            table.delete_item(Key={'PK': it['PK'], 'SK': it['SK']})

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
    elif cw_failed_regions:
        msg += f"  差异: 无法计算（{len(cw_failed_regions)} 个 Region 数据缺失）\n"
    else:
        msg += f"  差异: 无法计算（一侧为 0）\n"
    if cw_failed_regions:
        msg += f"  ⚠ 监控查询失败的 Region: {', '.join(cw_failed_regions)}\n"
    # 口径说明：CW 侧 SEARCH 覆盖全部 Bedrock 模型，CE 侧仅 "Amazon Bedrock Service"（Claude 等）。
    # 账号若有 Nova/Titan 等非 Claude 用量，监控总量会高于账单总量，diff 偏负属正常。
    msg += f"  注：监控含全部 Bedrock 模型，账单仅 Claude 系；有非 Claude 用量时差异偏负属正常\n"

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

    return {
        'msg': msg,
        'total_actual': total_actual,
        'reconcile_diff_pct': reconcile_diff_pct,
        # 供本轮月汇总直接覆盖刚写入日期，避免 DDB 最终一致读取短暂漏计。
        'model_costs': {
            model: detail['actual_cost']
            for model, detail in model_details.items()
            if detail['actual_cost'] >= 0.01
        },
    }


def _get_month_summary(reference_date, overrides=None, exclude=None):
    """读取报告日期所在月的数据，并用本轮结果覆盖刚写入日期。

    exclude 用于剔除未结算日期（T-1）。CE 的 DAILY 数据 T+1 才完整，把 T-1 的
    0 或残值计进来会拉低日均（例：8/1 $49.23 + 8/2 $15.87 两天日均 $32.55，
    混进 8/3 的 $0 就变成 $21.70），AI 会据此得出"已低于本月日均"的错误结论。
    """
    try:
        if isinstance(reference_date, str):
            month_prefix = reference_date[:7]
        else:
            month_prefix = reference_date.strftime('%Y-%m')
        excluded = set(exclude or ())
        # RECONCILE 记录 90 天 TTL，取满整个保留窗口再按月份筛选，
        # 避免默认小 limit 把目标月较早日期截断，导致月累计偏低。
        dates = sorted(
            date for date in get_reconcile_dates(limit=400)
            if date.startswith(month_prefix + '-') and date not in excluded
        )

        daily_costs = {}
        daily_model_costs = {}
        for date in dates:
            records = get_reconcile_by_date(date)
            models = {
                model: float(record.get('actual_cost', 0))
                for model, record in records.items()
                if not model.startswith('_')
            }
            model_total = sum(models.values())
            summary_total = records.get('_summary', {}).get('total_actual')
            daily_costs[date] = float(summary_total) if summary_total is not None else model_total
            daily_model_costs[date] = models

        # Query/Scan 默认最终一致；当前 T-2/T-1 刚写完时可能暂不可见。
        # 本轮结果来自同一次 CE 计算，直接覆盖这些日期才能保证手机金额一致。
        for date, result in (overrides or {}).items():
            if date in excluded or not date.startswith(month_prefix + '-') or result.get('ce_error'):
                continue
            if date not in dates:
                dates.append(date)
            daily_costs[date] = float(result.get('total_actual') or 0)
            daily_model_costs[date] = {
                model: float(cost)
                for model, cost in result.get('model_costs', {}).items()
            }

        dates.sort()
        if not dates:
            return None

        model_costs = {}
        for models in daily_model_costs.values():
            for model, cost in models.items():
                model_costs[model] = model_costs.get(model, 0.0) + cost

        total_cost = sum(daily_costs.values())
        top_model = max(model_costs.items(), key=lambda item: item[1]) if model_costs else None
        return {
            'dates': dates,
            'daily_costs': daily_costs,
            'model_costs': model_costs,
            'total_cost': total_cost,
            'top_model': top_model,
        }
    except Exception as e:
        logger.warning(f"Failed to build monthly summary: {e}")
        return None


def _build_month_context(now, month_summary=None):
    """把本月确定性汇总转换成 AgentCore 使用的上下文。"""
    # 兜底自建汇总时同样剔除未结算的 T-1：它的 $0 会出现在"每日费用列表"里，
    # 并多占一天日均分母，AI 据此会得出"费用下降"的错误结论。
    summary = month_summary or _get_month_summary(
        now, exclude={(now - timedelta(days=1)).strftime('%Y-%m-%d')}
    )
    if not summary or not summary['dates']:
        return None

    dates = summary['dates']
    daily_costs = summary['daily_costs']
    model_costs = summary['model_costs']
    average_cost = summary['total_cost'] / len(dates)
    top_models = sorted(model_costs.items(), key=lambda item: item[1], reverse=True)[:5]

    lines = [
        f"--- 本月累计数据（{dates[0]} 至 {dates[-1]}）---",
        f"累计费用: ${summary['total_cost']:.2f}",
        f"日均费用: ${average_cost:.2f}（{len(dates)} 天）",
        "Top 5 模型:",
    ]
    lines.extend(f"  {model}: ${cost:.2f}" for model, cost in top_models)
    lines.append("每日费用列表:")
    lines.extend(f"  {date}: ${cost:.2f}" for date, cost in daily_costs.items())
    return '\n'.join(lines)


def _format_model_name(model):
    """把账单内部模型 ID 压缩成适合手机阅读的名称。"""
    value = model.lower()
    families = (('opus', 'Opus'), ('sonnet', 'Sonnet'), ('haiku', 'Haiku'))
    for family, title in families:
        match = re.search(rf'claude[-_.]?{family}[-_.]?(\d+)(?:[-_.](\d+))?', value)
        if match:
            version = match.group(1)
            if match.group(2):
                version += f".{match.group(2)}"
            return f"Claude {title} {version}"
        match = re.search(rf'claude(\d+)[.-](\d+){family}', value)
        if match:
            return f"Claude {title} {match.group(1)}.{match.group(2)}"

    value = re.sub(r'^(?:global\.|us\.|eu\.)?(?:anthropic\.)?', '', value)
    value = re.sub(r'-(?:cross-region-global|mantle-global|global-standard|global)$', '', value)
    return value[:40]


def _format_change(current, previous):
    if previous is None or previous.get('ce_error'):
        return "（前日数据不可用）"
    previous_cost = float(previous.get('total_actual') or 0)
    current_cost = float(current.get('total_actual') or 0)
    if previous_cost <= 0:
        return "（前日无用量）" if current_cost > 0 else "（与前日持平）"
    change_pct = (current_cost - previous_cost) / previous_cost * 100
    if abs(change_pct) < 0.05:
        return "（与前日持平）"
    arrow = '↑' if change_pct > 0 else '↓'
    return f"（较前日 {arrow}{abs(change_pct):.1f}%）"


def _format_reconcile_status(result):
    if result.get('ce_error'):
        return "不可用（账单查询失败）"
    diff = result.get('reconcile_diff_pct')
    if diff is None:
        return "无用量" if float(result.get('total_actual') or 0) == 0 else "待核对"
    diff = float(diff)
    state = "正常" if abs(diff) <= 5 else "⚠ 异常"
    return f"{state}（差异 {diff:+.2f}%）"


def _load_previous_result(date_str):
    """从 DDB 读取指定日期的已结算汇总，用作环比基准。

    头条是 T-2，它的前一天（T-3）在更早的运行里已经对过账并落库，直接读库
    比再查一次 Cost Explorer 更快也更省（CE 每请求 $0.01）。
    读不到就返回 None，由调用方转成"（前日数据不可用）"，绝不凭空算百分比。
    """
    try:
        records = get_reconcile_by_date(date_str)
    except Exception as e:
        logger.warning(f"Failed to load previous summary for {date_str}: {e}")
        return None
    summary = records.get('_summary') if records else None
    if not summary or summary.get('total_actual') is None:
        return None
    return {'total_actual': float(summary['total_actual'])}


def _build_mobile_summary(report_date, current, previous=None, month_summary=None, cost_label='当日费用'):
    """构造固定五行手机摘要；完整技术明细只保留在日志和 Web 控制台。

    cost_label 与 previous 解耦：每日运行的头条是已结算日（T-2），标注"已结算"；
    回填/重跑走默认"当日费用"，措辞不随环比基准是否存在而变。
    """
    parsed = datetime.strptime(report_date, '%Y-%m-%d')
    date_label = f"{parsed.month} 月 {parsed.day} 日"
    # 报告日期就在标题行里，金额行不再自称"昨日"：头条用的是已结算日（T-2），
    # 说"昨日"与实际日期差一天。
    if current.get('ce_error'):
        cost_line = f"{cost_label}：获取失败"
    else:
        cost = float(current.get('total_actual') or 0)
        cost_line = f"{cost_label}：${cost:,.2f}"
        if previous is not None:
            cost_line += _format_change(current, previous)

    if month_summary:
        month_total = float(month_summary['total_cost'])
        top_model = month_summary.get('top_model')
        month_line = f"本月累计：${month_total:,.2f}"
    else:
        month_total = None
        top_model = None
        month_line = "本月累计：暂不可用"

    if top_model and month_total and month_total > 0:
        model, model_cost = top_model
        share = model_cost / month_total * 100
        model_line = (
            f"费用最高：{_format_model_name(model)}"
            f"（${model_cost:,.2f}，占 {share:.1f}%）"
        )
    else:
        model_line = "费用最高：暂无数据"

    return '\n'.join([
        f"📊 Bedrock 日报｜{date_label}",
        cost_line,
        month_line,
        model_line,
        f"对账状态：{_format_reconcile_status(current)}",
    ])


def _get_ai_summary(report_text, date_str, ai_config, month_summary=None):
    """调用 AgentCore endpoint 生成一行补充结论；失败不阻塞日报。"""
    endpoint_arn = os.environ.get('AGENTCORE_ENDPOINT_ARN', '')
    if not endpoint_arn:
        logger.warning("AGENTCORE_ENDPOINT_ARN not set, skipping AI summary")
        return None

    try:
        arn_parts = endpoint_arn.split(':')
        region = arn_parts[3] if len(arn_parts) > 3 and arn_parts[3] else (os.environ.get('AWS_REGION') or 'us-east-1')
        client = boto3.client('bedrock-agentcore', region_name=region)
        prompt = (
            f"以下是 {date_str} 的 Bedrock 对账数据。请只返回一句中文补充结论，"
            f"不分点、不换行：\n\n{report_text}"
        )
        month_context = _build_month_context(datetime.now(timezone.utc), month_summary)
        if month_context:
            prompt += f"\n\n{month_context}"
        payload = json.dumps({
            'model_id': ai_config['model_id'],
            'prompt': prompt,
        })
        parts = endpoint_arn.split('/runtime-endpoint/')
        runtime_arn = parts[0]
        qualifier = parts[1] if len(parts) > 1 else 'DEFAULT'
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            payload=payload.encode('utf-8'),
        )
        raw = resp['response'].read().decode('utf-8')
        try:
            body = json.loads(raw)
            if isinstance(body, str):
                return ' '.join(body.split())
            text = body.get('result') or body.get('text')
            if isinstance(text, str) and text.strip():
                return ' '.join(text.split())
            logger.warning(f"Unexpected agent response shape: {raw[:200]}")
            return None
        except (json.JSONDecodeError, TypeError, AttributeError):
            return ' '.join(raw.split())
    except Exception as e:
        logger.error(f"AI summary failed: {e}")
        return None


def handler(event, context):
    now = datetime.now(timezone.utc)
    webhooks = get_webhook_config()

    override_date = event.get('date')
    if override_date:
        # 回填 / 重跑指定日期（单日）。silent=True 时不推送（backfill 批量补录用）
        silent = bool(event.get('silent'))
        try:
            parsed = datetime.strptime(override_date, '%Y-%m-%d')
        except ValueError:
            return {'statusCode': 400, 'error': f'Invalid date format: {override_date}, expected YYYY-MM-DD'}
        if parsed.date() >= now.date():
            return {'statusCode': 400, 'error': f'Date must be before today: {override_date}'}
        start_date = override_date
        end_date = (parsed + timedelta(days=1)).strftime('%Y-%m-%d')
        result = reconcile_one(start_date, end_date, now)
        if result.get('ce_error'):
            if not silent:
                send_webhook_all(f"[Bedrock 对账] 账号 {get_account_id()} | Cost Explorer 查询失败: {result['ce_error']}", webhooks)
            return {'statusCode': 500, 'error': 'ce_failed'}
        if not silent:
            send_webhook_all(
                _build_mobile_summary(
                    start_date,
                    result,
                    month_summary=_get_month_summary(start_date, {start_date: result}),
                ),
                webhooks,
            )
        logger.info(result['msg'])
        return {'statusCode': 200, 'date': start_date, 'total_actual': result['total_actual'], 'reconcile_diff_pct': result['reconcile_diff_pct']}

    # 默认每日运行：T-2 最终值 + T-1 暂估值仍完整写库，但手机只收到五行摘要。
    jobs = [
        ('T-2 (已结算)', (now - timedelta(days=2)).strftime('%Y-%m-%d'), (now - timedelta(days=1)).strftime('%Y-%m-%d')),
        ('T-1 (临时·账单可能未结算完)', (now - timedelta(days=1)).strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d')),
    ]

    header = f"[Bedrock 日报] 账号 {get_account_id()}\n"
    detail_report = header
    sections = []  # 按 job 顺序保存每段正文，供 AI 只取已结算段
    dates = []
    results = []
    for label, start_date, end_date in jobs:
        dates.append(start_date)
        section = f"\n========== {label}  {start_date} ==========\n"
        result = reconcile_one(start_date, end_date, now)
        results.append(result)
        if result.get('ce_error'):
            section += f"  ⚠ Cost Explorer 查询失败: {result['ce_error']}\n"
        else:
            section += result['msg']
        sections.append(section)
        detail_report += section

    # 手机头条只用已结算日（T-2）。CE 的 DAILY 数据 T+1 才完整，而本 Lambda 在
    # UTC 01:00 跑，此时 T-1 刚结束一小时，CE 普遍还返回 0 或残值。拿 T-1 当头条
    # 就会推出"当日费用 $0.00（较前日 ↓100%）"+"对账状态：无用量"这种假结论，
    # 并把 AI 的趋势判断一起带偏。
    # T-1 仍照常对账写库（Web 控制台可看暂估值，次日作为 T-2 重算覆盖），只是不上头条。
    settled_date, settled_result = dates[0], results[0]
    # AI 只看已结算段。T-1 段的 $0 会被读成"费用骤降"，把整段结论带偏；
    # detail_report 仍保留双段，只给 logger 用于排查。
    ai_report = f"{header}{sections[0]}"

    month_summary = _get_month_summary(
        settled_date,
        {settled_date: settled_result},
        exclude={dates[-1]},
    )
    # 环比基准取 T-3（头条 T-2 的前一天），从 DDB 读历史已结算值。
    # 读不到时传 ce_error 哨兵，让摘要输出"（前日数据不可用）"而不是编一个百分比。
    prev_date = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    mobile_summary = _build_mobile_summary(
        settled_date,
        settled_result,
        previous=_load_previous_result(prev_date) or {'ce_error': 'no_record'},
        month_summary=month_summary,
        cost_label='费用（已结算）',
    )

    # 推送策略判断（先判断是否推送，避免不推送时仍调用 AI 产生模型费用）
    notify_policy = get_notify_policy()
    beijing_now = now.astimezone(BEIJING_TZ)
    should_notify = True

    if notify_policy == 'never':
        should_notify = False
        logger.info("Notify policy is 'never', skipping notification")
    elif notify_policy == 'workday':
        should_notify = is_workday(beijing_now.date())
        if not should_notify:
            logger.info(f"Notify policy is 'workday' and today ({beijing_now.strftime('%Y-%m-%d')}) is not a workday, skipping notification")

    # AI 只追加一句可选结论；失败只记日志，不再把技术警告发到手机。
    if should_notify and webhooks:
        ai_config = get_ai_summary_config()
        if ai_config['enabled']:
            ai_summary = _get_ai_summary(ai_report, settled_date, ai_config, month_summary)
            if ai_summary:
                mobile_summary += f"\n💡 {ai_summary}"
            else:
                logger.warning("AI summary unavailable; sending deterministic mobile summary only")

    if should_notify:
        send_webhook_all(mobile_summary, webhooks)

    logger.info(detail_report)
    logger.info(f"Mobile summary:\n{mobile_summary}")
    return {'statusCode': 200, 'dates': dates}
