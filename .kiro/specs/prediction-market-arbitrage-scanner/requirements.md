# Requirements Document

## Introduction

预测市场套利扫描器（Prediction Market Arbitrage Scanner）是一个系统，它实时从多个预测市场平台（如 Polymarket 和 Kalshi）摄取市场数据，识别交易量和流动性最高的市场与主题，跨平台匹配等价的市场和结果，并检测跨平台套利机会——在这些机会中，价格差异（在计入费用和价差后）能够带来有保证或近乎有保证的利润。系统通过排序视图和可配置的告警向用户呈现这些机会。

本文档涵盖第一阶段（Phase One）：实时摄取、市场排名、跨平台匹配、套利检测，以及机会呈现/告警。架构必须保持可扩展性，以便未来的阶段（预测聚合器、更多平台、自动化执行）能够在无需返工的情况下加入。本文档定义系统必须做什么；技术实现选择留待设计阶段决定。

## Glossary

- **Scanner**：本文档所描述的整个预测市场套利扫描器系统。
- **Platform**：Scanner 从中摄取数据的外部预测市场服务（例如 Polymarket 或 Kalshi）。
- **Platform_Adapter**：连接到一个 Platform、检索市场数据并将该数据规范化为 Canonical_Market 格式的 Scanner 组件。
- **Ingestion_Service**：负责从所有已配置的 Platform_Adapter 检索和刷新市场数据的 Scanner 组件。
- **Canonical_Market**：来自单个 Platform 的某个市场的规范化表示，包括标题、结果、价格、交易量、流动性、费用和时间戳。
- **Outcome**：Canonical_Market 内单个可交易的结果（例如 "Yes" 或 "No"），并带有相关联的价格。
- **Market_Price**：某个 Outcome 在某 Platform 上的当前价格，以介于 0 和 1 之间的隐含概率表示。
- **Volume**：Canonical_Market 在某一确定时间段内的累计成交价值，以美元表示。
- **Liquidity**：在 Canonical_Market 当前 Market_Price 或其附近可供交易的价值量，以美元表示。
- **Matching_Engine**：判定来自不同 Platform 的 Canonical_Market 是否代表同一现实世界事件及结果集的 Scanner 组件。
- **Equivalent_Market_Group**：由来自不同 Platform 的两个或更多 Canonical_Market 组成的集合，Matching_Engine 已判定它们代表同一现实世界事件，并在它们的 Outcome 之间建立映射。
- **Match_Confidence**：介于 0 和 1 之间的数值评分，表示 Matching_Engine 对某个 Equivalent_Market_Group 中的 Canonical_Market 代表同一事件的确定程度。
- **Arbitrage_Engine**：评估 Equivalent_Market_Group 中能够产生利润的价格差异的 Scanner 组件。
- **Arbitrage_Opportunity**：在某个 Equivalent_Market_Group 内检测到的一种状况——跨 Platform 买入互补的 Outcome 能够产生正的 Net_Profit_Margin。
- **Net_Profit_Margin**：某个 Arbitrage_Opportunity 的预期利润，在扣除 Platform 费用和跨越价差的成本后，以所投入资本的百分比表示。
- **Alert**：当某个 Arbitrage_Opportunity 满足用户所配置的标准时，向用户发送的通知。
- **User**：使用 Scanner 查看已排名市场和 Arbitrage_Opportunity 并配置 Alert 的人。
- **Staleness_Threshold**：Market_Price 数据在被视为过期之前所允许的最大存续时间（以秒为单位）。

## Requirements

### 需求 1：实时市场摄取

**User Story:** 作为一名交易者，我希望 Scanner 持续从多个预测市场平台摄取市场数据，以便我能够基于当前价格评估机会。

#### 验收标准

1. THE Ingestion_Service SHALL 从每个已配置的 Platform_Adapter 检索市场数据。
2. WHEN 从某个 Platform 检索到市场数据时，THE Ingestion_Service SHALL 将该数据规范化为 Canonical_Market 记录。
3. THE Ingestion_Service SHALL 以 60 秒或更短的间隔刷新活跃 Canonical_Market 的 Market_Price 数据。
4. WHEN 某个 Canonical_Market 被创建或更新时，THE Ingestion_Service SHALL 记录检索到该数据的时间戳。
5. IF 某个 Platform_Adapter 未能在 30 秒内返回数据，THEN THE Ingestion_Service SHALL 记录该失败并继续从其余 Platform_Adapter 摄取数据。
6. IF 某个 Canonical_Market 的 Market_Price 数据超过 Staleness_Threshold，THEN THE Scanner SHALL 将该 Canonical_Market 标记为过期，并将其从 Arbitrage_Opportunity 检测中排除。

