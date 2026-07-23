"""Bedrock Cost Guard — AI 账单总结 Agent

部署在 AgentCore Runtime 上（S3 zip code deploy），接收对账数据 JSON，
直接调 Bedrock invoke_model 生成中文费用摘要。

无外部依赖（boto3 在 AgentCore Runtime 中自带），zip 仅需此文件。

入口合约：
  POST /invocations
  Body: {"model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0", "prompt": "..."}
  Response: "总结文本"
"""

import json
import logging
import boto3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bedrock = boto3.client('bedrock-runtime', region_name='us-east-1')

SYSTEM_PROMPT = """你是 AWS Bedrock 费用分析助手。你会收到一段 Bedrock 每日对账数据（包含各模型费用、token 用量、对账差异等），请用简洁的中文生成一段摘要，要求：

1. 先给出今日总费用（美元）
2. 列出 Top 3 费用最高的模型及其花费
3. 如果对账差异（CE vs CW）超过 5%，提醒关注
4. 如果总费用相比数据中提及的前一日有显著变化（>20%），指出趋势
5. 保持简洁，不超过 200 字"""


def invoke(payload):
    """Agent 入口，接收 model_id + prompt，返回 AI 生成的总结文本。"""
    if isinstance(payload, (bytes, bytearray)):
        payload = json.loads(payload.decode('utf-8'))
    elif isinstance(payload, str):
        payload = json.loads(payload)

    model_id = payload.get("model_id", "us.anthropic.claude-sonnet-4-20250514-v1:0")
    prompt = payload.get("prompt", "")

    if not prompt:
        return "No data provided for summarization."

    logger.info(f"Invoking model: {model_id}")

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    })

    resp = bedrock.invoke_model(modelId=model_id, body=body)
    result = json.loads(resp['body'].read())
    text = result['content'][0]['text']

    logger.info(f"Summary generated, length: {len(text)}")
    return text


# === AgentCore Runtime HTTP server ===
# AgentCore 要求监听 8080 端口，提供 /invocations (POST) 和 /ping (GET)

from http.server import HTTPServer, BaseHTTPRequestHandler


class AgentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ping':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/invocations':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                result = invoke(body)
                response = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                logger.error(f"Invocation error: {e}")
                err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(err)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    server = HTTPServer(('0.0.0.0', 8080), AgentHandler)
    logger.info("Agent server starting on port 8080")
    server.serve_forever()
