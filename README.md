# Bedrock Lite Guard

AWS Bedrock 用量管控工具集：反盗刷监控、每日对账、Web 管理界面。

## 功能

> **注意**：本工具监控的是 Cost Explorer 中 **Amazon Bedrock Service**（不是 Amazon Bedrock）。  
> Claude 等模型的调用计费在 `Amazon Bedrock Service` 下，而 Nova 等模型在 `Amazon Bedrock` 下，两者是不同的 CE Service。

### 1. Monitor — 反盗刷监控

- 每 5 分钟跨所有 Region 聚合 Bedrock token 用量
- 覆盖 AWS/Bedrock + AWS/BedrockMantle 双 namespace，所有模型、所有 token 类型（含 cache）
- 监控 Region：首次运行自动写入一组默认区域（us-east-1/us-east-2/us-west-1/us-west-2/eu-central-1/eu-west-1/eu-west-3/ap-northeast-1/ap-southeast-1/ap-southeast-2），可通过 Web Console 修改
- 三层阈值告警（5min / 15min / daily），超阈值推送告警
- 告警附带 Top Region + Top Model 明细
- 告警抑制（15min/daily 窗口避免重复轰炸）
- **数据持久化**：每次运行结果写入 DynamoDB（PK=`MONITOR#{date}`, SK=`T#{HH:MM}`），含 5min 总量、daily 累计、各模型明细，3 天 TTL 自动过期
- 自我保护：阈值读取失败、多 region 查询失败时发送告警

### 2. Reconciler — 每日对账 + 账单汇总

- 每天 UTC 17:00（北京时间 01:00）自动运行
- **双日期对账**：每次 cron 同时对账 T-2（已结算，最终数据）和 T-1（临时，账单可能不完整），提供次日可见性 + 前日修正
- **Token 对账**：对比 CE（计费系统）与 CloudWatch（监控系统）的 token 总量，验证两个系统数据一致性
- 对账口径为 UTC 日（CE DAILY 粒度按 UTC 解释）
- CloudWatch 查询的 Region 从 CE 账单的 USAGE_TYPE 前缀自动推导（账单里有哪些区域就查哪些，37 个前缀映射），无需手动配置
- 按模型拆分费用明细，每模型展示 5 种 token 类型：input / output / cache_read / cache_write / cache_write_1h
- 对账结果 + CE 原始明细 + CW 各 region 明细 全部存入 DynamoDB（90 天 TTL）
- 每日推送合并报告（两个日期的 token 对账差异 + 费用明细），不管差异多少都推
- **手动触发模式**：传入 `event.date` 可单独对账指定日期

#### 对账原理与计算规则

