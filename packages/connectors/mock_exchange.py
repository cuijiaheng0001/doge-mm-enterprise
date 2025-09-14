#!/usr/bin/env python3
"""
Mock Exchange - 模拟交易所用于离线测试
"""

import asyncio
import time
import random
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
import logging
import math

logger = logging.getLogger(__name__)


class MockOrder:
    """模拟订单"""
    def __init__(self, oid: str, side: str, price: float, qty: float, 
                 order_type: str = 'LIMIT_MAKER', ttl_ms: int = 0):
        self.oid = oid
        self.side = side
        self.price = price
        self.qty = qty
        self.filled_qty = 0.0
        self.order_type = order_type
        self.status = 'NEW'
        self.create_time = time.time()
        self.ttl_ms = ttl_ms
        self.expire_time = self.create_time + ttl_ms/1000 if ttl_ms > 0 else float('inf')
        
    @property
    def remaining(self) -> float:
        return self.qty - self.filled_qty
        
    @property
    def is_filled(self) -> bool:
        return self.remaining <= 0
        
    @property
    def is_expired(self) -> bool:
        return time.time() > self.expire_time


class MockExchange:
    """模拟交易所 - 用于离线测试DLE和策略逻辑"""
    
    def __init__(self, symbol: str = 'DOGEUSDT'):
        self.symbol = symbol
        
        # 市场参数
        self.mid_price = 0.24000
        self.spread = 0.00010  # 0.01 cents
        self.tick_size = 0.00001
        self.step_size = 1.0
        self.min_notional = 5.0
        self.price_precision = 5
        self.qty_precision = 0
        
        # 账户余额
        self.balances = {
            'USDT': {'free': 300.0, 'locked': 0.0},
            'DOGE': {'free': 1200.0, 'locked': 0.0}
        }
        
        # 订单簿
        self.orders = {}  # oid -> MockOrder
        self.buy_orders = defaultdict(list)  # price -> [orders]
        self.sell_orders = defaultdict(list)  # price -> [orders]
        
        # 成交记录
        self.trades = deque(maxlen=1000)
        
        # 模拟参数
        self.fill_probability = 0.3  # 成交概率
        self.partial_fill_ratio = 0.5  # 部分成交比例
        self.price_volatility = 0.0001  # 价格波动率
        
        # 统计
        self.stats = defaultdict(int)
        
    def _update_market(self):
        """更新市场价格（模拟价格波动）"""
        # 随机游走
        change = random.gauss(0, self.price_volatility)
        self.mid_price *= (1 + change)
        self.mid_price = round(self.mid_price, self.price_precision)
        
    def get_best_bid_ask(self) -> Tuple[float, float]:
        """获取最佳买卖价"""
        best_bid = self.mid_price - self.spread/2
        best_ask = self.mid_price + self.spread/2
        return (
            round(best_bid, self.price_precision),
            round(best_ask, self.price_precision)
        )
        
    def _check_maker_only(self, side: str, price: float) -> bool:
        """检查是否满足LIMIT_MAKER条件"""
        best_bid, best_ask = self.get_best_bid_ask()
        if side == 'BUY':
            return price <= best_bid
        else:
            return price >= best_ask
            
    def _check_min_notional(self, price: float, qty: float) -> bool:
        """检查最小名义额"""
        return price * qty >= self.min_notional
        
    def _check_step_size(self, qty: float) -> bool:
        """检查数量步进"""
        return qty % self.step_size == 0
        
    def _check_balance(self, side: str, price: float, qty: float) -> bool:
        """检查余额是否足够"""
        if side == 'BUY':
            required = price * qty
            return self.balances['USDT']['free'] >= required
        else:
            return self.balances['DOGE']['free'] >= qty
            
    async def post_only_limit(self, side: str, price: float, qty: float, 
                              ttl_ms: int = 0) -> Tuple[bool, str]:
        """
        下单（LIMIT_MAKER）
        
        Returns:
            (success, reason/order_id)
        """
        # 合规检查
        if not self._check_maker_only(side, price):
            self.stats['reject_maker'] += 1
            return False, "Order would immediately match and take"
            
        if not self._check_min_notional(price, qty):
            self.stats['reject_min_notional'] += 1
            return False, f"MIN_NOTIONAL: {price*qty:.2f} < {self.min_notional}"
            
        if not self._check_step_size(qty):
            self.stats['reject_step_size'] += 1
            return False, f"LOT_SIZE: qty={qty} not aligned to {self.step_size}"
            
        if not self._check_balance(side, price, qty):
            self.stats['reject_balance'] += 1
            return False, "Insufficient balance"
            
        # 创建订单
        oid = f"MOCK-{int(time.time()*1000)}-{random.randint(1000,9999)}"
        order = MockOrder(oid, side, price, qty, 'LIMIT_MAKER', ttl_ms)
        
        # 锁定资金
        if side == 'BUY':
            amount = price * qty
            self.balances['USDT']['free'] -= amount
            self.balances['USDT']['locked'] += amount
            self.buy_orders[price].append(order)
        else:
            self.balances['DOGE']['free'] -= qty
            self.balances['DOGE']['locked'] += qty
            self.sell_orders[price].append(order)
            
        self.orders[oid] = order
        self.stats['orders_placed'] += 1
        
        # 异步触发成交模拟
        asyncio.create_task(self._simulate_fills(order))
        
        return True, oid
        
    async def _simulate_fills(self, order: MockOrder):
        """模拟订单成交"""
        await asyncio.sleep(random.uniform(0.5, 2.0))
        
        while not order.is_filled and not order.is_expired:
            # 检查是否成交
            if random.random() < self.fill_probability:
                # 部分或全部成交
                fill_ratio = random.uniform(self.partial_fill_ratio, 1.0)
                fill_qty = min(order.remaining, order.qty * fill_ratio)
                
                order.filled_qty += fill_qty
                
                # 更新余额
                if order.side == 'BUY':
                    self.balances['USDT']['locked'] -= order.price * fill_qty
                    self.balances['DOGE']['free'] += fill_qty
                else:
                    self.balances['DOGE']['locked'] -= fill_qty
                    self.balances['USDT']['free'] += order.price * fill_qty
                    
                # 记录成交
                self.trades.append({
                    'time': time.time(),
                    'side': order.side,
                    'price': order.price,
                    'qty': fill_qty,
                    'oid': order.oid
                })
                
                self.stats['fills'] += 1
                logger.debug(f"[MockEx] FILL {order.side} {fill_qty:.1f}@{order.price:.5f}")
                
                if order.is_filled:
                    order.status = 'FILLED'
                    break
                    
            # 检查TTL
            if order.is_expired:
                await self.cancel_order(order.oid)
                break
                
            await asyncio.sleep(random.uniform(1, 3))
            
    async def cancel_order(self, oid: str) -> bool:
        """撤单"""
        order = self.orders.get(oid)
        if not order or order.status != 'NEW':
            return False
            
        # 释放锁定资金
        remaining = order.remaining
        if order.side == 'BUY':
            self.balances['USDT']['locked'] -= order.price * remaining
            self.balances['USDT']['free'] += order.price * remaining
            self.buy_orders[order.price].remove(order)
        else:
            self.balances['DOGE']['locked'] -= remaining
            self.balances['DOGE']['free'] += remaining
            self.sell_orders[order.price].remove(order)
            
        order.status = 'CANCELED'
        self.stats['orders_canceled'] += 1
        return True
        
    async def get_account(self) -> Dict:
        """获取账户信息"""
        return {
            'balances': [
                {'asset': 'USDT', 'free': str(self.balances['USDT']['free']), 
                 'locked': str(self.balances['USDT']['locked'])},
                {'asset': 'DOGE', 'free': str(self.balances['DOGE']['free']), 
                 'locked': str(self.balances['DOGE']['locked'])},
            ]
        }
        
    async def get_open_orders(self) -> List[Dict]:
        """获取未成交订单"""
        open_orders = []
        for oid, order in self.orders.items():
            if order.status == 'NEW':
                open_orders.append({
                    'orderId': oid,
                    'clientOrderId': oid,
                    'side': order.side,
                    'price': str(order.price),
                    'origQty': str(order.qty),
                    'executedQty': str(order.filled_qty),
                    'status': order.status,
                    'type': order.order_type,
                    'time': int(order.create_time * 1000)
                })
        return open_orders
        
    async def get_depth(self, limit: int = 20) -> Dict:
        """获取深度"""
        self._update_market()
        best_bid, best_ask = self.get_best_bid_ask()
        
        # 生成模拟深度
        bids = []
        asks = []
        
        for i in range(limit):
            bid_price = best_bid - i * self.tick_size
            ask_price = best_ask + i * self.tick_size
            
            # 随机数量
            bid_qty = random.uniform(100, 10000)
            ask_qty = random.uniform(100, 10000)
            
            bids.append([str(bid_price), str(bid_qty)])
            asks.append([str(ask_price), str(ask_qty)])
            
        return {
            'bids': bids,
            'asks': asks,
            'lastUpdateId': int(time.time() * 1000)
        }
        
    def get_exchange_info(self) -> Dict:
        """获取交易规则"""
        return {
            'symbols': [{
                'symbol': self.symbol,
                'status': 'TRADING',
                'baseAsset': 'DOGE',
                'quoteAsset': 'USDT',
                'filters': [
                    {'filterType': 'PRICE_FILTER', 'tickSize': str(self.tick_size)},
                    {'filterType': 'LOT_SIZE', 'stepSize': str(self.step_size)},
                    {'filterType': 'MIN_NOTIONAL', 'minNotional': str(self.min_notional)}
                ]
            }]
        }
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return dict(self.stats)