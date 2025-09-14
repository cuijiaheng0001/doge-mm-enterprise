#!/usr/bin/env python3
"""
CoreTradeConnector - 精简版交易连接器
只负责核心交易执行，不管数据获取

职责：
✅ 创建订单
✅ 取消订单
✅ 取消并替换订单
✅ 测试订单
✅ 获取交易规则

不包含：
❌ 市场数据（使用DualActiveMarketData）
❌ 账户数据（使用UserDataStream）
❌ WebSocket管理（由专门组件管理）
❌ listenKey管理（由UserDataStream管理）
"""

import os
import time
import hmac
import hashlib
import logging
from typing import Optional, Dict, Any
from decimal import Decimal
import aiohttp
import asyncio
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class CoreTradeConnector:
    """
    精简版交易连接器 - 只管交易执行
    从TurboConnector精简而来，去除所有重叠功能
    """

    def __init__(self, api_key: str = None, api_secret: str = None):
        """初始化交易连接器"""
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")

        # API端点
        self.base_url = "https://api.binance.com"
        self.testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        if self.testnet:
            self.base_url = "https://testnet.binance.vision"

        # HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None

        # 交易规则缓存
        self.symbol_filters: Dict[str, Dict] = {}

        logger.info(
            "[CoreTradeConnector] 初始化完成 testnet=%s",
            self.testnet
        )

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.close()

    async def initialize(self):
        """初始化HTTP会话"""
        if not self.session:
            self.session = aiohttp.ClientSession()
            logger.info("[CoreTradeConnector] HTTP会话已创建")

    async def close(self):
        """关闭HTTP会话"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("[CoreTradeConnector] HTTP会话已关闭")

    def _sign(self, params: dict) -> dict:
        """签名请求参数"""
        params = params.copy()
        params['timestamp'] = int(time.time() * 1000)

        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        params['signature'] = signature
        return params

    async def _request(self, method: str, endpoint: str,
                       params: dict = None, signed: bool = False) -> dict:
        """发送HTTP请求"""
        if not self.session:
            await self.initialize()

        url = f"{self.base_url}{endpoint}"
        headers = {"X-MBX-APIKEY": self.api_key}

        if signed:
            params = self._sign(params or {})

        try:
            if method == "GET":
                async with self.session.get(url, params=params, headers=headers) as resp:
                    return await resp.json()
            elif method == "POST":
                async with self.session.post(url, params=params, headers=headers) as resp:
                    return await resp.json()
            elif method == "DELETE":
                async with self.session.delete(url, params=params, headers=headers) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error("[CoreTradeConnector] 请求失败: %s", str(e))
            raise

    # ==================== 核心交易方法 ====================

    async def create_order(self, symbol: str, side: str, order_type: str,
                          quantity: float = None, price: float = None,
                          time_in_force: str = "GTC",
                          client_order_id: str = None) -> dict:
        """
        创建订单

        Args:
            symbol: 交易对
            side: BUY/SELL
            order_type: LIMIT/MARKET/LIMIT_MAKER
            quantity: 数量
            price: 价格
            time_in_force: GTC/IOC/FOK
            client_order_id: 客户端订单ID

        Returns:
            订单响应
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "timeInForce": time_in_force
        }

        if quantity:
            params["quantity"] = str(quantity)
        if price:
            params["price"] = str(price)
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        logger.debug(
            "[CoreTradeConnector] 创建订单: %s %s %s qty=%s price=%s",
            symbol, side, order_type, quantity, price
        )

        result = await self._request("POST", "/api/v3/order", params, signed=True)

        if "orderId" in result:
            logger.info(
                "[CoreTradeConnector] 订单创建成功: orderId=%s status=%s",
                result["orderId"], result.get("status")
            )
        else:
            logger.error("[CoreTradeConnector] 订单创建失败: %s", result)

        return result

    async def cancel_order(self, symbol: str, order_id: int = None,
                          client_order_id: str = None) -> dict:
        """
        取消订单

        Args:
            symbol: 交易对
            order_id: 订单ID
            client_order_id: 客户端订单ID

        Returns:
            取消响应
        """
        params = {"symbol": symbol}

        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("必须提供order_id或client_order_id")

        logger.debug(
            "[CoreTradeConnector] 取消订单: %s orderId=%s",
            symbol, order_id
        )

        result = await self._request("DELETE", "/api/v3/order", params, signed=True)

        if "orderId" in result:
            logger.info(
                "[CoreTradeConnector] 订单取消成功: orderId=%s",
                result["orderId"]
            )
        else:
            logger.error("[CoreTradeConnector] 订单取消失败: %s", result)

        return result

    async def cancel_replace_order(self, symbol: str,
                                  cancel_order_id: int,
                                  side: str, quantity: float,
                                  price: float) -> dict:
        """
        取消并替换订单（原子操作）

        Args:
            symbol: 交易对
            cancel_order_id: 要取消的订单ID
            side: 新订单方向
            quantity: 新订单数量
            price: 新订单价格

        Returns:
            新订单响应
        """
        params = {
            "symbol": symbol,
            "cancelOrderId": cancel_order_id,
            "side": side,
            "type": "LIMIT_MAKER",
            "quantity": str(quantity),
            "price": str(price),
            "cancelReplaceMode": "STOP_ON_FAILURE"
        }

        logger.debug(
            "[CoreTradeConnector] 取消并替换: cancel=%s new=%s %s@%s",
            cancel_order_id, side, quantity, price
        )

        result = await self._request(
            "POST",
            "/api/v3/order/cancelReplace",
            params,
            signed=True
        )

        if result.get("newOrderResponse", {}).get("orderId"):
            logger.info(
                "[CoreTradeConnector] 取消并替换成功: old=%s new=%s",
                cancel_order_id,
                result["newOrderResponse"]["orderId"]
            )
        else:
            logger.error("[CoreTradeConnector] 取消并替换失败: %s", result)

        return result

    async def test_order(self, symbol: str, side: str, order_type: str,
                        quantity: float = None, price: float = None) -> dict:
        """
        测试订单（不会真实下单）

        Args:
            symbol: 交易对
            side: BUY/SELL
            order_type: LIMIT/MARKET
            quantity: 数量
            price: 价格

        Returns:
            测试结果
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type
        }

        if quantity:
            params["quantity"] = str(quantity)
        if price:
            params["price"] = str(price)

        logger.debug(
            "[CoreTradeConnector] 测试订单: %s %s %s",
            symbol, side, order_type
        )

        result = await self._request(
            "POST",
            "/api/v3/order/test",
            params,
            signed=True
        )

        # 测试订单成功返回空字典
        if result == {}:
            logger.info("[CoreTradeConnector] 订单测试通过")
            return {"status": "PASSED"}
        else:
            logger.error("[CoreTradeConnector] 订单测试失败: %s", result)
            return {"status": "FAILED", "error": result}

    async def get_symbol_filters(self, symbol: str) -> dict:
        """
        获取交易对过滤器（最小数量、价格精度等）

        Args:
            symbol: 交易对

        Returns:
            过滤器规则
        """
        # 如果已缓存，直接返回
        if symbol in self.symbol_filters:
            return self.symbol_filters[symbol]

        try:
            result = await self._request(
                "GET",
                "/api/v3/exchangeInfo",
                {"symbol": symbol}
            )

            for sym_info in result.get("symbols", []):
                if sym_info["symbol"] == symbol:
                    filters = {}
                    for f in sym_info["filters"]:
                        if f["filterType"] == "PRICE_FILTER":
                            filters["min_price"] = float(f["minPrice"])
                            filters["max_price"] = float(f["maxPrice"])
                            filters["tick_size"] = float(f["tickSize"])
                        elif f["filterType"] == "LOT_SIZE":
                            filters["min_qty"] = float(f["minQty"])
                            filters["max_qty"] = float(f["maxQty"])
                            filters["step_size"] = float(f["stepSize"])
                        elif f["filterType"] == "MIN_NOTIONAL":
                            filters["min_notional"] = float(f["minNotional"])

                    self.symbol_filters[symbol] = filters
                    logger.info(
                        "[CoreTradeConnector] %s过滤器: min_qty=%s tick_size=%s",
                        symbol, filters.get("min_qty"), filters.get("tick_size")
                    )
                    return filters

            return {}

        except Exception as e:
            logger.error("[CoreTradeConnector] 获取过滤器失败: %s", str(e))
            return {}

    # ==================== 工具方法 ====================

    def get_status(self) -> dict:
        """获取连接器状态"""
        return {
            "connected": self.session is not None,
            "testnet": self.testnet,
            "base_url": self.base_url
        }


# ==================== 使用示例 ====================
async def main():
    """测试示例"""
    async with CoreTradeConnector() as connector:
        # 获取交易规则
        filters = await connector.get_symbol_filters("DOGEUSDT")
        print(f"DOGEUSDT规则: {filters}")

        # 测试订单
        result = await connector.test_order(
            symbol="DOGEUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=100,
            price=0.3
        )
        print(f"测试结果: {result}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())