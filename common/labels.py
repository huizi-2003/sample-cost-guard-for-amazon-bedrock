"""CloudWatch metric label 解析（monitor 与 web 共用的唯一实现）。

Bedrock 的用量指标以 CW SEARCH 返回，label 形如
"AWS/Bedrock global.anthropic.claude-sonnet-4 InputTokenCount"。
这里负责把 label 拆成 (模型名, token 类型)，是计费与告警的共同基础，
因此集中在一处，避免 monitor / web 各自维护拷贝导致口径漂移。
"""


def extract_model_name(label):
    """从 CW SEARCH label 中提取模型名（去掉 namespace 前缀和 token 类型后缀）。

    识别不出模型名时返回空串，调用方应据此跳过该序列。CloudWatch SEARCH 在
    某个 ModelId 刚开始产生指标的瞬间，偶尔会返回只有 metric 名、没有 ModelId
    维度的聚合序列（label 形如裸 "CacheReadInputTokenCount"）；若原样返回，token
    类型后缀会被当成假模型名，污染模型列表并把真实用量错算成"未定价"。
    """
    # 前缀带不带前导空格两种形态都要剥（"AWS/Bedrock modelId ..." 及无前缀形态）
    label = label.replace('AWS/Bedrock ', '').replace('AWS/BedrockMantle ', '')
    label = label.replace('global.anthropic.', '').replace('anthropic.', '')
    # token 类型后缀：可能带前导空格（正常 "<model> <suffix>"），也可能整个 label
    # 就是裸后缀（无 ModelId）。两种都要能剥净，剥净后为空即视为无模型。
    for suffix in ('CacheReadInputTokenCount', 'CacheWriteInputTokenCount',
                   'InputTokenCount', 'OutputTokenCount',
                   'TotalInputTokens', 'TotalOutputTokens', 'Tokens'):
        if label == suffix or label.endswith(' ' + suffix):
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
