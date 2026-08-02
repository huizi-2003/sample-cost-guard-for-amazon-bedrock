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
  --parameter-overrides AllowedCidrs=YOUR_IP/32 \
  --capabilities CAPABILITY_NAMED_IAM
```

等 3~5 分钟即可完成。部署时会自动下载本仓库**最新正式 Release** 的代码，无需手动打包。

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
- 每周一上午 11:00（北京时间）检查并安装新版本

## 后续更新

**不需要做任何事。** 系统每周一自动检查新版本并整栈升级，升级后会自动验证服务是否正常；如果有问题会自动退回上一个可用版本，并通过你配置的 Webhook 告警。

在管理界面的「版本管理」页可以：
- 查看当前版本、最新版本、更新记录（含每次更新的内容）
- 关闭自动更新（如果你所在组织有变更管控要求）
- 点「立即更新」手动触发一次

只在需要回退或部署特定版本时才需要命令行：

```bash
# 部署指定版本（绕过自动升级）
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --parameter-overrides SourceRevision=<commit sha 或 tag> \
  --capabilities CAPABILITY_NAMED_IAM
```

## 删除

不用了可以一键删除所有资源：

```bash
aws cloudformation delete-stack --stack-name bedrock-cost-guard
```
