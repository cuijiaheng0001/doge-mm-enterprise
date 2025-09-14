#!/usr/bin/env python3
"""
Phase 3 - Track C2: 瞬时对冲响应（Sub-50ms Cross Response）
解决问题：一边成交后另一边无即时反应
"""

import time
import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)

class AdjustmentType(Enum):
    """调整类型"""
    REPRICE = "reprice"       # 改价
    RESIZE = "resize"         # 改量
    CANCEL = "cancel"         # 撤单
    NEW = "new"              # 新单
    REPLACE = "replace"       # 替换

@dataclass
class FillEvent:
    """成交事件"""
    order_id: str
    side: str  # BUY or SELL
    price: float
    quantity: float
    timestamp: float
    layer: str  # L0/L1/L2

@dataclass
class CrossAdjustment:
    """跨边调整指令"""
    action: AdjustmentType
    side: str
    layer: str
    orders: List[Dict]  # 要调整的订单列表
    urgency: float  # 0-1紧急度

@dataclass
class InventoryState:
    """库存状态"""
    doge_qty: float
    usdt_qty: float
    doge_frac: float  # DOGE占比
    target_frac: float = 0.5
    imbalance: float = 0  # 失衡度

class InstantCrossResponse:
    """
    瞬时对冲响应系统
    - 50ms内完成对边响应
    - 原子库存更新
    - 批量调整执行
    """
    
    def __init__(self, connector, order_manager):
        self.connector = connector
        self.order_manager = order_manager
        
        # 库存跟踪
        self.inventory = InventoryState(
            doge_qty=0,
            usdt_qty=0,
            doge_frac=0.5
        )
        
        # 调整方案缓存
        self.adjustment_cache = {}
        self.last_cache_update = 0
        
        # 延迟跟踪
        self.latency_tracker = deque(maxlen=100)
        self.latency_p50 = 0
        self.latency_p99 = 0
        
        # 统计
        self.stats = {
            'fill_events': 0,
            'adjustments': 0,
            'avg_latency_ms': 0,
            'slow_responses': 0,  # >50ms
            'failed_adjustments': 0
        }
        
        # 锁机制
        self.inventory_lock = asyncio.Lock()
        self.adjustment_lock = asyncio.Lock()
        
        logger.info("[Phase3-C2] InstantCrossResponse initialized")
        
    async def on_fill_event(self, fill: FillEvent):
        """
        成交事件处理 - 最高优先级
        目标：50ms内完成对边响应
        """
        start_time = time.monotonic()
        
        try:
            # 1. 立即更新库存 (0-5ms)
            await self.update_inventory_atomic(fill)
            
            # 2. 计算对边调整 (5-10ms)
            adjustment = await self.calculate_cross_adjustment(fill)
            
            # 3. 批量执行调整 (10-40ms)
            if adjustment:
                await self.execute_adjustments_batch(adjustment)
                
            # 4. 记录延迟
            latency_ms = (time.monotonic() - start_time) * 1000
            self.track_latency(latency_ms)
            
            if latency_ms > 50:
                self.stats['slow_responses'] += 1
                logger.warning(f"[Phase3-C2] SLOW response: {latency_ms:.1f}ms for {fill.side} fill")
            else:
                logger.debug(f"[Phase3-C2] Fast response: {latency_ms:.1f}ms")
                
            self.stats['fill_events'] += 1
            
        except Exception as e:
            logger.error(f"[Phase3-C2] Fill response failed: {e}")
            self.stats['failed_adjustments'] += 1
            
    async def update_inventory_atomic(self, fill: FillEvent):
        """原子更新库存"""
        async with self.inventory_lock:
            # 更新数量
            if fill.side == 'BUY':
                self.inventory.doge_qty += fill.quantity
                self.inventory.usdt_qty -= fill.quantity * fill.price
            else:  # SELL
                self.inventory.doge_qty -= fill.quantity
                self.inventory.usdt_qty += fill.quantity * fill.price
                
            # 计算比例
            total_value = self.inventory.usdt_qty + self.inventory.doge_qty * fill.price
            if total_value > 0:
                self.inventory.doge_frac = (self.inventory.doge_qty * fill.price) / total_value
                self.inventory.imbalance = abs(self.inventory.doge_frac - self.inventory.target_frac)
            
            logger.debug(f"[Phase3-C2] Inventory updated: DOGE={self.inventory.doge_qty:.1f}, frac={self.inventory.doge_frac:.3f}")
            
    async def calculate_cross_adjustment(self, fill: FillEvent) -> Optional[CrossAdjustment]:
        """
        计算对边调整方案
        买入成交 → 调整卖单
        卖出成交 → 调整买单
        """
        # 使用缓存的方案（如果新鲜）
        cache_key = f"{fill.side}_{fill.layer}"
        if cache_key in self.adjustment_cache:
            cached = self.adjustment_cache[cache_key]
            if time.time() - cached['timestamp'] < 1:  # 1秒内有效
                return cached['adjustment']
                
        # 计算新方案
        adjustment = None
        
        if fill.side == 'BUY':
            # 买入成交，需要调整卖单
            adjustment = self._calc_sell_adjustment(fill)
        else:
            # 卖出成交，需要调整买单
            adjustment = self._calc_buy_adjustment(fill)
            
        # 缓存方案
        self.adjustment_cache[cache_key] = {
            'adjustment': adjustment,
            'timestamp': time.time()
        }
        
        return adjustment
        
    def _calc_sell_adjustment(self, fill: FillEvent) -> CrossAdjustment:
        """计算卖单调整"""
        orders = []
        
        # 根据库存失衡度决定调整策略
        if self.inventory.imbalance > 0.1:  # 严重失衡
            # 激进调整：降价+增量
            action = AdjustmentType.REPLACE
            urgency = 0.9
            
            # L0卖单降价
            orders.append({
                'layer': 'L0',
                'action': 'reprice_down',
                'ticks': 2,
                'size_mult': 1.2
            })
            
        elif self.inventory.imbalance > 0.05:  # 轻度失衡
            # 温和调整：小幅降价
            action = AdjustmentType.REPRICE
            urgency = 0.5
            
            orders.append({
                'layer': 'L0',
                'action': 'reprice_down',
                'ticks': 1,
                'size_mult': 1.0
            })
            
        else:  # 平衡状态
            # 维持调整：补充订单
            action = AdjustmentType.NEW
            urgency = 0.3
            
            orders.append({
                'layer': fill.layer,
                'action': 'refill',
                'size_mult': 1.0
            })
            
        return CrossAdjustment(
            action=action,
            side='SELL',
            layer=fill.layer,
            orders=orders,
            urgency=urgency
        )
        
    def _calc_buy_adjustment(self, fill: FillEvent) -> CrossAdjustment:
        """计算买单调整"""
        orders = []
        
        # 镜像逻辑
        if self.inventory.imbalance > 0.1:
            action = AdjustmentType.REPLACE
            urgency = 0.9
            
            orders.append({
                'layer': 'L0',
                'action': 'reprice_up',
                'ticks': 2,
                'size_mult': 1.2
            })
            
        elif self.inventory.imbalance > 0.05:
            action = AdjustmentType.REPRICE
            urgency = 0.5
            
            orders.append({
                'layer': 'L0',
                'action': 'reprice_up',
                'ticks': 1,
                'size_mult': 1.0
            })
            
        else:
            action = AdjustmentType.NEW
            urgency = 0.3
            
            orders.append({
                'layer': fill.layer,
                'action': 'refill',
                'size_mult': 1.0
            })
            
        return CrossAdjustment(
            action=action,
            side='BUY',
            layer=fill.layer,
            orders=orders,
            urgency=urgency
        )
        
    async def execute_adjustments_batch(self, adjustment: CrossAdjustment):
        """批量执行调整"""
        async with self.adjustment_lock:
            start = time.monotonic()
            
            # 根据调整类型选择执行策略
            if adjustment.action == AdjustmentType.REPLACE:
                # 使用cancel-replace原子操作
                await self._execute_replace_batch(adjustment)
                
            elif adjustment.action == AdjustmentType.REPRICE:
                # 仅改价
                await self._execute_reprice_batch(adjustment)
                
            elif adjustment.action == AdjustmentType.NEW:
                # 下新单
                await self._execute_new_batch(adjustment)
                
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(f"[Phase3-C2] Batch execution took {elapsed_ms:.1f}ms")
            
            self.stats['adjustments'] += 1
            
    async def _execute_replace_batch(self, adjustment: CrossAdjustment):
        """执行批量替换"""
        # 构造替换请求
        replace_requests = []
        
        for order_spec in adjustment.orders:
            # 这里需要与实际的订单管理系统集成
            # 示例代码
            replace_requests.append({
                'side': adjustment.side,
                'layer': order_spec['layer'],
                'action': order_spec['action'],
                'params': order_spec
            })
            
        # 批量发送（需要实际实现）
        # await self.connector.batch_replace(replace_requests)
        
    async def _execute_reprice_batch(self, adjustment: CrossAdjustment):
        """执行批量改价"""
        # 类似逻辑
        pass
        
    async def _execute_new_batch(self, adjustment: CrossAdjustment):
        """执行批量新单"""
        # 类似逻辑
        pass
        
    def track_latency(self, latency_ms: float):
        """跟踪延迟"""
        self.latency_tracker.append(latency_ms)
        
        # 计算分位数
        if len(self.latency_tracker) >= 10:
            sorted_latencies = sorted(self.latency_tracker)
            n = len(sorted_latencies)
            self.latency_p50 = sorted_latencies[n // 2]
            self.latency_p99 = sorted_latencies[int(n * 0.99)]
            
            # 更新平均值
            self.stats['avg_latency_ms'] = sum(self.latency_tracker) / len(self.latency_tracker)
            
    def precompute_adjustments(self, market_state: Dict):
        """
        预计算调整方案
        在空闲时预先计算各种场景的调整方案，减少实时计算时间
        """
        scenarios = [
            ('BUY', 'L0'), ('BUY', 'L1'), ('BUY', 'L2'),
            ('SELL', 'L0'), ('SELL', 'L1'), ('SELL', 'L2')
        ]
        
        for side, layer in scenarios:
            # 模拟成交事件
            mock_fill = FillEvent(
                order_id='mock',
                side=side,
                price=market_state.get('mid', 0),
                quantity=100,
                timestamp=time.time(),
                layer=layer
            )
            
            # 计算并缓存
            if side == 'BUY':
                adjustment = self._calc_sell_adjustment(mock_fill)
            else:
                adjustment = self._calc_buy_adjustment(mock_fill)
                
            cache_key = f"{side}_{layer}"
            self.adjustment_cache[cache_key] = {
                'adjustment': adjustment,
                'timestamp': time.time()
            }
            
        logger.debug(f"[Phase3-C2] Precomputed {len(scenarios)} adjustment scenarios")
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'fill_events': self.stats['fill_events'],
            'adjustments': self.stats['adjustments'],
            'avg_latency_ms': self.stats['avg_latency_ms'],
            'latency_p50_ms': self.latency_p50,
            'latency_p99_ms': self.latency_p99,
            'slow_responses': self.stats['slow_responses'],
            'slow_pct': self.stats['slow_responses'] / max(1, self.stats['fill_events']) * 100,
            'failed_adjustments': self.stats['failed_adjustments'],
            'inventory_imbalance': self.inventory.imbalance,
            'doge_frac': self.inventory.doge_frac
        }
        
    def health_check(self) -> bool:
        """健康检查"""
        # P99延迟必须<50ms
        if self.latency_p99 > 50:
            logger.warning(f"[Phase3-C2] P99 latency {self.latency_p99:.1f}ms exceeds 50ms target")
            return False
            
        # 失败率必须<5%
        if self.stats['fill_events'] > 100:
            fail_rate = self.stats['failed_adjustments'] / self.stats['fill_events']
            if fail_rate > 0.05:
                logger.warning(f"[Phase3-C2] Failure rate {fail_rate:.1%} exceeds 5% threshold")
                return False
                
        return True

# 单例实例
_instant_response_instance = None

def get_instant_cross_response(connector=None, order_manager=None) -> InstantCrossResponse:
    """获取瞬时响应单例"""
    global _instant_response_instance
    if _instant_response_instance is None:
        _instant_response_instance = InstantCrossResponse(connector, order_manager)
    return _instant_response_instance