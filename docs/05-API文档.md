# 05 · API 文档

读取 API 由 FastAPI 提供，是用户与未来聚合器消费数据的稳定接口（需求 7.4）。FastAPI 自带 OpenAPI 文档，启动后可访问 `/docs`（Swagger UI）与 `/openapi.json`。

> ⚠️ **安全提示**：第一阶段 API **无鉴权**，是只读接口。生产环境务必在前置加认证网关，详见 [07-部署与运维.md](./07-部署与运维.md)。

## 端点总览

| 方法 | 路径 | 用途 | 对应需求 |
|---|---|---|---|
| GET | `/markets` | 已排名市场，支持排序与阈值过滤 | 3.3, 3.4, 3.5, 8.1 |
| GET | `/markets/{platform}/{market_id}` | 单个市场盘口明细 | Phase 3 · 切片 K |
| GET | `/groups` | 等价市场组，支持置信度过滤 | 4.4, 4.5 |
| GET | `/groups/{group_id}` | 单个匹配组明细 | Phase 3 · 切片 K |
| GET | `/opportunities` | 套利机会，按净利润率降序 | 5.5, 6.1, 8.1 |
| GET | `/opportunities/{group_id}/history` | 某机会组净利润率历史时间序列 | Phase 3 · 切片 K |
| GET | `/markets/{platform}/{market_id}/history` | 某市场 YES 价历史时间序列 | Phase 3 · 切片 K |
| GET | `/signals` | 当前活跃信号（含状态/峰值/持续时长） | Phase 2 · 切片 A |
| GET | `/signals/{group_id}` | 单个活跃信号明细 | Phase 3 · 切片 K |
| GET | `/signals/events` | 信号事件流（OPENED/UPDATED/CLOSED） | Phase 2 · 切片 A |
| GET | `/health` | 各适配器健康状态与计数 | 1.5 |
| GET | `/metrics` | 流水线运行指标快照（周期数、信号开关累计、最近周期各项计数） | Phase 2 · 切片 D |
| GET | `/trade/plans` | 交易计划列表（半自动确认流），可按状态过滤 | Phase 3 · 切片 H |
| GET | `/trade/plans/{plan_id}` | 单个交易计划明细 | Phase 3 · 切片 H |
| POST | `/trade/plans/{plan_id}/confirm` | 人工确认计划并按 dry-run 执行 | Phase 3 · 切片 H |
| POST | `/trade/plans/{plan_id}/reject` | 人工拒绝计划 | Phase 3 · 切片 H |
| GET | `/trade/balance` | 各平台执行账户可用余额（模拟盘） | Phase 3 · 切片 H/I |
| GET | `/trade/positions` | 各平台当前持仓 | Phase 3 · 切片 H/I |
| GET | `/trade/exposure` | 当前敞口快照（总 + 各市场） | Phase 3 · 切片 H |
| GET | `/trade/onchain-balance` | 真实链上 USDC 余额（只读，需配 RPC+地址） | Phase 3 · 切片 I |
| GET | `/trade/pnl` | 已执行计划真实盈亏汇总（实际成交价） | Phase 3 · 切片 H/I |
| GET | `/` | 可视化仪表盘（单页 HTML，定时轮询上述 API） | Phase 3 |
| GET | `/config/alerts` | 查看告警标准 | 6.2 |
| PUT | `/config/alerts` | 更新告警标准（内存） | 6.2 |

所有市场 / 机会响应都包含新鲜度字段 `data_age_seconds`（数据年龄，秒）与 `is_stale`（是否过期）。

---

## GET /markets

