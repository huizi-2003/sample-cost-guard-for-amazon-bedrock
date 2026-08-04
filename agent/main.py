"""Bedrock Cost Guard — AI 账单总结 Agent

部署在 AgentCore Runtime 上（S3 zip code deploy），接收对账数据，
调 Bedrock 生成中文费用摘要。使用 Strands Agent SDK 自动适配不同模型。

入口合约：
  POST /invocations
  Body: {"model_id": "global.amazon.nova-2-lite-v1:0", "prompt": "..."}
  Response: "总结文本"
"""

import logging
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """你会收到 Bedrock 对账数据（每日明细 + 本月累计）。你的输出会作为一行补充结论，
附在一份已包含“昨日费用、本月累计、费用最高模型、对账状态”的确定性摘要下方。

要求：
1. 只输出一句话，不分点、不换行、不加任何前缀
2. 不要复述具体金额和百分比——这些数字摘要里已经有了，你重复只会产生不一致
3. 说摘要说不出来的东西：费用趋势（连涨/连跌/平稳）、异常成因（哪个模型驱动了波动）、
   与本月日均的偏离方向
4. 没有值得说的异常时，就说费用平稳，不要硬找问题
5. 不超过 50 字"""


@app.entrypoint
def invoke(payload):
    """Agent 入口，接收 model_id + prompt，返回 AI 生成的总结文本。"""
    model_id = payload.get("model_id", "global.amazon.nova-2-lite-v1:0")
    prompt = payload.get("prompt", "")

    if not prompt:
        return "No data provided for summarization."

    logger.info(f"Invoking model: {model_id}")

    model = BedrockModel(model_id=model_id)
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
    response = agent(prompt)
    text = str(response).strip()

    logger.info(f"Summary generated, length: {len(text)}")
    return text


if __name__ == "__main__":
    app.run()