```
═══════════════════════════════════════════════════════════════
时间线
═══════════════════════════════════════════════════════════════

  cron(0 17 * * ? *) = UTC 17:00 触发
  对账日期 = T-2（即 now.date() - 2 天的完整 UTC 日）
  原因：CE 数据 T+1 才完整，查前天确保账单已出
  
  同时对账 T-1（临时数据，账单可能不完整）
  双日期策略：T-2 为最终数据，T-1 提供次日可见性

═══════════════════════════════════════════════════════════════
数据源 A — Cost Explorer (计费系统)
═══════════════════════════════════════════════════════════════

  API:     ce.get_cost_and_usage()
  Filter:  SERVICE = "Amazon Bedrock Service"
  GroupBy: USAGE_TYPE
  Metrics: UnblendedCost + UsageQuantity
  
  USAGE_TYPE 格式示例：
    USE1-Claude4.6Opus-input-tokens-cross-region-global
    USE1-Claude4.6Opus-cache-read-input-token-count-cross-region-global
    USW2-anthropic.claude-opus-4-8-mantle-cache-write-tokens-global-standard
  
  Token 判断规则：
    usage_type 中含 "token"（覆盖 "tokens" 和 "token-count"）
    排除 "searchunits" 等非 token 计费项
  
  Token 总量计算：
    ce_token_total = Σ (quantity × 1000)   // quantity 单位为 1K tokens

  模型名提取 (extract_model_identity)：
    1. 去掉 region 前缀（第一个 '-' 之前：USE1-, USW2-, EUW1-...）
    2. 去掉 token 类型段（长段优先匹配，保留路由后缀如 cross-region-global）：
       -cache-read-input-token-count
       -cache-write-1h-input-token-count
       -cache-write-input-token-count
       -cache-read-tokens
       -cache-write-1h-tokens
       -cache-write-tokens
       -cacheread-tokens
       -cachewrite-tokens
       -input-token-count
       -output-token-count
       -input-tokens
       -output-tokens
    3. 去掉 -standard 后缀，清理双连字符

  Token 类型判断 (get_token_type)：
    含 "cache-read" / "cacheread" → cache_read
    含 "cache-write" / "cachewrite" 且含 "1h" → cache_write_1h
    含 "cache-write" / "cachewrite"（不含 1h）→ cache_write
    含 "output" → output
    其余 → input

═══════════════════════════════════════════════════════════════
数据源 B — CloudWatch (监控系统)
═══════════════════════════════════════════════════════════════

  Region 来源：从 CE 账单 USAGE_TYPE 前缀自动推导（37 个前缀映射）
    （USE1→us-east-1, USE2→us-east-2, USW2→us-west-2, EUW1→eu-west-1,
     EUC1→eu-central-1, APN1→ap-northeast-1, APS1→ap-southeast-1 ...）
  跨推导出的 Region 并发查询（ThreadPoolExecutor, max_workers=5）：
  
  查询 1 — AWS/Bedrock namespace：
    SEARCH('{AWS/Bedrock,ModelId} TokenCount', 'Sum', 3600)
    聚合为 SUM(search_bedrock)
  
  查询 2 — AWS/BedrockMantle namespace：
    TotalInputTokens + TotalOutputTokens（Sum, Period=3600）
    表达式：FILL(mantle_in,0) + FILL(mantle_out,0)
  
  时间窗口：与 CE 对齐，同一 UTC 日 00:00 ~ 次日 00:00
  
  cw_token_total = Σ 所有 region 的 (bedrock_total + mantle_total)

═══════════════════════════════════════════════════════════════
对账公式
═══════════════════════════════════════════════════════════════

  reconcile_diff_pct = (CE_total - CW_total) / CW_total × 100

  预期结果：diff ≈ 0%
  实测：2026-06-23 CE=32,873,693, CW=32,873,693, diff=0.00%
  实测：2026-06-24 CE=22,968,294, CW=22,968,294, diff=0.00%

  若差异显著，可能原因：
    - CloudWatch 指标丢点或 Region 配置未覆盖
    - CE 侧有新 USAGE_TYPE 格式未被 is_token_usage() 识别
    - 跨账号调用计费归属差异

═══════════════════════════════════════════════════════════════
存储结构 (DynamoDB)
═══════════════════════════════════════════════════════════════

  PK = RECONCILE#{date}
  
  SK = {model_name}  → 每模型聚合数据
    actual_cost, cost_input, cost_output, cost_cache_read, cost_cache_write, cost_cache_write_1h
    tokens_input_1k, tokens_output_1k, tokens_cache_read_1k, tokens_cache_write_1k, tokens_cache_write_1h_1k
  
  SK = _summary → 当日汇总
    total_actual, model_count, ce_token_total, cw_token_total, reconcile_diff_pct
  
  SK = _ce_detail → CE 原始明细 (JSON)
    [{usage_type, cost, quantity, unit}, ...]
  
  SK = _cw_detail → CW 各 region 明细 (JSON)
    {region: token_count, ...}
```

### 3. Web Console — 管理界面

- **费用总览**：
  - 近 N 天汇总卡片（总费用、日均、环比变化）
  - 费用趋势堆叠面积图（按模型着色）
  - Top 模型水平柱状图
  - 路由分类占比（cross-region / mantle / direct）
- **历史对账**：
  - 按日期查看每模型费用 + token 用量（input/output/cache_read/cache_write/cache_write_1h 五类）
  - 可展开 CE 原始明细（每条 USAGE_TYPE 的 cost、quantity、unit）
  - 可展开 CW 各 region 明细（每 region 的 token 总量）
  - 对账指标卡片（总费用、模型数、CE/CW token 总量、对账差异百分比）
  - 历史回填功能：指定天数批量触发 reconciler 补录历史数据
- **今日监控**：
  - 实时 5 分钟用量图表（模型维度堆叠，增量展示）
  - 昨日同期对比虚线叠加
  - 每 5 分钟自动刷新
  - 异常告警条（当日用量 >150% 昨日同期时红色提醒）
- **配置管理**：阈值、监控 Region 列表、Webhook 设置
- 无鉴权，通过 API Gateway Resource Policy 限制 IP 访问

