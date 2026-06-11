"""第一版完整流程演示（无需真实资金）。

用伪适配器驱动 Polymarket + predict.fun 两个平台，跑通整条信号流水线，并通过
真实 HTTP API（FastAPI TestClient）展示每个端点的输出：

    行情摄取 → 跨平台金标准匹配 → 套利检测 → 信号生命周期 → 模拟盘双腿执行
    + 只读 API（/markets /groups /opportunities /signals /metrics /health）

不联网、不动真钱。真实链上下单是后续单独的一步（需钱包私钥与资金）。

运行：  .venv/bin/python demo.py
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from scanner.app import build_application
from scanner.config import load_config_from_dict
from scanner.execution import PaperExecutionAdapter
from scanner.execution_engine import ExecutionEngine
from scanner.models import CanonicalMarket, Outcome, TradePlanStatus
from tests.adapter_contract import FakeAdapter

NOW = datetime.now(timezone.utc)
EVENT = "Will Bitcoin close above 100000 in 2025?"


def _m(platform, mid, yes_ask, no_ask, *, cross_refs=None):
    return CanonicalMarket(
        platform=platform, market_id=mid, title=f"[{platform}] {EVENT}",
        outcomes=[
            Outcome(name="YES", price=yes_ask, bid=yes_ask - 0.01, ask=yes_ask, available_liquidity_usd=1000.0),
            Outcome(name="NO", price=no_ask, bid=no_ask - 0.01, ask=no_ask, available_liquidity_usd=1000.0),
        ],
        volume_usd=1_000_000.0, liquidity_usd=1000.0, fee_rate=0.0,
        retrieved_at=NOW, cross_refs=cross_refs or {},
    )


def _hr(title):
    print("\n" + "=" * 64 + "\n" + title + "\n" + "=" * 64)


def main():
    # Polymarket: YES 便宜 (0.40)；predict.fun: NO 便宜 (0.45)。组装成本 0.85 < 1 → 套利。
    # predict.fun 自报 cross_ref 指向 Polymarket 的 conditionId → 金标准匹配。
    cfg = load_config_from_dict({
        "scanner": {"refresh_interval_seconds": 0.05, "fetch_timeout_seconds": 5,
                    "staleness_threshold_seconds": 60, "match_confidence_min": 0.6},
        "platforms": [
            {"name": "polymarket", "enabled": True, "fee_model": {"type": "flat", "rate": 0.0}},
            {"name": "predictfun", "enabled": True, "fee_model": {"type": "flat", "rate": 0.0}},
        ],
        "alerts": {"channels": ["log"], "criteria": {"min_net_profit_margin": 0.02}},
    })
    poly = FakeAdapter(name="polymarket", responses=[[_m("polymarket", "0xPOLYBTC", 0.40, 0.62)]])
    pf = FakeAdapter(name="predictfun", responses=[[_m("predictfun", "777", 0.55, 0.45,
                                                       cross_refs={"polymarket": ["0xPOLYBTC"]})]])
    app = build_application(cfg, adapters=[poly, pf])

    _hr("第 1 步：启动服务，后台自动摄取 + 检测（Polymarket + predict.fun）")
    with TestClient(app.app) as client:
        time.sleep(0.4)  # 让后台摄取+流水线跑几轮

        health = client.get("/health").json()
        print(f"  服务状态={health['status']}  市场数={health['market_count']}  机会数={health['opportunity_count']}")
        for a in health["adapters"]:
            print(f"    适配器 {a['name']}: 健康={a['healthy']} 市场数={a['market_count']}")

        _hr("第 2 步：GET /markets —— 两平台行情")
        for mk in client.get("/markets").json():
            print(f"  [{mk['platform']:11}] {mk['title']}  数据年龄={mk['data_age_seconds']:.1f}s 过期={mk['is_stale']}")

        _hr("第 3 步：GET /groups —— 跨平台金标准匹配")
        for g in client.get("/groups").json():
            print(f"  组 {g['group_id']}  置信度={g['match_confidence']}（金标准 cross_ref）")

        _hr("第 4 步：GET /opportunities —— 套利机会")
        for o in client.get("/opportunities").json():
            print(f"  净利润率={o['net_profit_margin']:.4f}  建议规模=${o['recommended_size_usd']:,.0f}")
            for leg in o["legs"]:
                print(f"    └─ 在 {leg['platform']} 买入 {leg['outcome']} @ {leg['price']}")

        _hr("第 5 步：GET /signals —— 有状态信号（出现→持续→消失）")
        for s in client.get("/signals").json():
            print(f"  信号 {s['group_id']}  状态={s['status']}  峰值利润率={s['peak_net_profit_margin']:.4f}  时长={s['duration_seconds']:.1f}s")
        events = client.get("/signals/events").json()
        print(f"  事件流累计 {len(events)} 条（OPENED/UPDATED/CLOSED）")

        _hr("第 6 步：GET /metrics —— 运行指标")
        mx = client.get("/metrics").json()
        print(f"  周期数={mx['cycles_total']}  信号开启累计={mx['signals_opened_total']}  当前活跃信号={mx['active_signals_count']}")

    _hr("第 7 步：模拟盘双腿执行（无需真钱，演示双腿原子性）")
    opp_for_exec = None
    # 直接用引擎对最新一个机会做一次模拟执行。
    from scanner.arbitrage import ArbitrageEngine
    from scanner.matching import MatchingEngine
    markets = app.market_store.list_all()
    groups = MatchingEngine(score_threshold=0.6).match(markets)
    opps = ArbitrageEngine(clock=lambda: NOW).evaluate(groups)
    if opps:
        engine = ExecutionEngine(adapters={
            "polymarket": PaperExecutionAdapter(name="polymarket", clock=lambda: NOW),
            "predictfun": PaperExecutionAdapter(name="predictfun", clock=lambda: NOW),
        }, clock=lambda: NOW)
        plan = engine.build_plan(opps[0], size_usd=100.0)
        plan.status = TradePlanStatus.CONFIRMED
        result = asyncio.run(engine.execute_plan(plan))
        print(f"  计划状态={result.status.value}  残余敞口={engine.has_residual_exposure(result)}")
        for leg in result.legs:
            print(f"    └─ {leg.platform} {leg.outcome}: 订单 {leg.order_id}")

    _hr("演示完成")
    print("  第一版全流程已跑通：行情→金标准匹配→套利→信号→只读API→模拟盘双腿执行。")
    print("  真实链上下单为后续一步（需钱包私钥 + 链上资金）。")


if __name__ == "__main__":
    main()
