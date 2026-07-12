"""CloudWatch metric label 解析（monitor 与 web 共用的唯一实现）。

Bedrock 的用量指标以 CW SEARCH 返回，label 形如
"AWS/Bedrock global.anthropic.claude-sonnet-4 InputTokenCount"。
这里负责把 label 拆成 (模型名, token 类型)，是计费与告警的共同基础，
因此集中在一处，避免 monitor / web 各自维护拷贝导致口径漂移。
"""


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