### 需求 2：市场数据规范化

**User Story:** 作为一名系统集成人员，我希望将每个平台的数据规范化为单一的规范格式，以便能够一致地比较来自不同平台的市场。

#### 验收标准

1. WHEN 某个 Platform_Adapter 生成一个 Canonical_Market 时，THE Canonical_Market SHALL 包含标题、Platform 标识符、Outcome 列表、每个 Outcome 的 Market_Price、Volume 值、Liquidity 值、适用的费率，以及检索时间戳。
2. THE Platform_Adapter SHALL 将每个 Market_Price 表示为介于 0 和 1（含）之间的隐含概率。
3. THE Platform_Adapter SHALL 以美元表示 Volume 和 Liquidity 值。
4. IF 某个必需字段在 Platform 源数据中缺失，THEN THE Platform_Adapter SHALL 将对应的 Canonical_Market 字段标记为不可用，并记录原因。
5. WHERE 某个 Platform 提供费用信息，THE Platform_Adapter SHALL 根据该信息填充 Canonical_Market 的费率。

### 需求 3：最高交易量与最高流动性市场识别

**User Story:** 作为一名交易者，我希望看到交易量和流动性最高的市场与主题，以便我能够聚焦于那些我实际能够大规模交易的机会。

#### 验收标准

1. THE Scanner SHALL 按 Volume 降序对 Canonical_Market 排名。
2. THE Scanner SHALL 按 Liquidity 降序对 Canonical_Market 排名。
3. WHEN User 请求市场列表时，THE Scanner SHALL 按 User 所选的排名标准返回排序后的 Canonical_Market。
4. WHERE User 指定了最小 Volume 阈值，THE Scanner SHALL 从返回的列表中排除 Volume 低于该阈值的 Canonical_Market。
5. WHERE User 指定了最小 Liquidity 阈值，THE Scanner SHALL 从返回的列表中排除 Liquidity 低于该阈值的 Canonical_Market。

### 需求 4：跨平台市场匹配

**User Story:** 作为一名交易者，我希望 Scanner 能够识别出不同平台上的市场何时指向同一现实世界事件，以便我能够比较它们的价格以进行套利。

#### 验收标准

1. THE Matching_Engine SHALL 评估来自不同 Platform 的 Canonical_Market，以判定它们是否代表同一现实世界事件。
2. WHEN Matching_Engine 判定来自不同 Platform 的两个或更多 Canonical_Market 代表同一现实世界事件时，THE Matching_Engine SHALL 将它们分组为一个 Equivalent_Market_Group。
3. WHEN Matching_Engine 创建一个 Equivalent_Market_Group 时，THE Matching_Engine SHALL 在被分组的 Canonical_Market 的对应 Outcome 之间生成一个映射。
4. WHEN Matching_Engine 创建一个 Equivalent_Market_Group 时，THE Matching_Engine SHALL 为该组分配一个介于 0 和 1 之间的 Match_Confidence。
5. WHERE User 指定了最小 Match_Confidence 阈值，THE Scanner SHALL 从 Arbitrage_Opportunity 检测中排除 Match_Confidence 低于该阈值的 Equivalent_Market_Group。
6. IF Matching_Engine 无法将某个 Canonical_Market 的每个 Outcome 都映射到候选组中的某个 Outcome，THEN THE Matching_Engine SHALL 将该 Canonical_Market 从 Equivalent_Market_Group 中排除。

### 需求 5：套利机会检测

**User Story:** 作为一名交易者，我希望 Scanner 检测跨平台能够产生利润的价格差异，以便我能够在有保证或近乎有保证的回报上采取行动。

#### 验收标准

