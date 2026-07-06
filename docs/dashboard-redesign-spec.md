# Dashboard 报表重设计 — 实施规格

## 问题定义

当前 Bedrock Cost Guard 的 Web Dashboard 存在以下不足：

1. **缺乏总览视角**：进入就是单日对账表格，无法一眼看出费用趋势和整体规模
2. **无法感知异常**：日费用突增 30% 没有任何视觉提示，需要人肉翻日期对比
3. **对账页信息密度低**：纯表格，没有图表辅助理解费用结构
4. **今日监控无对比基线**：只有当日曲线，不知道"这个速率正不正常"
5. **无多日聚合能力**：看不到本月累计、本周趋势、模型占比变化

## 设计目标

为 AWS SA 提供 Bedrock 费用"驾驶舱"——30 秒内理解：钱花了多少、花在哪、趋势如何、是否异常。

**非目标**：不做多租户/用户维度拆分（与 litellm 定位不同），不改 Lambda/DDB schema。

## 方案概览

将 Tab 从 `[历史对账 | 今日监控 | 配置管理]` 改为：

```
[费用总览 | 历史对账 | 今日监控 | 配置管理]
```

### Tab 1：费用总览（新增，首屏）

```
┌─────────────────────────────────────────────────────────────┐
│  Summary Cards（4 个）                                       │
│  [本月累计 $847]  [昨日 $42]  [日均 $28]  [环比 ▲12%]       │
├─────────────────────────────────────────────────────────────┤
│  AreaChart: 最近 30 天日费用趋势（按模型堆叠分色）           │
├──────────────────────────┬──────────────────────────────────┤
│  Doughnut: 模型费用占比   │  Bar: Top 5 模型费用排名         │
└──────────────────────────┴──────────────────────────────────┘
```

### Tab 2：历史对账（增强）

- 顶部新增**日环比对比条**：`昨日 $42.15 → 前日 $38.70 (▲8.9%)`
- 保留现有模型×token 类型表格
- （可选后续）每模型行增加 7 日 sparkline

### Tab 3：今日监控（增强）

- 叠加**昨日同期虚线**作为对比基线
- 异常标记：当前小时速率 > 过去 7 天同时段均值 × 1.5 时标红提示

### Tab 4：配置管理（不变）

---

## API 设计

### 新增 API 1: `GET /api/reconcile/summary`

**用途**：聚合多日对账数据，返回总览所需的全部指标。

**参数**：
- `days` (int, default=30): 聚合天数

**响应**：

```json
{
  "period": {"start": "2026-06-01", "end": "2026-06-30", "days_with_data": 28},
  "totals": {
    "total_cost": 847.32,
    "daily_avg": 30.26,
    "yesterday_cost": 42.15,
    "day_before_cost": 38.70,
    "mom_change_pct": 12.3
  },
  "daily_costs": [
    {"date": "2026-06-01", "cost": 28.5, "models": {"claude-opus": 15.2, "claude-sonnet": 10.1, "other": 3.2}},
    ...
  ],
  "model_totals": [
    {"model": "claude-opus-cross-region-global", "cost": 380.5, "pct": 44.9},
    {"model": "claude-sonnet-cross-region-global", "cost": 297.2, "pct": 35.1},
    ...
  ],
  "routing_breakdown": [
    {"routing": "cross-region-global", "cost": 620.0, "pct": 73.2},
    {"routing": "direct", "cost": 150.0, "pct": 17.7},
    {"routing": "mantle", "cost": 77.3, "pct": 9.1}
  ]
}
```

**实现逻辑**：
1. 获取最近 N 天有数据的日期列表（`get_reconcile_dates`）
2. 逐日 query `RECONCILE#{date}` 的所有模型记录，累加 `actual_cost`
3. 按模型身份聚合 → `model_totals`
4. 从模型身份中提取 routing 后缀 → `routing_breakdown`
5. 计算环比：本月 vs 上月同期

### 新增 API 2: `GET /api/monitor/<date>/yesterday`

**用途**：返回前一天同日期的监控数据，用于叠加对比线。

**参数**：无额外参数，路径中的 date 自动推导 yesterday = date - 1 day

**响应**：与 `/api/monitor/<date>/models` 相同格式（模型级时间序列）

**实现逻辑**：
- 计算 yesterday_date = date - 1 day
- 复用 `monitor_models()` 逻辑查询 yesterday 的 CW 数据
- 如果 yesterday 有 DDB `MONITOR#` 缓存数据则优先用缓存（减少 CW API 调用）

---

## 前端设计

### 技术选型

- 继续使用 Chart.js（已有 `static/chart.min.js`，无 CDN 依赖）
- 原生 HTML/CSS/JS（与现有风格一致）
- 新增图表类型：Doughnut（Chart.js 内置）、Stacked Area（line + fill + stacked）

### 费用总览页布局

```html
<div id="overview" class="panel active">
  <!-- Summary Cards -->
  <div class="summary" id="overview-summary">...</div>

  <!-- 30 天趋势 AreaChart -->
  <div class="chart-container" style="height:300px">
    <canvas id="trend-chart"></canvas>
  </div>

  <!-- 底部两栏 -->
  <div class="row">
    <div class="col-half">
      <canvas id="model-pie"></canvas>  <!-- Doughnut -->
    </div>
    <div class="col-half">
      <canvas id="model-bar"></canvas>  <!-- Horizontal Bar -->
    </div>
  </div>
</div>
```

### 对账页增强

```html
<!-- 新增：日环比对比条 -->
<div id="reconcile-compare" class="compare-bar">
  <span>昨日 <strong>$42.15</strong></span>
  <span class="arrow up">▲ 8.9%</span>
  <span>前日 $38.70</span>
</div>
```

### 监控页增强

Chart.js 配置中增加第二个 dataset（虚线，昨日数据）：

```javascript
{
  label: '昨日同期',
  data: yesterdayData,
  borderDash: [5, 5],
  borderColor: 'rgba(150,150,150,0.6)',
  fill: false,
}
```

---

## 任务拆分与验收标准

| # | 任务 | 验收标准 |
|---|------|---------|
| 1 | 后端 `/api/reconcile/summary` | 返回上述 JSON 结构；days=7 时 < 2s 响应 |
| 2 | 后端 `/api/monitor/<date>/yesterday` | 返回昨日模型时间序列；格式与 models API 一致 |
| 3 | 前端 Tab 重构 + 费用总览页 | 首屏显示 4 个 summary cards + 趋势图 + 饼图 + 柱图 |
| 4 | 前端对账页增强 | 顶部显示日环比对比条，涨跌用颜色区分 |
| 5 | 前端监控页增强 | 图表叠加昨日虚线；速率异常时顶部显示红色提示 |
| 6 | 本地验证 | `python web/app.py` 启动后所有 Tab 正常渲染，无 JS 报错 |

## 风险与约束

- **DDB Scan 性能**：`get_reconcile_dates()` 目前用 scan，30 天数据量 OK，但 365 天可能慢。后续可加 GSI 或改用 query。
- **CW API 限流**：yesterday 数据建议优先用 DDB `MONITOR#` 缓存，只在缓存无数据时 fallback 到实时 CW 查询。
- **Chart.js 版本**：确认本地 `chart.min.js` 版本支持 stacked area 和 doughnut（Chart.js 2.x+ 即可）。