返回按指定指标排名的市场列表。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sort` | `volume` \| `liquidity` | `volume` | 排序指标，非法值返回 422 |
| `min_volume` | float ≥ 0 | 无 | 最小交易量过滤 |
| `min_liquidity` | float ≥ 0 | 无 | 最小流动性过滤 |

**响应示例**

```json
[
  {
    "platform": "polymarket",
    "market_id": "btc-100k",
    "title": "Will Bitcoin close above 100000 in 2025?",
    "outcomes": [
      {"name": "YES", "price": 0.4, "bid": 0.39, "ask": 0.4, "available_liquidity_usd": 1500.0},
      {"name": "NO",  "price": 0.62, "bid": 0.61, "ask": 0.62, "available_liquidity_usd": 1500.0}
    ],
    "volume_usd": 2000000.0,
    "liquidity_usd": 1500.0,
    "fee_rate": 0.0,
    "retrieved_at": "2026-06-08T01:22:03.341488Z",
    "is_stale": false,
    "data_age_seconds": 3.77
  }
]
```

指标不可用（None）的市场排在最后；被激活的阈值会排除该指标不可用的市场。

---

## GET /markets/{platform}/{market_id}

返回单个市场的盘口明细（Phase 3 · 切片 K），供仪表盘行点开看各结果的 bid/ask/流动性。响应结构同 `/markets` 列表中的单条 `MarketResponse`。**市场不存在时返回 404**。

---

## GET /groups

返回匹配引擎识别出的等价市场组。

**查询参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `min_confidence` | float [0,1] | 仅返回置信度 ≥ 该值的组 |

**响应示例**

```json
[
  {
    "group_id": "kalshi:BTC-100K|polymarket:btc-100k",
    "members": [ /* MarketResponse 数组 */ ],
    "outcome_map": [
      {"canonical_outcome": "YES",
       "platform_outcomes": {"polymarket": "YES", "kalshi": "YES"},
       "inverted": {"polymarket": false, "kalshi": false}},
      {"canonical_outcome": "NO", "platform_outcomes": {"polymarket": "NO", "kalshi": "NO"},
       "inverted": {"polymarket": false, "kalshi": false}}
    ],
    "match_confidence": 0.95
  }
]
```

---

## GET /groups/{group_id}

返回单个匹配组的明细（Phase 3 · 切片 K），含各成员市场盘口与结果映射。响应结构同 `/groups` 列表中的单条 `GroupResponse`。**组不存在时返回 404**。

---

## GET /opportunities

返回当前套利机会，**按 `net_profit_margin` 降序，且不含 ≤0 的机会**（Property 10）。

**查询参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `min_margin` | float | 进一步只返回净利润率 ≥ 该值的机会 |

**响应示例**

```json
[
  {
    "group_id": "kalshi:BTC-100K|polymarket:btc-100k",
    "event_title": "Will Bitcoin close above 100000 in 2025?",
    "legs": [
      {"platform": "polymarket", "market_id": "btc-100k", "outcome": "YES", "side": "buy", "price": 0.4},
      {"platform": "kalshi", "market_id": "BTC-100K", "outcome": "NO", "side": "buy", "price": 0.45}
    ],
    "net_profit_margin": 0.1494,
    "recommended_size_usd": 800.0,
    "detected_at": "2026-06-08T01:22:03.341488Z",
    "data_age_seconds": 0.0,
    "is_stale": false
  }
]
```

字段说明：
- `legs` — 套利各腿，每条说明在哪个平台买哪个结果、价格多少。
- `net_profit_margin` — 净利润率（计入费用与价差）。
- `recommended_size_usd` — 受最薄一腿流动性限制的建议规模。
- `is_stale` — 当 `data_age_seconds > staleness_threshold` 时为 true。

---

## GET /opportunities/{group_id}/history

返回某机会组的**净利润率历史时间序列**（Phase 3 · 切片 K），供仪表盘画价差/利润率走势。**未配置历史存储（`history_store_path`）时返回空列表 `[]`**。

**查询参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `limit` | int | 返回最近 N 个点（默认 500，范围 1–5000） |

**响应示例**（按时间升序）

```json
[
  {"value": 0.1494, "at": "2026-06-08T01:22:03.341488+00:00", "label": "Will Bitcoin close above 100000 in 2025?"},
  {"value": 0.1521, "at": "2026-06-08T01:22:33.500102+00:00", "label": "Will Bitcoin close above 100000 in 2025?"}
]
```

---

## GET /markets/{platform}/{market_id}/history

返回某市场的 **YES 价历史时间序列**（Phase 3 · 切片 K），用于回看价格变动。**未配置历史存储时返回空列表 `[]`**。查询参数与响应结构同上（`value` 为 YES 价）。

---

## GET /signals

返回当前**活跃**的套利信号（Phase Two · 切片 A）。信号是有状态的机会：记录从出现到消失的生命周期。按 `net_profit_margin` 降序返回。

**响应示例**

```json
[
  {
    "group_id": "kalshi:BTC-100K|polymarket:btc-100k",
    "event_title": "Will Bitcoin close above 100000 in 2025?",
    "status": "sustained",
    "legs": [
      {"platform": "polymarket", "market_id": "btc-100k", "outcome": "YES", "side": "buy", "price": 0.4},
      {"platform": "kalshi", "market_id": "BTC-100K", "outcome": "NO", "side": "buy", "price": 0.45}
    ],
    "net_profit_margin": 0.1494,
    "recommended_size_usd": 800.0,
    "peak_net_profit_margin": 0.1612,
    "data_age_seconds": 0.0,
    "first_detected_at": "2026-06-08T12:00:00Z",
    "last_seen_at": "2026-06-08T12:01:00Z",
    "closed_at": null,
    "duration_seconds": 60.0
  }
]
```

字段说明：
- `status` — 信号状态：`open`（首次检测）/ `sustained`（持续存在）/ `closed`（已关闭，不出现在活跃列表）。
- `peak_net_profit_margin` — 信号存活期间观测到的峰值净利润率（单调不减）。
- `first_detected_at` / `last_seen_at` / `closed_at` — 生命周期时间戳。
- `duration_seconds` — 已持续时长（首次检测到最近一次出现）。

---

## GET /signals/{group_id}

返回单个活跃信号的明细（Phase 3 · 切片 K）。响应结构同 `/signals` 列表中的单条 `SignalResponse`。**信号存储未配置、或该 group 无活跃信号时返回 404**。

> 路由声明在 `/signals/events` 之后，避免静态路径 `events` 被路径参数 `{group_id}` 遮蔽。

---

## GET /signals/events

返回信号事件流，按发生顺序排列。事件是信号工具的核心产物，可被消费、回放、审计。

**查询参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `limit` | int ≥ 1 | 仅返回最近的 N 个事件 |

**响应示例**

```json
[
  {"event_type": "opened",  "group_id": "kalshi:BTC-100K|polymarket:btc-100k",
   "event_title": "Will Bitcoin close above 100000 in 2025?", "status": "open",
   "net_profit_margin": 0.1494, "recommended_size_usd": 800.0,
   "peak_net_profit_margin": 0.1494, "duration_seconds": 0.0,
   "occurred_at": "2026-06-08T12:00:00Z"},
  {"event_type": "closed", "group_id": "kalshi:BTC-100K|polymarket:btc-100k",
   "event_title": "Will Bitcoin close above 100000 in 2025?", "status": "closed",
   "net_profit_margin": 0.1494, "recommended_size_usd": 800.0,
   "peak_net_profit_margin": 0.1612, "duration_seconds": 120.0,
   "occurred_at": "2026-06-08T12:02:00Z"}
]
```

- `event_type` — `opened` / `updated` / `closed`，与信号状态转移一一对应。

---

## GET /health

返回服务整体状态与各适配器健康情况。

**响应示例**

```json
{
  "status": "ok",
  "market_count": 2,
  "opportunity_count": 1,
  "adapters": [
    {"name": "kalshi", "healthy": true, "market_count": 1,
     "last_successful_cycle": "2026-06-08T01:22:03.341488Z", "last_error": null},
    {"name": "polymarket", "healthy": true, "market_count": 1,
     "last_successful_cycle": "2026-06-08T01:22:03.341488Z", "last_error": null}
  ]
}
```

`status` 在所有上报适配器都健康时为 `ok`，否则为 `degraded`。

每个适配器项还可能包含数据接入韧性指标（Phase Two · 切片 C，仅当适配器被 `ResilientAdapter` 包装时）：
- `circuit_state` — 熔断状态：`closed` / `open` / `half_open`（`open` 视为不健康）。
- `success_count` / `failure_count` — 累计成功/失败次数。

---

## GET /metrics

返回流水线运行指标快照（Phase Two · 切片 D）。指标在每个检测周期结束时更新：累计量（`*_total`）单调不减，`last_*` 反映最近一次周期。**未配置 metrics 时返回空对象 `{}`**。

**响应示例**

```json
{
  "cycles_total": 12,
  "signals_opened_total": 5,
  "signals_closed_total": 3,
  "alerts_fired_total": 4,
  "last_cycle_at": "2026-06-08T01:22:03.341488+00:00",
  "last_cycle_duration_seconds": 0.042,
  "last_markets_count": 2,
  "last_groups_count": 1,
  "last_opportunities_count": 1,
  "active_signals_count": 2
}
```

字段说明：
- `cycles_total` — 累计已运行的检测周期数。
- `signals_opened_total` / `signals_closed_total` — 累计开启 / 关闭的信号数。
- `alerts_fired_total` — 累计触发的告警数。
- `last_cycle_at` — 最近一次周期完成时间（ISO 8601 UTC）；尚未运行任何周期时为 `null`。
- `last_cycle_duration_seconds` — 最近一次周期的耗时（秒）。
- `last_markets_count` / `last_groups_count` / `last_opportunities_count` — 最近一次周期的市场 / 等价组 / 机会数量。
- `active_signals_count` — 当前活跃信号数。

---

## 交易 API（半自动确认流 · Phase 3 · 切片 H）

> ⚠️ **安全**：这些端点会改变交易计划状态，确认时会触发执行。系统默认 **dry-run**（由 `config.yaml` 的 `risk.dry_run` 决定）且仅接入**模拟盘执行适配器**，因此不会动真钱。真实下单需接入真实执行适配器（切片 I）、显式关闭 dry-run，并为交易 API 加鉴权（硬前置）。**未配置交易服务时，列表返回 `[]`，其余返回 404。**
>
> 🔐 **鉴权**：配置 `scanner.trade_api_key_env`（指向一个环境变量名）后，所有交易端点要求在 `X-API-Key` 头携带正确密钥，否则 **401**；未配置时交易端点不鉴权（本机自用，与只读 API 一致）。只读端点始终不受此鉴权影响。密钥只经环境变量注入，绝不写入配置文件/日志/响应。

### GET /trade/plans

列出交易计划，按创建时间升序。

**查询参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `status` | string | 按状态过滤：`pending_confirmation` / `confirmed` / `executing` / `completed` / `failed` / `rejected`，非法值返回 422 |

**响应示例**

```json
[
  {
    "plan_id": "plan-1",
    "group_id": "polymarket:btc|predictfun:1471",
    "event_title": "Will Bitcoin close above 100000 in 2025?",
    "legs": [
      {"platform": "polymarket", "market_id": "btc", "outcome": "YES", "side": "buy",
       "target_price": 0.40, "quantity": 250.0, "order_id": null},
      {"platform": "predictfun", "market_id": "1471", "outcome": "NO", "side": "buy",
       "target_price": 0.45, "quantity": 222.2, "order_id": null}
    ],
    "expected_net_profit_margin": 0.15,
    "size_usd": 100.0,
    "status": "pending_confirmation",
    "created_at": "2026-06-09T02:00:00Z",
    "updated_at": "2026-06-09T02:00:00Z",
    "notes": "[风控通过 · DRY-RUN · 需人工确认] 核准规模 $100.00；通过检查：..."
  }
]
```

### GET /trade/plans/{plan_id}

返回单个交易计划明细（结构同上）。计划不存在或未配置交易服务返回 404。

### POST /trade/plans/{plan_id}/confirm

人工确认一个待确认计划，随即按 dry-run 标志执行，返回更新后的计划。
- 计划不存在 → 404；计划非待确认状态 → 409。
- dry-run 下计划置 `completed`，`notes` 以 `[DRY-RUN]` 标注「将要下的单」，不动真钱。

### POST /trade/plans/{plan_id}/reject

人工拒绝一个待确认计划，计划置 `rejected`。计划不存在 → 404；非待确认状态 → 409。

---

## GET / PUT /config/alerts

查看或更新告警标准（更新仅作用于内存，进程重启失效）。

**GET 响应 / PUT 请求体**

```json
{
  "min_net_profit_margin": 0.02,
  "min_match_confidence": 0.8,
  "platforms": ["polymarket"]
}
```

- `min_net_profit_margin` — 触发告警的最低净利润率。
- `min_match_confidence` — 可选，最低匹配置信度。
- `platforms` — 可选，平台允许列表（机会的所有腿都在列表内才告警）。

---

## 调用示例

```bash
curl -s http://127.0.0.1:8000/health
curl -s "http://127.0.0.1:8000/markets?sort=volume&min_volume=100000"
curl -s "http://127.0.0.1:8000/opportunities?min_margin=0.05"
curl -s -X PUT http://127.0.0.1:8000/config/alerts \
  -H "Content-Type: application/json" \
  -d '{"min_net_profit_margin": 0.05}'
```
