# Bedrock Cost Guard 部署指南

## 这是什么

Bedrock 用量管控工具——帮你监控 Claude 等模型的调用费用，防盗刷 + 每日自动对账 + Web 管理界面。

## 费用

纯 Serverless 架构（Lambda + DynamoDB + API Gateway + EventBridge），**无 EC2、无常驻实例**。  
正常使用月费用约 **几块钱人民币**（主要是 Lambda 调用 + DynamoDB 存储，用量极低）。

## 部署步骤（5 分钟搞定）

推荐使用 **CloudShell**，无需安装任何东西，浏览器里直接操作。

### 1. 打开 CloudShell

登录 AWS Console → 右上角点击 `>_` 图标（或搜索 CloudShell）。

### 2. 获取你的公网 IP

```bash
curl -s https://checkip.amazonaws.com
```

记下输出的 IP（例如 `52.83.xxx.xxx`），后面要用。

### 3. 部署

```bash
# 克隆代码
git clone https://github.com/huizi-2003/sample-cost-guard-for-amazon-bedrock.git
cd sample-cost-guard-for-amazon-bedrock

# 部署（把 YOUR_IP 替换成第 2 步拿到的 IP）
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --parameter-overrides AllowedCidrs=YOUR_IP/32 Version=$(date +%s) \
  --capabilities CAPABILITY_IAM
```

等 3~5 分钟即可完成。

### 4. 获取管理界面地址

```bash
aws cloudformation describe-stacks --stack-name bedrock-cost-guard \
  --query 'Stacks[0].Outputs[?OutputKey==`WebConsoleUrl`].OutputValue' --output text
```

输出的 HTTPS 链接就是你的管理界面，浏览器打开即可。

### 5. 首次配置

打开管理界面后，在「配置管理」页设置：
- **Webhook URL**：填你的飞书/钉钉/企微机器人地址（用于接收告警）
- **渠道类型**：选 feishu / dingtalk / wecom
- 阈值和监控区域有默认值，可按需调整

## 完成 🎉

部署后系统会自动：
- 每 5 分钟监控 Bedrock 用量（超阈值推送告警）
- 每天凌晨 01:00（北京时间）自动对账

## 后续更新

代码有更新时，重新执行部署命令即可（改一下 Version）：

```bash
cd sample-cost-guard-for-amazon-bedrock && git pull
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --parameter-overrides AllowedCidrs=YOUR_IP/32 Version=$(date +%s) \
  --capabilities CAPABILITY_IAM
```

## 删除

不用了可以一键删除所有资源：

```bash
aws cloudformation delete-stack --stack-name bedrock-cost-guard
```
