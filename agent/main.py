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

SYSTEM_PROMPT = """你是 AWS Bedrock 费用分析助手。你会收到一段 Bedrock 每日对账数据（包含各模型费用、token 用量、对账差异等），请用简洁的中文生成一段摘要，要求：

1. 先给出今日总费用（美元）
2. 列出 Top 3 费用最高的模型及其花费
3. 如果对账差异（CE vs CW）超过 5%，提醒关注
4. 如果总费用相比数据中提及的前一日有显著变化（>20%），指出趋势
5. 保持简洁，不超过 200 字"""


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
    text = response.message['content'][0]['text']

    logger.info(f"Summary generated, length: {len(text)}")
    return text


if __name__ == "__main__":
    app.run()
