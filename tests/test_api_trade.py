"""交易 API 测试（Phase 3 · 切片 H）。

用 TestClient 驱动 :func:`scanner.api.create_app`，验证交易端点:
- 未配置 trading_service：``/trade/plans`` 返回 []，明细/确认/拒绝返回 404。
- 配置后：列出计划、状态过滤、明细、确认（dry-run 执行）、拒绝。
- 确认/拒绝非待确认计划返回 409；不存在返回 404。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.execution import PaperExecutionAdapter
from scanner.execution_engine import ExecutionEngine
from scanner.models import ArbLeg, ArbitrageOpportunity, TradePlanStatus
from scanner.risk import RiskLimits, RiskManager
from scanner.store import InMemoryMarketStore
from scanner.trade_store import InMemoryTradeStore
from scanner.trading import TradingService

NOW = datetime.now(timezone.utc)


def _opp(group_id: str = "g1") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=f"事件 {group_id}",
        legs=[
            ArbLeg(platform="polymarket", market_id="p1", outcome="YES", price=0.40),
            ArbLeg(platform="predictfun", market_id="1471", outcome="NO", price=0.45),
        ],
        net_profit_margin=0.10,
        recommended_size_usd=100.0,
        detected_at=NOW,
        data_age_seconds=5.0,
    )


def _trading_service(dry_run: bool = True) -> TradingService:
    adapters = {
        "polymarket": PaperExecutionAdapter(name="polymarket", starting_balance_usd=10_000.0),
        "predictfun": PaperExecutionAdapter(name="predictfun", starting_balance_usd=10_000.0),
    }
    return TradingService(
        risk_manager=RiskManager(RiskLimits(dry_run=dry_run)),
        execution_engine=ExecutionEngine(adapters=adapters),
        store=InMemoryTradeStore(),
    )


# --------------------------------------------------------------------------- #
# 未配置 trading_service
# --------------------------------------------------------------------------- #

def test_trade_endpoints_without_service():
    client = TestClient(create_app(market_store=InMemoryMarketStore()))
    assert client.get("/trade/plans").json() == []
    assert client.get("/trade/plans/x").status_code == 404
    assert client.post("/trade/plans/x/confirm").status_code == 404
    assert client.post("/trade/plans/x/reject").status_code == 404


# --------------------------------------------------------------------------- #
# 配置后的完整链路
# --------------------------------------------------------------------------- #

def test_list_and_get_trade_plans():
    svc = _trading_service()
    svc.propose([_opp("g1"), _opp("g2")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))

    plans = client.get("/trade/plans").json()
    assert len(plans) == 2
    assert {p["group_id"] for p in plans} == {"g1", "g2"}
    assert all(p["status"] == "pending_confirmation" for p in plans)

    pid = plans[0]["plan_id"]
    detail = client.get(f"/trade/plans/{pid}")
    assert detail.status_code == 200
    assert detail.json()["plan_id"] == pid


def test_status_filter():
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    assert len(client.get("/trade/plans", params={"status": "pending_confirmation"}).json()) == 1
    assert client.get("/trade/plans", params={"status": "completed"}).json() == []
    # 非法状态 → 422。
    assert client.get("/trade/plans", params={"status": "bogus"}).status_code == 422


def test_confirm_paper_simulates_and_completes():
    # 模拟盘默认 dry_run 下仍模拟成交：确认 → COMPLETED，带已实现收益。
    svc = _trading_service(dry_run=True)
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    pid = client.get("/trade/plans").json()[0]["plan_id"]

    resp = client.post(f"/trade/plans/{pid}/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    # 模拟盘成交 → 有真实收益核算。
    assert body["realized_profit_usd"] is not None


def test_reject_plan():
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    pid = client.get("/trade/plans").json()[0]["plan_id"]

    resp = client.post(f"/trade/plans/{pid}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_confirm_unknown_returns_404():
    svc = _trading_service()
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    assert client.post("/trade/plans/nope/confirm").status_code == 404
    assert client.post("/trade/plans/nope/reject").status_code == 404


def test_confirm_non_pending_returns_409():
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    pid = client.get("/trade/plans").json()[0]["plan_id"]
    client.post(f"/trade/plans/{pid}/confirm")  # → completed
    # 再次确认应 409。
    assert client.post(f"/trade/plans/{pid}/confirm").status_code == 409
    # 拒绝已完成计划也应 409。
    assert client.post(f"/trade/plans/{pid}/reject").status_code == 409


# --------------------------------------------------------------------------- #
# 交易 API 鉴权（Phase 3 · 切片 H）
# --------------------------------------------------------------------------- #

def test_trade_endpoints_require_api_key_when_configured():
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(
        market_store=InMemoryMarketStore(), trading_service=svc, trade_api_key="secret",
    ))
    # 缺 key → 401。
    assert client.get("/trade/plans").status_code == 401
    pid_resp = client.get("/trade/plans", headers={"X-API-Key": "secret"})
    assert pid_resp.status_code == 200
    pid = pid_resp.json()[0]["plan_id"]
    # 错误 key → 401。
    assert client.post(f"/trade/plans/{pid}/confirm", headers={"X-API-Key": "wrong"}).status_code == 401
    # 正确 key → 放行。
    assert client.post(f"/trade/plans/{pid}/confirm", headers={"X-API-Key": "secret"}).status_code == 200


def test_trade_endpoints_open_when_no_api_key_configured():
    # 未配置 trade_api_key → 交易端点不鉴权（本机自用），与只读 API 一致。
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    assert client.get("/trade/plans").status_code == 200


def test_read_endpoints_never_require_trade_key():
    # 即便配置了交易鉴权，只读端点仍开放（不受影响）。
    client = TestClient(create_app(
        market_store=InMemoryMarketStore(), trade_api_key="secret",
    ))
    assert client.get("/markets").status_code == 200
    assert client.get("/opportunities").status_code == 200


# --------------------------------------------------------------------------- #
# 余额 / 持仓 / 敞口端点（切片 I 第一步：只读账户状态）
# --------------------------------------------------------------------------- #

def test_balance_positions_exposure_without_service():
    client = TestClient(create_app(market_store=InMemoryMarketStore()))
    assert client.get("/trade/balance").json() == {}
    assert client.get("/trade/positions").json() == []
    assert client.get("/trade/exposure").json() == {}


def test_balance_reports_per_platform():
    svc = _trading_service()
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    bal = client.get("/trade/balance").json()
    assert set(bal.keys()) == {"polymarket", "predictfun"}
    assert bal["polymarket"]["balance_usd"] == 10_000.0


def test_exposure_reflects_open_plans():
    svc = _trading_service()
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    exp = client.get("/trade/exposure").json()
    assert exp["total_exposure_usd"] == 100.0  # 一个计划核准 100
    assert any(m["exposure_usd"] == 100.0 for m in exp["per_market"])


def test_positions_after_real_execution():
    # dry_run=False 经模拟盘真实建仓后，持仓端点应反映持仓。
    svc = _trading_service(dry_run=False)
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    pid = client.get("/trade/plans").json()[0]["plan_id"]
    client.post(f"/trade/plans/{pid}/confirm")
    positions = client.get("/trade/positions").json()
    assert len(positions) >= 1
    assert {p["platform"] for p in positions} <= {"polymarket", "predictfun"}


def test_trade_balance_requires_auth_when_configured():
    svc = _trading_service()
    client = TestClient(create_app(
        market_store=InMemoryMarketStore(), trading_service=svc, trade_api_key="secret",
    ))
    assert client.get("/trade/balance").status_code == 401
    assert client.get("/trade/balance", headers={"X-API-Key": "secret"}).status_code == 200


# --------------------------------------------------------------------------- #
# 链上真实余额端点（切片 I 第一步）
# --------------------------------------------------------------------------- #

class _FakeReader:
    def __init__(self, balance):
        self._balance = balance

    async def get_balance(self, address):
        if isinstance(self._balance, Exception):
            raise self._balance
        return self._balance


def test_onchain_balance_empty_when_not_configured():
    client = TestClient(create_app(market_store=InMemoryMarketStore()))
    assert client.get("/trade/onchain-balance").json() == {}


def test_onchain_balance_reports_real_balance():
    onchain = {"polymarket": {"reader": _FakeReader(943.21), "address": "0xabc", "asset": "USDC"}}
    client = TestClient(create_app(market_store=InMemoryMarketStore(), onchain_balances=onchain))
    body = client.get("/trade/onchain-balance").json()
    assert body["polymarket"]["balance"] == 943.21
    assert body["polymarket"]["asset"] == "USDC"
    assert body["polymarket"]["source"] == "onchain"


def test_onchain_balance_isolates_errors():
    onchain = {"polymarket": {"reader": _FakeReader(RuntimeError("rpc down")), "address": "0xabc"}}
    client = TestClient(create_app(market_store=InMemoryMarketStore(), onchain_balances=onchain))
    body = client.get("/trade/onchain-balance").json()
    assert "error" in body["polymarket"]


def test_pnl_endpoint():
    svc = _trading_service(dry_run=False)
    svc.propose([_opp("g1")])
    client = TestClient(create_app(market_store=InMemoryMarketStore(), trading_service=svc))
    pid = client.get("/trade/plans").json()[0]["plan_id"]
    client.post(f"/trade/plans/{pid}/confirm")
    pnl = client.get("/trade/pnl").json()
    assert pnl["executed_trades"] == 1
    assert "total_realized_profit_usd" in pnl
    # 计划明细也带真实收益字段。
    plan = client.get(f"/trade/plans/{pid}").json()
    assert "realized_profit_usd" in plan
    assert plan["genuinely_profitable"] in (True, False)
