"""链上只读余额查询测试（Phase 3 · 切片 I 第一步）。

用 respx mock JSON-RPC，验证 ErcBalanceReader 正确构造 balanceOf calldata、解析
十六进制余额、按小数位换算，并对错误/异常返回稳健处理。不发起真实网络请求。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scanner.onchain import ErcBalanceReader, OnchainError, POLYGON_USDC_E

RPC = "https://rpc.example.test/polygon"


def _rpc_result(hexval: str):
    return {"jsonrpc": "2.0", "id": 1, "result": hexval}


@respx.mock
async def test_reads_and_scales_balance():
    # 1,000.500000 USDC = 1000500000 (6 decimals) = 0x3ba1ca40
    raw = 1_000_500_000
    route = respx.post(RPC).mock(return_value=httpx.Response(200, json=_rpc_result(hex(raw))))
    reader = ErcBalanceReader(rpc_url=RPC)
    bal = await reader.get_balance("0x" + "ab" * 20)
    assert bal == pytest.approx(1000.5)
    # 校验请求体：eth_call balanceOf 到 USDC 合约。
    sent = route.calls.last.request
    import json
    body = json.loads(sent.content)
    assert body["method"] == "eth_call"
    assert body["params"][0]["to"] == POLYGON_USDC_E
    assert body["params"][0]["data"].startswith("0x70a08231")
    # 地址被左填充到 32 字节（64 hex）。
    assert body["params"][0]["data"].endswith("ab" * 20)
    assert len(body["params"][0]["data"]) == 2 + 8 + 64


@respx.mock
async def test_zero_balance():
    respx.post(RPC).mock(return_value=httpx.Response(200, json=_rpc_result("0x0")))
    reader = ErcBalanceReader(rpc_url=RPC)
    assert await reader.get_balance("0x" + "00" * 20) == 0.0


@respx.mock
async def test_rpc_error_raises_onchain_error():
    respx.post(RPC).mock(return_value=httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
    ))
    reader = ErcBalanceReader(rpc_url=RPC)
    with pytest.raises(OnchainError):
        await reader.get_balance("0x" + "ab" * 20)


@respx.mock
async def test_http_error_raises_onchain_error():
    respx.post(RPC).mock(return_value=httpx.Response(500, text="server error"))
    reader = ErcBalanceReader(rpc_url=RPC)
    with pytest.raises(OnchainError):
        await reader.get_balance("0x" + "ab" * 20)


async def test_invalid_address_rejected():
    reader = ErcBalanceReader(rpc_url=RPC)
    with pytest.raises(OnchainError):
        await reader.get_balance("0x1234")  # 太短


@respx.mock
async def test_custom_decimals():
    # 18 位小数代币：1e18 = 1.0。
    respx.post(RPC).mock(return_value=httpx.Response(200, json=_rpc_result(hex(10**18))))
    reader = ErcBalanceReader(rpc_url=RPC, decimals=18)
    assert await reader.get_balance("0x" + "cd" * 20) == pytest.approx(1.0)
