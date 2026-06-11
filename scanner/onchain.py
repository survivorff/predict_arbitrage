"""链上只读余额查询（Phase 3 · 切片 I 第一步：真实账户只读连通）。

真实交易的最稳妥第一步是「能真实读到账户余额」——这一步**完全只读、不需要私钥**：
链上 ERC20 余额是公开数据，用钱包地址通过 JSON-RPC 的 ``eth_call`` 调用代币合约的
``balanceOf(address)`` 即可读取。本模块据此提供 Polygon 上 USDC 余额的只读查询，
用于在仪表盘展示用户真实账户余额（接入真实下单前的连通验证）。

安全：
- 只读，绝不下单、绝不需要私钥/助记词。
- 钱包地址是公开信息，由用户在配置中提供。
- RPC URL 经配置注入；不传地址/RPC 则本模块不启用（优雅降级）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Polygon 主网上的 USDC 合约地址。
#   - 原生 USDC（Circle 发行）：0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359
#   - 桥接 USDC.e：           0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
# Polymarket 结算用 USDC.e（桥接版），故默认用它；可经配置覆盖。
POLYGON_USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359"
USDC_DECIMALS = 6

# ERC20 balanceOf(address) 的方法选择器。
_BALANCE_OF_SELECTOR = "0x70a08231"


class OnchainError(Exception):
    """链上查询失败的统一错误类型。"""


@dataclass
class ErcBalanceReader:
    """通过 JSON-RPC eth_call 读取某地址的 ERC20 代币余额（只读，无需私钥）。

    Args:
        rpc_url: EVM 链的 JSON-RPC HTTP 端点（如 Polygon 公共 RPC）。
        token_address: ERC20 代币合约地址（默认 Polygon USDC.e）。
        decimals: 代币小数位（USDC 为 6）。
        http_client: 注入的 httpx 客户端（测试用 respx mock）；省略则按需创建。
        timeout: 单次请求超时秒数。
    """

    rpc_url: str
    token_address: str = POLYGON_USDC_E
    decimals: int = USDC_DECIMALS
    http_client: Optional[httpx.AsyncClient] = None
    timeout: float = 15.0

    def _client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=self.timeout)
        return self.http_client

    @staticmethod
    def _encode_balance_of(address: str) -> str:
        """构造 balanceOf(address) 的 calldata：选择器 + 32 字节左填充地址。"""
        addr = address.lower().removeprefix("0x")
        if len(addr) != 40:
            raise OnchainError(f"非法 EVM 地址：{address!r}")
        return _BALANCE_OF_SELECTOR + addr.rjust(64, "0")

    async def get_balance(self, address: str) -> float:
        """返回 ``address`` 的代币余额（以人类可读单位，如 USDC 数量）。

        Raises:
            OnchainError: 地址非法、RPC 传输失败或返回错误。
        """
        data = self._encode_balance_of(address)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": self.token_address, "data": data}, "latest"],
        }
        try:
            response = await self._client().post(self.rpc_url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise OnchainError(f"RPC 请求失败：{exc!r}") from exc
        except ValueError as exc:
            raise OnchainError(f"RPC 返回非 JSON：{exc}") from exc

        if "error" in body and body["error"]:
            raise OnchainError(f"RPC 错误：{body['error']}")
        result = body.get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise OnchainError(f"RPC 返回异常 result：{result!r}")
        try:
            raw = int(result, 16)
        except ValueError as exc:
            raise OnchainError(f"无法解析余额十六进制：{result!r}") from exc
        return raw / (10 ** self.decimals)

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None


__all__ = [
    "OnchainError",
    "ErcBalanceReader",
    "POLYGON_USDC_E",
    "POLYGON_USDC_NATIVE",
    "USDC_DECIMALS",
]
