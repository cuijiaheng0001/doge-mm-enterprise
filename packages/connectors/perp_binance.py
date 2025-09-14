"""
Perp Binance - Binance永续合约适配器
处理DOGE-USDT线性永续合约交易
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from enum import Enum
import aiohttp
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """订单类型"""
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class TimeInForce(Enum):
    """有效期类型"""
    GTC = "GTC"  # Good Till Cancel
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill
    GTX = "GTX"  # Good Till Crossing (Post Only)


class PositionSide(Enum):
    """持仓方向"""
    BOTH = "BOTH"  # 单向持仓模式
    LONG = "LONG"  # 双向持仓-多头
    SHORT = "SHORT"  # 双向持仓-空头


@dataclass
class PerpOrder:
    """永续合约订单"""
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    time_in_force: Optional[TimeInForce] = None
    position_side: PositionSide = PositionSide.BOTH
    reduce_only: bool = False
    client_order_id: Optional[str] = None
    
    def to_params(self) -> Dict[str, Any]:
        """转换为API参数"""
        params = {
            'symbol': self.symbol,
            'side': self.side.value,
            'type': self.order_type.value,
            'quantity': self.quantity,
            'positionSide': self.position_side.value
        }
        
        if self.price is not None:
            params['price'] = f"{self.price:.5f}"
        
        if self.time_in_force is not None:
            params['timeInForce'] = self.time_in_force.value
        
        if self.reduce_only:
            params['reduceOnly'] = 'true'
        
        if self.client_order_id:
            params['newClientOrderId'] = self.client_order_id
        
        return params


@dataclass
class PerpPosition:
    """永续合约持仓"""
    symbol: str
    position_amt: float  # 持仓数量（正=多，负=空）
    entry_price: float  # 开仓均价
    mark_price: float  # 标记价格
    unrealized_pnl: float  # 未实现盈亏
    margin_type: str  # 保证金模式
    leverage: int  # 杠杆倍数
    liquidation_price: float  # 强平价格
    margin_ratio: float  # 保证金率
    
    @property
    def notional(self) -> float:
        """名义价值"""
        return abs(self.position_amt * self.mark_price)
    
    @property
    def is_long(self) -> bool:
        """是否多头"""
        return self.position_amt > 0
    
    @property
    def is_short(self) -> bool:
        """是否空头"""
        return self.position_amt < 0


class PerpBinanceConnector:
    """
    Binance永续合约连接器 - FAHE对冲执行接口
    处理DOGE-USDT线性永续合约
    """
    
    def __init__(self,
                 api_key: str,
                 api_secret: str,
                 testnet: bool = False,
                 symbol: str = "DOGEUSDT"):
        """
        初始化连接器
        
        Args:
            api_key: API密钥
            api_secret: API密钥
            testnet: 是否测试网
            symbol: 交易对
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        
        # API端点
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
            self.ws_url = "wss://stream.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"
            self.ws_url = "wss://fstream.binance.com"
        
        # 会话管理
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws_connection = None
        
        # 订单簿缓存
        self.orderbook = {
            'bids': [],
            'asks': [],
            'lastUpdateId': 0,
            'ts': 0
        }
        
        # 持仓缓存
        self.position: Optional[PerpPosition] = None
        
        # 活跃订单
        self.active_orders = {}  # order_id -> order_info
        
        # 回调函数
        self.fill_callback: Optional[Callable] = None
        
        # 统计信息
        self.stats = {
            'total_orders': 0,
            'filled_orders': 0,
            'cancelled_orders': 0,
            'rejected_orders': 0,
            'total_volume': 0.0,
            'total_fees': 0.0,
            'api_calls': 0,
            'ws_messages': 0
        }
        
        # 限流参数
        self.rate_limits = {
            'order': 100,  # 每分钟订单数
            'weight': 2400  # 每分钟权重
        }
        self.current_limits = {
            'order': 0,
            'weight': 0
        }
        self.limit_reset_ts = time.time() + 60
        
        logger.info(f"[PerpBinance] 初始化完成: symbol={symbol}, testnet={testnet}")
    
    async def start(self) -> None:
        """
        启动连接器
        """
        # 创建HTTP会话
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        # 获取账户信息
        await self.get_account_info()
        
        # 获取持仓信息
        await self.update_position()
        
        # 启动WebSocket
        await self.start_websocket()
        
        logger.info("[PerpBinance] ✅ 连接器启动成功")
    
    async def stop(self) -> None:
        """
        停止连接器
        """
        # 关闭WebSocket
        if self.ws_connection:
            await self.ws_connection.close()
            self.ws_connection = None
        
        # 关闭HTTP会话
        if self.session:
            await self.session.close()
            self.session = None
        
        logger.info("[PerpBinance] 连接器已停止")
    
    async def place_order(self, order: PerpOrder) -> Dict[str, Any]:
        """
        下单
        
        Args:
            order: 永续合约订单
        
        Returns:
            订单结果
        """
        # 检查限流
        if not self._check_rate_limit('order', 1):
            raise Exception("订单限流")
        
        # 准备参数
        params = order.to_params()
        params['timestamp'] = int(time.time() * 1000)
        
        # 发送请求
        result = await self._request('POST', '/fapi/v1/order', params, signed=True)
        
        # 更新统计
        self.stats['total_orders'] += 1
        
        # 记录活跃订单
        if result.get('orderId'):
            self.active_orders[result['orderId']] = result
        
        logger.info(f"[PerpBinance] 下单成功: {result.get('orderId')} "
                   f"{order.side.value} {order.quantity} @ {order.price}")
        
        return result
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        撤单
        
        Args:
            order_id: 订单ID
        
        Returns:
            撤单结果
        """
        params = {
            'symbol': self.symbol,
            'orderId': order_id,
            'timestamp': int(time.time() * 1000)
        }
        
        result = await self._request('DELETE', '/fapi/v1/order', params, signed=True)
        
        # 更新统计
        self.stats['cancelled_orders'] += 1
        
        # 移除活跃订单
        if order_id in self.active_orders:
            del self.active_orders[order_id]
        
        logger.info(f"[PerpBinance] 撤单成功: {order_id}")
        
        return result
    
    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        查询订单
        
        Args:
            order_id: 订单ID
        
        Returns:
            订单信息
        """
        params = {
            'symbol': self.symbol,
            'orderId': order_id,
            'timestamp': int(time.time() * 1000)
        }
        
        return await self._request('GET', '/fapi/v1/order', params, signed=True)
    
    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        获取所有挂单
        
        Returns:
            挂单列表
        """
        params = {
            'symbol': self.symbol,
            'timestamp': int(time.time() * 1000)
        }
        
        return await self._request('GET', '/fapi/v1/openOrders', params, signed=True)
    
    async def update_position(self) -> Optional[PerpPosition]:
        """
        更新持仓信息
        
        Returns:
            持仓信息
        """
        params = {
            'symbol': self.symbol,
            'timestamp': int(time.time() * 1000)
        }
        
        positions = await self._request('GET', '/fapi/v2/positionRisk', params, signed=True)
        
        for pos in positions:
            if pos['symbol'] == self.symbol:
                self.position = PerpPosition(
                    symbol=pos['symbol'],
                    position_amt=float(pos['positionAmt']),
                    entry_price=float(pos['entryPrice']),
                    mark_price=float(pos['markPrice']),
                    unrealized_pnl=float(pos['unRealizedProfit']),
                    margin_type=pos['marginType'],
                    leverage=int(pos['leverage']),
                    liquidation_price=float(pos['liquidationPrice'] or 0),
                    margin_ratio=float(pos.get('marginRatio', 0))
                )
                
                logger.debug(f"[PerpBinance] 持仓更新: amt={self.position.position_amt:.2f}, "
                           f"pnl={self.position.unrealized_pnl:.2f}")
                
                return self.position
        
        # 无持仓
        self.position = None
        return None
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息
        
        Returns:
            账户信息
        """
        params = {
            'timestamp': int(time.time() * 1000)
        }
        
        return await self._request('GET', '/fapi/v2/account', params, signed=True)
    
    async def get_orderbook(self, depth: int = 10) -> Dict[str, Any]:
        """
        获取订单簿
        
        Args:
            depth: 深度
        
        Returns:
            订单簿数据
        """
        params = {
            'symbol': self.symbol,
            'limit': depth
        }
        
        book = await self._request('GET', '/fapi/v1/depth', params)
        
        # 更新缓存
        self.orderbook = {
            'bids': [[float(p), float(q)] for p, q in book['bids']],
            'asks': [[float(p), float(q)] for p, q in book['asks']],
            'lastUpdateId': book['lastUpdateId'],
            'ts': time.time()
        }
        
        return self.orderbook
    
    async def start_websocket(self) -> None:
        """
        启动WebSocket连接
        """
        # 获取listenKey
        listen_key = await self._get_listen_key()
        
        # 创建WebSocket连接
        ws_url = f"{self.ws_url}/ws/{listen_key}"
        
        asyncio.create_task(self._ws_handler(ws_url))
        
        logger.info("[PerpBinance] WebSocket启动")
    
    async def _ws_handler(self, url: str) -> None:
        """
        WebSocket处理器
        
        Args:
            url: WebSocket URL
        """
        try:
            async with self.session.ws_connect(url) as ws:
                self.ws_connection = ws
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        await self._process_ws_message(data)
                        self.stats['ws_messages'] += 1
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"[PerpBinance] WebSocket错误: {ws.exception()}")
                        break
                        
        except Exception as e:
            logger.error(f"[PerpBinance] WebSocket异常: {e}")
        finally:
            self.ws_connection = None
    
    async def _process_ws_message(self, data: Dict[str, Any]) -> None:
        """
        处理WebSocket消息
        
        Args:
            data: 消息数据
        """
        event_type = data.get('e')
        
        if event_type == 'ORDER_TRADE_UPDATE':
            # 订单更新
            order = data['o']
            order_id = order['i']
            status = order['X']
            
            logger.info(f"[PerpBinance] 订单更新: {order_id} status={status}")
            
            if status == 'FILLED':
                self.stats['filled_orders'] += 1
                
                # 触发回调
                if self.fill_callback:
                    await self.fill_callback({
                        'order_id': order_id,
                        'side': order['S'],
                        'qty': float(order['q']),
                        'price': float(order['p']),
                        'fee': float(order.get('n', 0))
                    })
            
            # 更新活跃订单
            if status in ['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED']:
                if order_id in self.active_orders:
                    del self.active_orders[order_id]
        
        elif event_type == 'ACCOUNT_UPDATE':
            # 账户更新
            positions = data.get('a', {}).get('P', [])
            for pos in positions:
                if pos['s'] == self.symbol:
                    logger.debug(f"[PerpBinance] 持仓更新: {pos}")
    
    async def _get_listen_key(self) -> str:
        """
        获取listenKey
        
        Returns:
            listenKey
        """
        result = await self._request('POST', '/fapi/v1/listenKey', signed=False)
        return result['listenKey']
    
    async def _request(self, method: str, endpoint: str, 
                      params: Dict[str, Any] = None, signed: bool = False) -> Any:
        """
        发送HTTP请求
        
        Args:
            method: HTTP方法
            endpoint: API端点
            params: 参数
            signed: 是否需要签名
        
        Returns:
            响应数据
        """
        if params is None:
            params = {}
        
        # 更新API调用统计
        self.stats['api_calls'] += 1
        self._update_rate_limits('weight', 1)
        
        # 添加API Key
        headers = {'X-MBX-APIKEY': self.api_key}
        
        # 签名
        if signed:
            query_string = urlencode(params)
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            params['signature'] = signature
        
        # 构建URL
        url = f"{self.base_url}{endpoint}"
        
        # 发送请求
        async with self.session.request(method, url, params=params, headers=headers) as response:
            if response.status != 200:
                error_data = await response.text()
                raise Exception(f"API错误 {response.status}: {error_data}")
            
            return await response.json()
    
    def _check_rate_limit(self, limit_type: str, weight: int) -> bool:
        """
        检查限流
        
        Args:
            limit_type: 限流类型
            weight: 权重
        
        Returns:
            是否可以继续
        """
        # 重置计数器
        if time.time() >= self.limit_reset_ts:
            self.current_limits = {'order': 0, 'weight': 0}
            self.limit_reset_ts = time.time() + 60
        
        # 检查限制
        if self.current_limits[limit_type] + weight > self.rate_limits[limit_type]:
            return False
        
        return True
    
    def _update_rate_limits(self, limit_type: str, weight: int) -> None:
        """
        更新限流计数
        
        Args:
            limit_type: 限流类型
            weight: 权重
        """
        self.current_limits[limit_type] += weight
    
    def set_fill_callback(self, callback: Callable) -> None:
        """
        设置成交回调
        
        Args:
            callback: 回调函数
        """
        self.fill_callback = callback
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'active_orders': len(self.active_orders),
            'position_amt': self.position.position_amt if self.position else 0,
            'current_limits': self.current_limits,
            'orderbook_age': time.time() - self.orderbook['ts'] if self.orderbook['ts'] > 0 else float('inf')
        }