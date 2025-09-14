#!/usr/bin/env python3
"""
Phase 7.2: 智能批量执行引擎 (IBEE) - Jane Street级别订单管理
基于good version文档中的Phase 7设计

核心理念: "密集小单 + 受控批量 + 生命周期治理"
目标: 部署时间从20-30秒 → <1秒，防止55,000+订单堆积
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)

class OrderLayer(Enum):
    """订单层级"""
    L0_ULTRA_THIN = "L0_ultra_thin"  # 超薄层
    L1_THIN = "L1_thin"              # 薄层  
    L2_MEDIUM = "L2_medium"          # 中层

@dataclass
class LayerConfig:
    """订单层配置"""
    size_range: Tuple[int, int]
    count: int
    spread_bps: float
    refresh_freq_ms: int
    ttl_seconds: int

@dataclass
class BatchOrder:
    """批量订单"""
    symbol: str
    side: str
    quantity: float
    price: float
    layer: OrderLayer
    client_order_id: str
    ttl_seconds: int

class IntelligentBatchExecutor:
    """
    Phase 7.2: 智能批量执行引擎
    实现密集小单 + 批量执行 + 生命周期治理
    """
    
    def __init__(self, max_active_orders: int = 200, batch_size: int = 10):
        # 三层订单结构配置（基于good version文档）
        self.layer_configs = {
            OrderLayer.L0_ULTRA_THIN: LayerConfig(
                size_range=(1, 5),    # 1-5 DOGE
                count=50,             # 50个超小订单
                spread_bps=1.0,       # 1bp价差
                refresh_freq_ms=100,  # 100ms高频更新
                ttl_seconds=3         # 3秒TTL
            ),
            OrderLayer.L1_THIN: LayerConfig(
                size_range=(5, 20),   # 5-20 DOGE
                count=30,             # 30个小订单
                spread_bps=2.0,       # 2bp价差
                refresh_freq_ms=500,  # 500ms更新
                ttl_seconds=10        # 10秒TTL
            ),
            OrderLayer.L2_MEDIUM: LayerConfig(
                size_range=(20, 50),  # 20-50 DOGE
                count=20,             # 20个中订单
                spread_bps=5.0,       # 5bp价差
                refresh_freq_ms=1000, # 1秒更新
                ttl_seconds=30        # 30秒TTL
            )
        }
        
        self.max_active_orders = max_active_orders
        self.batch_size = batch_size
        
        # 订单管理
        self.active_orders: Dict[str, BatchOrder] = {}
        self.order_timestamps: Dict[str, float] = {}
        self.pending_batches: deque = deque()
        
        # 执行统计
        self.stats = {
            'burst_executions': 0,
            'drip_executions': 0,
            'batch_submissions': 0,
            'ttl_cancellations': 0,
            'successful_orders': 0,
            'failed_orders': 0
        }
        
        logger.info(f"[Phase7.2] IntelligentBatchExecutor initialized: max_orders={max_active_orders}, batch_size={batch_size}")

    def generate_order_batch(self, equity: float, mid_price: float, 
                           target_utilization: float = 0.10) -> List[BatchOrder]:
        """
        生成智能订单批次
        
        Args:
            equity: 账户净值
            mid_price: 中间价
            target_utilization: 目标资金利用率
            
        Returns:
            批量订单列表
        """
        if mid_price <= 0:
            return []
            
        target_notional = equity * target_utilization
        orders = []
        order_id_counter = int(time.time() * 1000)
        
        # 按层级分配资金
        layer_allocation = {
            OrderLayer.L0_ULTRA_THIN: 0.70,  # 70%给L0
            OrderLayer.L1_THIN: 0.25,        # 25%给L1
            OrderLayer.L2_MEDIUM: 0.05       # 5%给L2
        }
        
        for layer, allocation in layer_allocation.items():
            config = self.layer_configs[layer]
            layer_notional = target_notional * allocation
            
            # 每侧资金
            side_notional = layer_notional / 2
            
            # 生成买单
            buy_orders = self._generate_side_orders(
                'BUY', side_notional, mid_price, layer, config, order_id_counter
            )
            orders.extend(buy_orders)
            order_id_counter += len(buy_orders)
            
            # 生成卖单  
            sell_orders = self._generate_side_orders(
                'SELL', side_notional, mid_price, layer, config, order_id_counter
            )
            orders.extend(sell_orders)
            order_id_counter += len(sell_orders)
        
        return orders

    def _generate_side_orders(self, side: str, notional: float, mid_price: float,
                            layer: OrderLayer, config: LayerConfig, 
                            start_id: int) -> List[BatchOrder]:
        """生成单侧订单"""
        orders = []
        
        if notional <= 0:
            return orders
            
        # 价格调整
        spread_ratio = config.spread_bps / 10000
        if side == 'BUY':
            base_price = mid_price * (1 - spread_ratio / 2)
        else:
            base_price = mid_price * (1 + spread_ratio / 2)
            
        # 订单数量和大小
        count = min(config.count // 2, 10)  # 限制单侧最多10个订单
        if count <= 0:
            return orders
            
        avg_notional_per_order = notional / count
        
        for i in range(count):
            # 大小变化 (-20% to +20%)
            size_variance = 0.8 + (i / count) * 0.4
            order_notional = avg_notional_per_order * size_variance
            
            # 限制在配置范围内
            min_size, max_size = config.size_range
            quantity = max(min_size, min(max_size, order_notional / mid_price))
            
            # 价格微调 (±0.1bp)
            price_variance = 1 + (i - count/2) * 0.00001
            price = base_price * price_variance
            
            order = BatchOrder(
                symbol='DOGEUSDT',
                side=side,
                quantity=quantity,
                price=price,
                layer=layer,
                client_order_id=f"{side}_L{layer.value[-1]}_{start_id + i}",
                ttl_seconds=config.ttl_seconds
            )
            orders.append(order)
            
        return orders

    async def execute_burst_batch(self, orders: List[BatchOrder], 
                                connector=None) -> Tuple[int, int]:
        """
        突发批量执行：一次性批量生成5-20个小单
        用于资金变化>$30或缺口>10%时的快速部署
        """
        if not orders or not connector:
            return 0, 0
            
        successful = 0
        failed = 0
        
        logger.info(f"[Phase7.2] 🚀 Burst批量执行: {len(orders)}个订单")
        
        # 分批并行提交（每批batch_size个）
        batches = [orders[i:i+self.batch_size] 
                  for i in range(0, len(orders), self.batch_size)]
        
        for batch in batches:
            # 并行提交当前批次
            tasks = []
            for order in batch:
                task = self._submit_single_order(order, connector)
                tasks.append(task)
                
            # 等待当前批次完成
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 统计结果
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    failed += 1
                    logger.warning(f"[Phase7.2] ❌ 批量订单失败: {batch[i].client_order_id} - {result}")
                elif result:  # _submit_single_order返回True/False
                    successful += 1
                    # 订单已在_submit_single_order中记录到active_orders
                else:
                    failed += 1
                    logger.debug(f"[Phase7.2] ⚠️ 批量订单被限流或失败: {batch[i].client_order_id}")
            
            # 避免过快提交
            if len(batches) > 1:
                await asyncio.sleep(0.05)  # 50ms间隔
        
        self.stats['burst_executions'] += 1
        self.stats['batch_submissions'] += len(batches)
        self.stats['successful_orders'] += successful
        self.stats['failed_orders'] += failed
        
        logger.info(f"[Phase7.2] ✅ Burst执行完成: {successful}成功, {failed}失败")
        return successful, failed

    async def execute_drip_补充(self, missing_slots: Dict[str, int],
                              connector=None) -> int:
        """
        滴灌补齐：20-50ms微批节奏补短板，只补缺位槽
        用于维护目标在册金额的增量补充
        """
        if not missing_slots or not connector:
            return 0
            
        补充_orders = []
        
        # 根据缺口生成补充订单
        for layer_side, count in missing_slots.items():
            if count <= 0:
                continue
                
            # 解析layer和side
            parts = layer_side.split('_')
            if len(parts) != 2:
                continue
                
            layer_name, side = parts
            try:
                layer = OrderLayer(f"{layer_name}_thin" if layer_name in ['L0', 'L1'] else f"{layer_name}_medium")
            except ValueError:
                continue
                
            # 生成小批量补充订单  
            config = self.layer_configs[layer]
            for _ in range(min(count, 5)):  # 最多补充5个
                order = BatchOrder(
                    symbol='DOGEUSDT',
                    side=side,
                    quantity=sum(config.size_range) / 2,  # 中等大小
                    price=0.26400,  # 临时价格，实际会调整
                    layer=layer,
                    client_order_id=f"drip_{layer_side}_{int(time.time()*1000)}",
                    ttl_seconds=config.ttl_seconds
                )
                补充_orders.append(order)
        
        if 补充_orders:
            successful, failed = await self.execute_burst_batch(补充_orders, connector)
            self.stats['drip_executions'] += 1
            logger.info(f"[Phase7.2] 💧 Drip补充完成: {successful}成功")
            return successful
            
        return 0

    async def _submit_single_order(self, order: BatchOrder, connector) -> bool:
        """提交单个订单"""
        try:
            # 调用连接器下单
            result = await connector.create_order_v2(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=order.price,
                order_type='LIMIT_MAKER',
                client_order_id=order.client_order_id
            )

            # FillingGate返回None表示被限流阻止
            if result is None:
                logger.debug(f"[Phase7.2] 订单被FillingGate限流: {order.client_order_id}")
                return False

            # 检查是否有真实订单ID
            if isinstance(result, dict) and 'orderId' in result:
                # 记录真实订单ID映射
                self.active_orders[str(result['orderId'])] = order  # 使用真实orderId
                self.order_timestamps[str(result['orderId'])] = time.time()
                logger.info(f"[Phase7.2] ✅ 订单创建成功: {order.client_order_id} -> orderId={result['orderId']}")
                return True

            return False

        except Exception as e:
            logger.error(f"[Phase7.2] 订单提交失败 {order.client_order_id}: {e}")
            return False

    async def cleanup_expired_orders(self, connector=None) -> int:
        """清理过期订单（TTL管理）"""
        if not connector:
            return 0
            
        now = time.time()
        expired_orders = []
        
        for order_id, order in self.active_orders.items():
            if order_id in self.order_timestamps:
                age = now - self.order_timestamps[order_id]
                if age > order.ttl_seconds:
                    expired_orders.append(order_id)
        
        if not expired_orders:
            return 0
            
        # 批量撤单
        cancelled = 0
        for order_id in expired_orders[:20]:  # 限制单次撤单数
            try:
                await connector.cancel_order_v2('DOGEUSDT', order_id)
                self.active_orders.pop(order_id, None)
                self.order_timestamps.pop(order_id, None)
                cancelled += 1
            except Exception as e:
                logger.warning(f"[Phase7.2] TTL撤单失败 {order_id}: {e}")
        
        if cancelled > 0:
            self.stats['ttl_cancellations'] += cancelled
            logger.info(f"[Phase7.2] 🧹 TTL清理: 撤销{cancelled}个过期订单")
            
        return cancelled

    def get_execution_stats(self) -> Dict[str, Any]:
        """获取执行统计"""
        return {
            **self.stats,
            'active_orders': len(self.active_orders),
            'avg_order_age': self._calculate_avg_age(),
            'layer_distribution': self._get_layer_distribution()
        }

    def _calculate_avg_age(self) -> float:
        """计算平均订单年龄"""
        if not self.order_timestamps:
            return 0.0
            
        now = time.time()
        ages = [now - ts for ts in self.order_timestamps.values()]
        return sum(ages) / len(ages)

    def _get_layer_distribution(self) -> Dict[str, int]:
        """获取层级分布"""
        distribution = defaultdict(int)
        for order in self.active_orders.values():
            distribution[order.layer.value] += 1
        return dict(distribution)

# Phase 7.2 集成接口
def create_intelligent_batch_executor(max_orders: int = 200) -> IntelligentBatchExecutor:
    """创建智能批量执行引擎实例"""
    return IntelligentBatchExecutor(max_active_orders=max_orders)

if __name__ == "__main__":
    # 测试代码
    executor = create_intelligent_batch_executor()
    
    # 模拟订单生成
    orders = executor.generate_order_batch(
        equity=1000.0, 
        mid_price=0.26400,
        target_utilization=0.10
    )
    
    print(f"Phase 7.2 智能批量执行引擎测试:")
    print(f"生成订单数: {len(orders)}")
    print(f"层级分布: {executor._get_layer_distribution() if orders else '无订单'}")
    print(f"统计信息: {executor.get_execution_stats()}")