### 4. 通知推送

支持三种 Webhook 渠道（DDB `CONFIG#webhook` 配置 type 字段）：

| 渠道 | type 值 | Payload 格式 |
|------|---------|-------------|
| 飞书 | feishu | `{msg_type: "text", content: {text: ...}}` |
| 钉钉 | dingtalk | `{msgtype: "text", text: {content: ...}}` |
| 企业微信 | wecom | `{msgtype: "text", text: {content: ...}}` |

### 5. Log Explorer — 调用日志查询（规划中）

- 数据源：Bedrock Invocation Log (S3) + CloudTrail (S3)
- 查询引擎：Athena
- 按 requestId / 用户 / 模型 / 时间范围查询

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                     CloudFormation Stack                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  EventBridge (5min) ──→ Lambda: monitor                      │
│  EventBridge (daily) ─→ Lambda: reconciler                   │
│                              │                               │
│                              ▼                               │
│                        DynamoDB Table                         │
│                     (bedrock-cost-guard)                      │
│                              ▲                               │
│                              │                               │
│  API Gateway (REST) ──→ Lambda: web (FastAPI + mangum)        │
│       (Resource Policy IP 白名单)                             │
│                                                              │
└─────────────────────────────────────────────────────────────┘

外部依赖：
  - CloudWatch（跨 Region 读取 Bedrock 指标）
  - Cost Explorer（T+1 账单数据）
  - 飞书/钉钉/企微 Webhook（告警通知）
```

| 组件 | 技术 | 说明 |
|------|------|------|
| 后端计算 | Lambda Python 3.12 | monitor + reconciler + web |
| 定时触发 | EventBridge Rules | 5 分钟 / 每日 UTC 17:00 |
| 数据存储 | DynamoDB 单表 | 配置 + 阈值 + 对账记录 + 监控记录 + 告警状态 |
| Web 管理 | FastAPI + mangum + 原生 HTML/JS | API Gateway 代理到 Lambda |
| 访问控制 | API Gateway Resource Policy | IP 白名单（AllowedCidrs 参数） |
| 基础设施 | CloudFormation | 纯 serverless，无服务器管理 |
| 通知 | Webhook（DDB 配置） | 飞书 / 钉钉 / 企微 |
| 代码来源 | GitHub | 部署时自动从本仓库拉取代码，无需打包 |

## 部署

整个栈是**自包含**的：CloudFormation 自动创建 S3 桶、从 GitHub 拉取代码、部署所有资源。**无需手动打包代码。**

> **关于"一键 Launch Stack 按钮"**：AWS CloudFormation 的 `templateURL` 只接受 S3 地址，不接受 GitHub raw URL（实测报错 `TemplateURL must be a supported URL`）。因此在不使用 S3 托管模板的前提下，无法提供"点一下就建栈"的按钮。下面的**方式一（控制台上传）**是无需 S3、无需 CLI 的最快替代方案，代码仍从 GitHub 拉取。

### 方式一：CloudFormation 控制台上传（最快，无需 CLI / 无需 S3）

1. 下载模板文件 [`template.yaml`](https://raw.githubusercontent.com/huizi-2003/sample-cost-guard-for-amazon-bedrock/main/template.yaml)（右键另存，或 `curl -O`）。
2. 打开 [CloudFormation 控制台 → Create stack](https://console.aws.amazon.com/cloudformation/home#/stacks/create) → **With new resources (standard)**。
3. Specify template → **Upload a template file** → 选择刚下载的 `template.yaml`。
4. Stack name 填 `bedrock-cost-guard`。
5. 参数 `AllowedCidrs` 填你访问 Web Console 的公网 IP（如 `1.2.3.4/32`，多个用逗号分隔；默认 `127.0.0.1/32` 为全部关闭）。
6. 勾选 **"I acknowledge that AWS CloudFormation might create IAM resources"** → Create stack。
7. 等待 3-5 分钟，在 **Outputs** 标签找到 `WebConsoleUrl` 即为管理界面地址。

> 部署过程中模板会自动从 GitHub 下载 monitor/reconciler/web 代码，无需手动打包。

### 方式二：CloudShell（推荐 CLI 用户）

打开 [CloudShell](https://console.aws.amazon.com/cloudshell/)，执行：

```bash
# 克隆代码
git clone https://github.com/huizi-2003/sample-cost-guard-for-amazon-bedrock.git
cd sample-cost-guard-for-amazon-bedrock

