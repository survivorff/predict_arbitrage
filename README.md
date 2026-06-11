# Prediction Market Arbitrage Scanner

实时从多个预测市场平台（**Polymarket** 与 **predict.fun**）摄取行情，规范化为统一模型，
跨平台匹配等价市场，检测计入费用与价差的套利机会，把机会跟踪为有状态的「信号」，
并通过只读 API 与告警呈现；内置模拟盘双腿执行，可在不动真钱的情况下演练交易链路。

- **版本**：v0.2.0（第一版可交付：信号检测 + 模拟盘执行；真实资金下单尚未启用）
- **完整版本说明 / 使用指南**：[`docs/版本说明-v0.2.0.md`](./docs/版本说明-v0.2.0.md)
- **工程文档**：[`docs/`](./docs/)（架构、技术方案、模块、API、测试、部署、迭代计划、变更日志）
- **规格**：`.kiro/specs/prediction-market-arbitrage-scanner/`（需求 / 设计 / 任务）

## 安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## 先跑一遍演示（无需联网、无需密钥）

```bash
.venv/bin/python demo.py
```

会用伪行情驱动 Polymarket + predict.fun，打印 行情 → 跨平台匹配 → 套利 → 信号 →
只读 API → 模拟盘双腿执行 的全过程。

## 启动服务

默认配置 [`config.yaml`](./config.yaml) **开箱即用**：启用 Polymarket + predict.fun
（两者均无需密钥即可读公开行情），Kalshi 暂缓禁用。

```bash
# 默认加载 ./config.yaml；后台自动摄取 + 周期检测
.venv/bin/python -m uvicorn scanner.app:app --host 127.0.0.1 --port 8000

# 指定配置文件
SCANNER_CONFIG=/path/to/my-config.yaml .venv/bin/python -m uvicorn scanner.app:app
```

查询端点（也可访问 `/docs` 看 OpenAPI）：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/opportunities
curl -s http://127.0.0.1:8000/signals
curl -s http://127.0.0.1:8000/metrics
```

> **安全提示**：只读 API **无鉴权**，是只读接口且**不执行真实交易**。不要直接暴露到
> 不可信网络——请置于带认证的反向代理 / 网关之后。Kalshi 交易暂缓（等风控）；真实链上
> 下单需钱包私钥与资金，为后续单独的一步。

## 运行测试

```bash
.venv/bin/python -m pytest      # 634 个测试，离线确定性运行
```