1. WHEN 某个 Equivalent_Market_Group 的已映射 Outcome 具有当前的、非过期的 Market_Price 数据时，THE Arbitrage_Engine SHALL 评估该组是否存在 Arbitrage_Opportunity。
2. WHEN Arbitrage_Engine 评估某个 Equivalent_Market_Group 时，THE Arbitrage_Engine SHALL 在扣除 Platform 费用和跨越价差的成本后计算 Net_Profit_Margin。
3. WHEN Arbitrage_Engine 完成对某个 Equivalent_Market_Group 的评估时，THE Arbitrage_Engine SHALL 为该组记录一个 Arbitrage_Opportunity，包括 Net_Profit_Margin 为 0 或以下的机会，这些机会随后会按照需求 6.4 从机会列表中移除。
4. WHEN Arbitrage_Engine 记录一个 Arbitrage_Opportunity 时，THE Arbitrage_Opportunity SHALL 包含 Net_Profit_Margin、在每个 Platform 上需买入的 Outcome、相关联的 Market_Price，以及检测时间戳。
5. WHERE User 指定了最小 Net_Profit_Margin 阈值，THE Arbitrage_Engine SHALL 从向 User 呈现的结果中排除低于该阈值的 Arbitrage_Opportunity。
6. THE Arbitrage_Engine SHALL 将某个 Arbitrage_Opportunity 的建议交易规模限制在 Equivalent_Market_Group 中各 Outcome 的可用 Liquidity 范围内。

### 需求 6：机会呈现与告警

**User Story:** 作为一名交易者，我希望查看已排名的套利机会，并在符合条件的机会出现时收到告警，以便我能够在价差消失之前采取行动。

#### 验收标准

1. WHEN User 请求机会列表时，THE Scanner SHALL 返回当前的 Arbitrage_Opportunity，并按 Net_Profit_Margin 降序排序。
2. WHERE User 已配置 Alert 标准，WHEN 一个新检测到的 Arbitrage_Opportunity 满足这些标准时，THE Scanner SHALL 向 User 发送一个 Alert。
3. WHEN Scanner 发送一个 Alert 时，THE Alert SHALL 包含匹配到的事件、参与的 Platform、Net_Profit_Margin，以及检测时间戳。
4. WHEN 某个 Arbitrage_Opportunity 因其 Net_Profit_Margin 已降至 0 或以下而不再有效时，THE Scanner SHALL 将该 Arbitrage_Opportunity 从机会列表中移除。
5. IF Alert 发送失败，THEN THE Scanner SHALL 记录该失败并重试发送最多 3 次。

### 需求 7：平台配置与可扩展性

**User Story:** 作为一名系统运维人员，我希望通过配置来添加或移除受支持的平台，以便未来的阶段能够纳入新平台而无需重建系统。

#### 验收标准

1. THE Scanner SHALL 在启动时从配置中加载活跃 Platform_Adapter 的集合。
2. WHERE 一个新的 Platform_Adapter 符合 Platform_Adapter 接口，THE Scanner SHALL 在不修改 Ingestion_Service 的情况下从该 Platform_Adapter 摄取数据。
3. WHEN 某个 Platform_Adapter 在配置中被禁用时，THE Scanner SHALL 将该 Platform 的 Canonical_Market 从摄取、匹配和检测中排除。
4. THE Scanner SHALL 通过一个已定义的接口暴露 Canonical_Market 和 Arbitrage_Opportunity 数据，以便未来的聚合功能能够消费这些数据。

### 需求 8：数据新鲜度与可靠性

**User Story:** 作为一名交易者，我希望确信我所看到的机会是基于新鲜数据的，以免我对已不复存在的价差采取行动。

#### 验收标准

1. WHEN Scanner 向 User 呈现一个 Canonical_Market 或 Arbitrage_Opportunity 时，THE Scanner SHALL 显示底层 Market_Price 数据的存续时间。
2. IF 某个 Equivalent_Market_Group 中的任一 Outcome 的 Market_Price 数据超过 Staleness_Threshold，THEN THE Arbitrage_Engine SHALL 将该 Equivalent_Market_Group 从 Arbitrage_Opportunity 检测中排除。
3. THE Scanner SHALL 应用 60 秒的默认 Staleness_Threshold。
4. WHERE User 指定了 Staleness_Threshold，THE Scanner SHALL 应用 User 指定的值以替代默认值。
