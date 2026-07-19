# Bedrock Cost Guard

> ⚠️ **本项目为示例代码（Sample），仅供学习和参考，不建议直接用于生产环境。使用本代码所产生的任何费用或安全风险由使用者自行承担，作者不提供任何担保或技术支持。**

AWS Bedrock 用量管控工具集：用量监控、每日对账、Web 管理界面。

## 目录

- [为什么需要这个项目](#为什么需要这个项目)
- [功能](#功能)
  - [1. Monitor — 用量监控](#1-monitor--用量监控)
  - [2. Reconciler — 每日对账](#2-reconciler--每日对账--账单汇总)
  - [3. Web Console — 管理界面](#3-web-console--管理界面)
  - [4. 通知推送](#4-通知推送)
  - [5. IAM 权限扫描](#5-iam-权限扫描--bedrock-调用权限审计)
  - [6. Log Explorer — 调用日志查询（规划中）](#6-log-explorer--调用日志查询规划中)
- [技术架构](#技术架构)
- [成本估算](#成本估算)
- [部署](#部署)
- [目录结构](#目录结构)

## 为什么需要这个项目

AWS 账单默认 T+1 才出数据——今天的用量明天才能在 Cost Explorer 看到。如果 Bedrock 被滥用（Key 泄露、内部无节制调用等），等你发现时可能已经产生了大量费用。

**本项目的核心价值：准实时监控 + 即时告警**，每 5 分钟从 CloudWatch 聚合全账号所有 Region 的 Bedrock Token 用量，超阈值立即推送告警，不用等第二天的账单。

### 与 LiteLLM 等代理网关的关系

本项目**不冲突**，定位不同：

| | Bedrock Cost Guard | LiteLLM / Gateway 类 |
|---|---|---|
| 监控粒度 | **整个 AWS 账号**，全 Region、全模型 | 仅经过网关的流量 |
| 部署方式 | Serverless（Lambda + EventBridge），无需代理层 | 需要部署代理服务 |
| 数据来源 | CloudWatch 指标 + Cost Explorer 账单 | 自身请求日志 |
| 适用场景 | 账号级别的费用兜底、异常发现、每日对账 | 请求级别的 quota 控制、路由、负载均衡 |

简单说：LiteLLM 管的是"经过它的流量"，本项目管的是"整个 AWS 账号的 Bedrock 费用"。即使你用了 LiteLLM，仍然可能有直接调用 Bedrock API 的场景（IDE 插件、其他服务、CLI 等），这些不经过网关的流量只有本项目能覆盖到。

## 功能

> **注意**：本工具监控的是 Cost Explorer 中 **Amazon Bedrock Service**（不是 Amazon Bedrock）。  
> Claude 等模型的调用计费在 `Amazon Bedrock Service` 下，而 Nova 等模型在 `Amazon Bedrock` 下，两者是不同的 CE Service。

### 1. Monitor — 用量监控

- 每 5 分钟跨所有 Region 聚合 Bedrock token 用量
- 覆盖 AWS/Bedrock + AWS/BedrockMantle 双 namespace，所有模型、所有 token 类型（含 cache）
- **CW 查询方式**（与 reconciler 不同）：直接 SEARCH per-model 指标并返回原始数据点（`ReturnData=True`, Period=300），代码侧按时间戳切分 5min/15min/daily 窗口，同时获得每模型每类型明细；BedrockMantle 使用 `SEARCH('{AWS/BedrockMantle,Model} Tokens', 'Sum', 300)` 聚合 metric
- **总开关**：DDB `CONFIG#monitor_enabled`，设为 `false` 时跳过全部监控逻辑（省 CloudWatch 费用），默认开启。通过 Web Console 配置管理页面切换
- 监控 Region：首次运行自动写入一组默认区域（us-east-1/us-east-2/us-west-1/us-west-2/eu-central-1/eu-west-1/eu-west-3/ap-northeast-1/ap-southeast-1/ap-southeast-2），可通过 Web Console 修改
- **Delta 增量告警**（5min / 15min / daily 三层阈值，单位为美元 $）：
  - 5min/15min 窗口：比较当前 daily 累计与基线记录的**增量**（delta = 当前累计 - 基线累计），增量超阈值才告警，避免累计值持续触发
  - daily 窗口：直接比较当日累计总量
  - 基线选取：从当天已有完整记录中取最近 5min/15min 内的快照
  - warm-up 保护：无有效基线时跳过 5min/15min 判定，避免冷启动误报
- 告警附带 Top Region + Top Model 明细
- 告警抑制（15min/daily 窗口避免重复轰炸）
- **数据持久化**：每次运行结果写入 DynamoDB（PK=`MONITOR#{date}`, SK=`T#{HH:MM}`），含 5min 总量、daily 累计、各模型按 token 类型拆分明细（input/output/cache_read/cache_write），2 天 TTL 自动过期
- 自我保护：阈值读取失败、多 region 查询失败时发送告警

### 2. Reconciler — 每日对账 + 账单汇总

- 每天 UTC 01:00（北京时间 09:00）自动运行
- **双日期对账**：每次 cron 同时对账 T-2（已结算，最终数据）和 T-1（临时，账单可能不完整），提供次日可见性 + 前日修正
- **Token 对账**：对比 CE（计费系统）与 CloudWatch（监控系统）的 token 总量，验证两个系统数据一致性
- 对账口径为 UTC 日（CE DAILY 粒度按 UTC 解释）
- CloudWatch 查询的 Region 从 CE 账单的 USAGE_TYPE 前缀自动推导（账单里有哪些区域就查哪些，37 个前缀映射），无需手动配置
- 按模型拆分费用明细，每模型展示 5 种 token 类型：input / output / cache_read / cache_write / cache_write_1h
- 对账结果 + CE 原始明细 + CW 各 region 明细 全部存入 DynamoDB（90 天 TTL）
- 每日推送合并报告（两个日期的 token 对账差异 + 费用明细），不管差异多少都推
- **手动触发模式**：传入 `event.date` 可单独对账指定日期；`event.silent = true` 时不推送通知（Web Console 批量回填历史数据时使用，避免刷屏）

#### 对账原理与计算规则

```
═══════════════════════════════════════════════════════════════
时间线
═══════════════════════════════════════════════════════════════

  cron(0 1 * * ? *) = UTC 01:00 触发
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
  - 本月汇总卡片（本月费用、本月日均）
  - 费用趋势堆叠面积图（按模型着色）
  - Top 模型水平柱状图
  - 路由分类占比（cross-region / mantle / direct）（TODO）
- **历史对账**：
  - 按日期查看每模型费用 + token 用量（input/output/cache_read/cache_write/cache_write_1h 五类）
  - 可展开 CE 原始明细（每条 USAGE_TYPE 的 cost、quantity、unit）
  - 可展开 CW 各 region 明细（每 region 的 token 总量）
  - 对账指标卡片（总费用、模型数、CE/CW token 总量、对账差异百分比）
  - 历史回填功能：指定天数批量触发 reconciler 补录历史数据
- **今日监控**（滚动 24 小时窗口）：
  - **预估费用卡片**（最近 24h 预估总费用、Top 模型费用、未定价模型数）
  - **累计费用趋势线**（按 5 分钟粒度，滚动 24h，实时更新）
  - 每小时用量图表（模型维度堆叠，增量展示）
  - 每 5 分钟自动刷新
- **配置管理**：阈值、监控 Region 列表、Webhook 设置
- **版本管理**：
  - 基于 commit SHA 比对检测更新（当前部署 SHA vs GitHub 仓库分支最新 SHA，构建时自动固化，无需手动维护版本号）
  - 检查结果缓存 1 小时（DynamoDB），GitHub 不可达时回退过期缓存
  - 堆栈名称、最后部署时间、IP 白名单（读取 CloudFormation 栈参数）
  - 内置升级命令说明
- **⚠️ 安全提示**：本示例未实现用户认证，仅依赖 API Gateway Resource Policy 的 IP 白名单控制访问。如用于生产环境，请自行添加认证机制（如 Cognito、IAM Auth 等），避免管理界面暴露在公网

### 4. 通知推送

支持多渠道 Webhook（DDB `CONFIG#webhooks` 存 `items` 列表，每项含 name/url/type，最多 3 个）。旧的单条 `CONFIG#webhook` 格式仍兼容，读取时会自动迁移到新格式。渠道类型由 type 字段决定：

| 渠道 | type 值 | Payload 格式 |
|------|---------|-------------|
| 飞书 | feishu | `{msg_type: "text", content: {text: ...}}` |
| 钉钉 | dingtalk | `{msgtype: "text", text: {content: ...}}` |
| 企业微信 | wecom | `{msgtype: "text", text: {content: ...}}` |

发送行为：超时 10s，失败重试 1 次（间隔 1s）；未知 type 值 fallback 为飞书格式。

**日报推送策略**（DDB `CONFIG#notify_policy`）：

| 策略 | 行为 |
|------|------|
| `always`（默认） | 每天推送日报 |
| `workday` | 仅中国工作日推送日报（跳过法定假日和周末，调休上班日照常推送） |
| `never` | 不推送日报（对账数据照常写入 DynamoDB） |

- 工作日判断数据来源：[NateScarlet/holiday-cn](https://github.com/NateScarlet/holiday-cn)（自动跟踪国务院公告）
- **用量告警不受此策略影响，始终实时推送，不可关闭**（`never` 也只关日报，不关告警）
- 策略仅控制 reconciler 日报推送；对账数据无论是否推送都会正常写入 DynamoDB

### 5. IAM 权限扫描 — Bedrock 调用权限审计

被盗刷时第一步：快速定位账号内**谁能调用 Bedrock 产生费用**。

- Web Console 第 5 个 tab，点按钮即扫描
- 遍历所有 IAM Users / Roles / Groups 的托管策略 + 内联策略
- **只标记危险 Action**：`bedrock:Invoke*`、`bedrock:Converse*`、`bedrock:*`、`*` 等能产生调用的权限；过滤掉只读的 `List*/Get*`
- 显示每个身份的策略来源（哪个 Policy 授予的、是否通过 Group 继承）
- Role 附带信任关系（谁能 assume）；Group 附带成员列表
- 扫描结果持久化到 DynamoDB，无 TTL，直到下次点击扫描覆盖
- 异步执行（Lambda 自调用），支持 IAM 身份数百个的大型账号

### 6. Log Explorer — 调用日志查询（规划中）

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
| 定时触发 | EventBridge Rules | 5 分钟 / 每日 UTC 01:00 |
| 数据存储 | DynamoDB 单表 | 配置 + 阈值 + 对账记录 + 监控记录 + 告警状态 |
| Web 管理 | FastAPI + mangum + 原生 HTML/JS | API Gateway 代理到 Lambda |
| 访问控制 | API Gateway Resource Policy | IP 白名单（AllowedCidrs 参数） |
| 基础设施 | CloudFormation | 纯 serverless，无服务器管理 |
| 通知 | Webhook（DDB 配置） | 飞书 / 钉钉 / 企微 |
| 代码来源 | GitHub | 部署时自动从本仓库拉取代码，无需打包 |

## 成本估算

纯 Serverless，本身没有常驻资源，运行成本很低。下表为默认 10 个监控 Region、稳态运行下的**月成本预估**（按 AWS 计费单价与实测用量推算）：

| 组件 | 用量 | 月成本（约） | 说明 |
|------|------|------------|------|
| **CloudWatch GetMetricData** | ~2.5 万 metrics/天 | **~$7.5** | **主要成本**。monitor 每 5 分钟 × 10 Region 拉取 token 指标，按返回指标条数计费（$0.01/1000 条） |
| Cost Explorer API | 2 次/天 | ~$0.6 | reconciler 每日双日期对账，$0.01/次 |
| Lambda | ~9,000 次/月 | < $0.05 | monitor 288/天 + reconciler + web，基本在免费额度内 |
| DynamoDB（按需） | 极小读写 | < $0.05 | 记录带 TTL 自动过期，存量极小 |
| API Gateway | 按 Web 访问 | ≈ $0 | 仅管理界面访问，量小 |
| S3 | 部署代码包 | ≈ $0 | 几 MB |
| **合计** | | **~$8–9 / 月** | |

**成本几乎全部来自监控 Lambda 的 CloudWatch 拉取**，公式约为：

```
CloudWatch 成本 ≈ 轮询频率 × Region 数 × 各 Region 活跃模型数 × $0.01/1000
```

实测中 us-east-1 与 us-west-2 因模型最多，占了 GetMetricData 的大头（其余 8 个 Region 每个仅几百条/天）。

**如需进一步压缩成本：**
- **减少监控 Region**（Web Console 配置）：只保留实际调用 Bedrock 的区域，成本近似线性下降
- **拉长监控间隔**：默认 5 分钟，改成 10 分钟即可让 GetMetricData 成本减半（调整 EventBridge 规则的 `rate`）
- 对以 Bedrock 为主要支出的账号而言，这点管控开销通常只占被监控支出的极小比例，属于"费用兜底保险"的合理成本

> 注：具体金额随账号的 Region 数、模型数量浮动，请以自己账单为准。上表为 2026-07 实测用量下的代表值。

## 部署

整个栈是**自包含**的：CloudFormation 自动创建 S3 桶、从 GitHub 拉取代码、部署所有资源。**无需手动打包代码。**

> 💡 **获取你的公网 IP**：浏览器打开 https://checkip.amazonaws.com/ ，显示的即为你的出口 IP，填入 `AllowedCidrs` 时加上 `/32` 后缀。注意：如果使用 CloudShell 部署，不要在 CloudShell 里 curl 这个地址——那拿到的是 AWS 的 IP，不是你浏览器的。

### 方式一：CloudFormation 控制台上传（最快，无需 CLI / 无需 S3）

1. 下载模板文件 [`template.yaml`](https://raw.githubusercontent.com/huizi-2003/sample-cost-guard-for-amazon-bedrock/main/template.yaml)（右键另存，或 `curl -O`）。
2. 打开 [CloudFormation 控制台 → Create stack](https://console.aws.amazon.com/cloudformation/home#/stacks/create) → **With new resources (standard)**。
3. Specify template → **Upload a template file** → 选择刚下载的 `template.yaml`。
4. Stack name 填 `bedrock-cost-guard`。
5. 参数 `AllowedCidrs` 填你访问 Web Console 的公网 IP（如 `1.2.3.4/32`，多个用逗号分隔；默认 `127.0.0.1/32` 为全部关闭）。
6. 勾选 **"I acknowledge that AWS CloudFormation might create IAM resources"** → Create stack。
7. 等待 3-5 分钟，在 **Outputs** 标签找到 `WebConsoleUrl` 即为管理界面地址。

> 部署过程中模板会自动从 GitHub 下载 monitor/reconciler/web 代码，无需手动打包。

### 方式二：CLI 部署（CloudShell 或本地终端）

在 [CloudShell](https://console.aws.amazon.com/cloudshell/) 或本地终端（需安装 AWS CLI 并配置凭证）中执行：

```bash
# 克隆代码
git clone https://github.com/huizi-2003/sample-cost-guard-for-amazon-bedrock.git
cd sample-cost-guard-for-amazon-bedrock

# 部署（将 AllowedCidrs 改为你的出口 IP，多个用逗号分隔）
aws cloudformation deploy --template-file template.yaml --stack-name bedrock-cost-guard --parameter-overrides AllowedCidrs=1.2.3.4/32 Version=$(date +%s) --capabilities CAPABILITY_IAM

# 查看部署结果（获取 Web Console URL）
aws cloudformation describe-stacks --stack-name bedrock-cost-guard --query 'Stacks[0].Outputs'
```

> CloudShell 会话闲置 20 分钟会断开，但不影响 CloudFormation 部署（后台异步执行）。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `AllowedCidrs` | 允许访问 Web Console 的 CIDR 列表（逗号分隔）<br/>**变更时自动重新部署 API，Resource Policy 即时生效** | `127.0.0.1/32`（全部关闭） |
| `Version` | 代码版本标记，改动即触发重新拉代码 + 重新部署 Lambda | `1`（建议用 `$(date +%s)` 时间戳） |
| `GitHubOwner` | GitHub 仓库所有者（fork 时改为你的用户名） | `huizi-2003` |
| `GitHubRepo` | GitHub 仓库名 | `sample-cost-guard-for-amazon-bedrock` |
| `Branch` | 拉取代码的 Git 分支 | `main` |

> **⚠️ 关于 `Version`**：改了代码就改这个值（触发重新拉取 GitHub 代码），没改代码就保持不变。值本身不重要，只要和上次不同即可，用 `$(date +%s)` 自动生成时间戳最省事。

### 部署后

栈 Outputs 中的 `WebConsoleUrl` 即为管理界面地址（HTTPS），通过它配置：
- Webhook URL + 渠道类型（feishu / dingtalk / wecom）
- 监控 Region 列表
- 阈值

```bash
# 更新代码：push 后重新部署（Version 用时间戳自动变）
aws cloudformation deploy --template-file template.yaml --stack-name bedrock-cost-guard --parameter-overrides Version=$(date +%s) AllowedCidrs=1.2.3.4/32 --capabilities CAPABILITY_IAM

# 手动触发对账（不用等定时任务）
aws lambda invoke --function-name bedrock-cost-guard-reconciler --region us-east-1 /dev/null

# 删除所有资源
aws cloudformation delete-stack --stack-name bedrock-cost-guard
```

## 目录结构

```
bedrock-cost-guard/
├── README.md
├── DEPLOY-GUIDE.md        # 部署指南（快速上手）
├── template.yaml          # CloudFormation 模板（自包含：自动建桶 + 拉代码 + 部署）
├── common/
│   ├── __init__.py
│   ├── config.py          # DynamoDB 读写封装
│   ├── webhook.py         # 通知发送（飞书/钉钉/企微）
│   ├── holiday.py         # 中国工作日判断（节假日/调休）
│   ├── iam_scanner.py     # IAM Bedrock 权限扫描逻辑
│   ├── pricing.py         # 模型价格匹配与费用估算
│   ├── labels.py          # CloudWatch 指标 label 解析（模型名/token类型提取）
│   └── version.py         # 项目版本号定义
├── monitor/
│   ├── __init__.py
│   └── handler.py         # 用量监控 Lambda
├── reconciler/
│   ├── __init__.py
│   └── handler.py         # 每日对账 Lambda
├── web/
│   ├── app.py             # FastAPI 后端
│   ├── requirements.txt
│   └── static/
│       ├── index.html     # 管理页面（费用总览 + 历史对账 + 今日监控 + 配置管理）
│       └── chart.min.js   # Chart.js 图表库
├── tests/                 # 单元测试
│   ├── test_config.py
│   ├── test_monitor.py
│   ├── test_monitor_delta.py
│   ├── test_monitor_extended.py
│   ├── test_reconciler.py
│   ├── test_reconciler_extended.py
│   ├── test_web_api.py
│   ├── test_web_crossyear.py
│   ├── test_web_extended.py
│   ├── test_webhook.py
│   ├── test_iam_scanner.py
│   ├── test_pricing.py
│   ├── test_notify_policy.py
│   ├── test_labels.py
│   └── test_version.py
└── docs/                  # 设计文档
```

