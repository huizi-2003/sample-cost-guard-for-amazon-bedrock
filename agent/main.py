"""Bedrock Cost Guard — AI 账单总结 Agent

部署在 AgentCore Runtime 上（S3 zip code deploy），接收对账数据，
调 Bedrock 生成中文费用摘要。使用 Strands Agent SDK 自动适配不同模型。

入口合约：
  POST /invocations
  Body: {"model_id": "us.amazon.nova-2-lite-v1:0", "prompt": "..."}
  Response: "总结文本"
"""

import logging
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """你会收到 Bedrock 对账数据，包含每日明细和本月累计数据。
请用一句简洁的中文总结账单情况：
1. 只输出一句话，不分点、不换行、不加任何前缀
2. 必须包含：本月累计费用、昨日费用、费用最高的模型
3. 昨日费用相比本月日均波动 >20% 或对账差异 >5% 时，在这句话里顺带点出
4. 不超过 80 字"""


@app.entrypoint
def invoke(payload):
    """Agent 入口，接收 model_id + prompt，返回 AI 生成的总结文本。"""
    model_id = payload.get("model_id", "us.amazon.nova-2-lite-v1:0")
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
