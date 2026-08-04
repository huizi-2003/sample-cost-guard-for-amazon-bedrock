# Bedrock Cost Guard 部署指南

## 这是什么

Bedrock 用量管控工具——帮你监控 Claude 等模型的调用费用，防盗刷 + 每日自动对账 + Web 管理界面。

本文是面向使用者的快速部署指南；架构说明、参数详解、自动更新的安全设计和 fork 维护者指南见 [README](README.md)。

## 费用

纯 Serverless 架构（Lambda + DynamoDB + API Gateway + EventBridge），**无 EC2、无常驻实例**。  
正常使用月费用约 **几块钱人民币**（主要是 Lambda 调用 + DynamoDB 存储，用量极低）。

## 部署步骤（5 分钟搞定）

推荐使用 **CloudShell**，无需安装任何东西，浏览器里直接操作。

### 1. 打开 CloudShell

登录 AWS Console → 右上角点击 `>_` 图标（或搜索 CloudShell）。

### 2. 获取你的公网 IP

> 💡 **获取你的公网 IP**：浏览器打开 https://checkip.amazonaws.com/ ，显示的即为你的出口 IP，填入 `AllowedCidrs` 时加上 `/32` 后缀。注意：如果使用 CloudShell 部署，不要在 CloudShell 里 curl 这个地址——那拿到的是 AWS 的 IP，不是你浏览器的。

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
- 每天 UTC 01:00（北京时间 09:00）自动对账
- 每周一 UTC 03:00（北京时间 11:00）检查并安装新版本

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

> 第一次自动升级之后，栈会永久关联一个专用的 CloudFormation service role。手工操作（含回退、删栈）的行为和注意事项见 README 的[「手动恢复」](README.md#手动恢复)一节。

## 从旧版本升级

如果你的栈是在自动更新功能发布之前部署的，只需手动升级这一次；完成后，系统会按每周计划自动升级。

升级前请确认目标 GitHub 仓库已经发布至少一个**正式 Release**。上游仓库已满足此前提；如果使用 fork，需要先自行发布 Release。

```bash
curl -LO https://raw.githubusercontent.com/huizi-2003/sample-cost-guard-for-amazon-bedrock/main/template.yaml

aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --capabilities CAPABILITY_NAMED_IAM
```

注意：

1. 不需要传任何 `--parameter-overrides`：白名单等原有参数会自动沿用；已删除的旧参数 `Branch` 会自动丢弃；新增的 `SourceRevision` 留空后会自动解析最新正式 Release。
2. `--capabilities` 必须是 `CAPABILITY_NAMED_IAM`。旧文档中的 `CAPABILITY_IAM` 会导致 `InsufficientCapabilities`，因为这次更新会把 IAM 角色替换为显式命名的角色。
3. 使用你自己的 AWS 凭证执行，不要传 `--role-arn`。

常见报错：

- `InsufficientCapabilities`：`--capabilities` 参数写错了，请使用 `CAPABILITY_NAMED_IAM`。
- CloudFormation 事件中出现 `has no published GitHub Release`：目标仓库还没有正式 Release。请先发布 Release，或临时在上述部署命令中加入 `--parameter-overrides SourceRevision=<commit sha>`，将版本固定到指定提交。

## 删除

不用了可以一键删除栈内资源：

```bash
aws cloudformation delete-stack --stack-name bedrock-cost-guard
aws cloudformation wait stack-delete-complete --stack-name bedrock-cost-guard
```

`StackUpdateRole` 是 CloudFormation 删栈全过程使用的 service role，因此模板通过 `DeletionPolicy: Retain` 保留它，避免角色先于其他资源删除而导致栈卡在 `DELETE_FAILED`。删栈成功后，这个角色不会自动删除。

该角色采用固定名称 `<栈名>-stack-update-role`。如果不清理，之后重建同名栈会在 `CreateRole` 阶段报 `EntityAlreadyExists`。确认栈已删除成功后，将以下命令中的 `<栈名>` 替换为实际栈名并执行；必须先删除内联策略，再删除角色：

```bash
aws iam delete-role-policy --role-name <栈名>-stack-update-role --policy-name StackUpdatePolicy
aws iam delete-role --role-name <栈名>-stack-update-role
```