# 部署（将 AllowedCidrs 改为你的出口 IP，多个用逗号分隔）
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --parameter-overrides AllowedCidrs=1.2.3.4/32 Version=$(date +%s) \
  --capabilities CAPABILITY_IAM

# 查看部署结果（获取 Web Console URL）
aws cloudformation describe-stacks --stack-name bedrock-cost-guard \
  --query 'Stacks[0].Outputs'
```

> CloudShell 会话闲置 20 分钟会断开，但不影响 CloudFormation 部署（后台异步执行）。

### 方式三：本地 AWS CLI

```bash
git clone https://github.com/huizi-2003/sample-cost-guard-for-amazon-bedrock.git
cd sample-cost-guard-for-amazon-bedrock

aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name bedrock-cost-guard \
  --parameter-overrides AllowedCidrs=1.2.3.4/32 Version=$(date +%s) \
  --capabilities CAPABILITY_IAM
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `AllowedCidrs` | 允许访问 Web Console 的 CIDR 列表（逗号分隔）<br/>**变更时自动重新部署 API，Resource Policy 即时生效** | `127.0.0.1/32`（全部关闭） |
| `GitHubOwner` / `GitHubRepo` / `Branch` | 代码来源 | 本仓库 main 分支 |
| `Version` | 代码版本标记，改动即触发重新拉代码 + 重新部署 API | 1 |

> **⚠️ 关于 `Version`（发布代码必读）**
>
> `Version` 是由**开发者手动控制**的"发布开关"，用来告诉 CloudFormation 是否需要重新从 GitHub 拉取最新代码：
>
> - 模板中拉代码的自定义资源 `CodeObject` **只在 `Version` 变化时才会重新触发**，进而重新下载 GitHub 代码、更新 Lambda。
> - **改了代码 → 必须改 `Version`**（`git push` 之后部署时把它改成一个新值），否则 CloudFormation 认为没变化，不会重新拉代码，Lambda 仍是旧版本（"部署了但代码没更新"就是这么来的）。
> - **没改代码**（只调 `AllowedCidrs`、阈值等）→ **保持 `Version` 不变**，部署更快，也不会白拉一遍 GitHub。
> - 值是多少不重要，只要**和上次不同**就会触发。可以简单递增（`1 → 2 → 3`），也可以直接用发布日期，一眼看出这版代码是哪天发的：
>
>   ```bash
>   --parameter-overrides Version=20260706 AllowedCidrs=1.2.3.4/32
>   ```
>
> 一句话：**改代码就动 `Version`，不改代码就别动它。**
>
> **✅ 修复说明（v3）**：`AllowedCidrs` 变更时，模板现在使用 Custom Resource 强制重新部署 API Gateway，确保 Resource Policy 变更立即生效（之前版本只更新 Policy 但不重新部署，导致访问控制不生效）。

### 部署后

栈 Outputs 中的 `WebConsoleUrl` 即为管理界面地址（HTTPS），通过它配置：
- Webhook URL + 渠道类型（feishu / dingtalk / wecom）
- 监控 Region 列表
- 阈值

```bash
# 更新代码：push 后重新部署（Version 用时间戳自动变）
aws cloudformation deploy --template-file template.yaml --stack-name bedrock-cost-guard \
  --parameter-overrides Version=$(date +%s) AllowedCidrs=1.2.3.4/32 --capabilities CAPABILITY_IAM

# 手动触发对账（不用等定时任务）
aws lambda invoke --function-name bedrock-cost-guard-reconciler --region us-east-1 /dev/null

# 删除所有资源
aws cloudformation delete-stack --stack-name bedrock-cost-guard
```

## 目录结构

```
bedrock-cost-guard/
├── README.md
├── template.yaml          # CloudFormation 模板（自包含：自动建桶 + 拉代码 + 部署）
├── common/
│   ├── config.py          # DynamoDB 读写封装
│   └── webhook.py         # 通知发送（飞书/钉钉/企微）
├── monitor/
│   └── handler.py         # 反盗刷监控 Lambda
├── reconciler/
│   └── handler.py         # 每日对账 Lambda
└── web/
    ├── app.py             # FastAPI 后端
    ├── requirements.txt
    └── static/
        └── index.html     # 管理页面（费用总览 + 历史对账 + 今日监控 + 配置管理）
```

