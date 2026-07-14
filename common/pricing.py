"""Bedrock 价目表与成本估算（monitor / web 共用）。

$/MTok — Bedrock cross-region 基准价格（direct in-region ≈ ×1.1，差异忽略）。
cache_write 统一用 5min 标准价格。

用途是"预估 / 费用红线兜底"，是**近似价**，不是账单真值
（账单真值由 reconciler 从 Cost Explorer T+1 获取）。
"""

TOKEN_TYPES = ('input', 'output', 'cache_read', 'cache_write')

PRICING = {
    'opus':    {'input': 5,   'output': 25,  'cache_read': 0.5,  'cache_write': 6.25},
    'fable':   {'input': 10,  'output': 50,  'cache_read': 1.0,  'cache_write': 12.5},
    'sonnet':  {'input': 3,   'output': 15,  'cache_read': 0.3,  'cache_write': 3.75},
    'haiku':   {'input': 1,   'output': 5,   'cache_read': 0.1,  'cache_write': 1.25},
    # OpenAI 模型走 bedrock-mantle 端点，CloudWatch 只发布 TotalInputTokens/
    # TotalOutputTokens（无 cache 拆分），故 cache 命中的 token 会并入 input，
    # 按满价 input 估算（有 cache 时略偏高）。cache_read 价保留仅备将来指标拆分。
    # GPT-5.6 系列(Sol/Terra/Luna)有独立的 30m cache-write 计费；
    # GPT-5.5/5.4 无独立 cache-write，故其 cache_write 取 input 价。
    # key 用带变体后缀的完整串(gpt-5.6-sol 等)，避免 5.6 三个变体价格互相串味。
    'gpt-5.6-sol':   {'input': 5.0,  'output': 30.0, 'cache_read': 0.5,  'cache_write': 6.25},
    'gpt-5.6-terra': {'input': 2.5,  'output': 15.0, 'cache_read': 0.25, 'cache_write': 3.125},
    'gpt-5.6-luna':  {'input': 1.0,  'output': 6.0,  'cache_read': 0.1,  'cache_write': 1.25},
    'gpt-5.5': {'input': 5.5,  'output': 33,   'cache_read': 0.55,  'cache_write': 5.5},
    'gpt-5.4': {'input': 2.75, 'output': 16.5, 'cache_read': 0.275, 'cache_write': 2.75},
}


def match_pricing(model_name):
    """按模型名模糊匹配到价格系列，返回 {token_type: $/MTok} 或 None。"""
    lower = model_name.lower()
    for series, prices in PRICING.items():
        if series in lower:
            return prices
    return None


def estimate_cost(models):
    """按价目表估算一批模型明细的预估费用。

    参数 models: {model_name: {input, output, cache_read, cache_write}}（token 数）
    返回 (cost_usd: float, unpriced_models: set[str])
      - cost_usd: 预估美元。单价单位为 $/MTok，故 tokens / 1e6 * price
      - unpriced_models: 有 token 量但匹配不到价格的模型；这部分费用被低估，
        供告警提示，避免"费用红线"因缺价而静默失效
    """
    cost = 0.0
    unpriced = set()
    for model_name, type_counts in (models or {}).items():
        if not isinstance(type_counts, dict):
            continue
        prices = match_pricing(model_name)
        if not prices:
            if any(type_counts.get(t, 0) for t in TOKEN_TYPES):
                unpriced.add(model_name)
            continue
        for token_type in TOKEN_TYPES:
            cost += type_counts.get(token_type, 0) / 1_000_000 * prices[token_type]
    return cost, unpriced
