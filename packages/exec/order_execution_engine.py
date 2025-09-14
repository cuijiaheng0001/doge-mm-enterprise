"""
Order Execution Engine - 订单执行引擎
配合SSOT预扣闭环实现完整的订单生命周期管理

模拟真实订单执行场景，用于验证预扣闭环机制
"""
import asyncio
import logging
import random
import time
from decimal import Decimal
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass
import json

from .order_response_handler import (
    SSOTReservationClosedLoop, 
    OrderResponse, 
    OrderResponseType, 
    OrderStatus
)

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """订单请求"""
    order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    order_type: str = "LIMIT"
    time_in_force: str = "GTC"


class MockOrderExecutionEngine:
    """
    模拟订单执行引擎
    
    用于测试SSOT预扣闭环机制，模拟各种订单响应场景：
    - 正常ACK->FILL流程
    - 订单REJECT场景
    - 部分成交场景
    - 订单CANCEL场景
    - 响应TIMEOUT场景
    """
    
    def __init__(self, closed_loop_handler: SSOTReservationClosedLoop):
        """
        初始化模拟执行引擎
        
        Args:
            closed_loop_handler: SSOT预扣闭环处理器
        """
        self.closed_loop = closed_loop_handler
        self.running = False
        
        # 模拟参数
        self.reject_rate = 0.02        # 2%拒绝率（目标<1%，但测试需要一些拒绝）
        self.partial_fill_rate = 0.15  # 15%部分成交率
        self.timeout_rate = 0.01       # 1%超时率
        self.cancel_rate = 0.05        # 5%主动撤单率
        
        # 延迟模拟 (毫秒)
        self.ack_delay_range = (10, 50)      # ACK延迟10-50ms
        self.fill_delay_range = (50, 200)    # FILL延迟50-200ms
        self.cancel_delay_range = (30, 100)  # CANCEL延迟30-100ms
        
        # 订单追踪
        self.pending_orders: Dict[str, OrderRequest] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
        
        # 统计
        self.stats = {
            'orders_submitted': 0,
            'orders_acked': 0,
            'orders_rejected': 0,
            'orders_filled': 0,
            'orders_partial_filled': 0,
            'orders_canceled': 0,
            'orders_timeout': 0,
        }
        
        logger.info("[MockOrderEngine] 初始化完成")
    
    async def submit_order(self, request: OrderRequest) -> bool:
        """
        提交订单
        
        Args:
            request: 订单请求
            
        Returns:
            bool: 提交是否成功
        """
        order_id = request.order_id
        
        if order_id in self.pending_orders:
            logger.error("[MockOrderEngine] 订单ID重复: %s", order_id)
            return False
        
        # 注册到闭环系统
        success = self.closed_loop.register_order(
            order_id, request.side, request.qty, request.price
        )
        
        if not success:
            logger.error("[MockOrderEngine] 闭环注册失败: %s", order_id)
            return False
        
        # 记录订单
        self.pending_orders[order_id] = request
        self.stats['orders_submitted'] += 1
        
        # 启动异步处理任务
        task = asyncio.create_task(self._process_order(request))
        self.active_tasks[order_id] = task
        
        logger.info(
            "[MockOrderEngine] 📤 提交订单: %s %s %s@%s",
            order_id, request.side, request.qty, request.price
        )
        
        return True
    
    async def cancel_order(self, order_id: str) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单ID
            
        Returns:
            bool: 取消是否成功
        """
        request = self.pending_orders.get(order_id)
        if not request:
            logger.warning("[MockOrderEngine] 订单不存在，无法取消: %s", order_id)
            return False
        
        # 取消异步任务
        task = self.active_tasks.get(order_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # 发送CANCEL响应
        await self._send_cancel_response(order_id)
        
        return True
    
    async def _process_order(self, request: OrderRequest):
        """
        处理单个订单的完整生命周期
        
        Args:
            request: 订单请求
        """
        order_id = request.order_id
        
        try:
            # 决定订单命运
            fate = self._decide_order_fate()
            
            # 模拟网络延迟和处理时间
            ack_delay = random.uniform(*self.ack_delay_range) / 1000  # 转为秒
            await asyncio.sleep(ack_delay)
            
            if fate == 'reject':
                # 发送REJECT响应
                await self._send_reject_response(order_id)
                
            elif fate == 'timeout':
                # 模拟超时（不发送响应）
                timeout_delay = 35  # 超过30秒超时阈值
                await asyncio.sleep(timeout_delay)
                await self._send_timeout_response(order_id)
                
            else:
                # 发送ACK响应
                await self._send_ack_response(order_id)
                
                # 等待成交或撤单
                if fate == 'cancel':
                    # 模拟一段时间后主动撤单
                    cancel_delay = random.uniform(1.0, 3.0)
                    await asyncio.sleep(cancel_delay)
                    await self._send_cancel_response(order_id)
                    
                elif fate == 'fill':
                    # 模拟完全成交
                    fill_delay = random.uniform(*self.fill_delay_range) / 1000
                    await asyncio.sleep(fill_delay)
                    await self._send_fill_response(order_id, request.qty)
                    
                elif fate == 'partial_fill':
                    # 模拟部分成交
                    fill_delay = random.uniform(*self.fill_delay_range) / 1000
                    await asyncio.sleep(fill_delay)
                    
                    # 随机部分成交比例 (30%-80%)
                    fill_ratio = random.uniform(0.3, 0.8)
                    filled_qty = request.qty * Decimal(str(fill_ratio))
                    
                    await self._send_partial_fill_response(order_id, filled_qty, request.qty)
                    
                    # 等待一段时间后撤单剩余部分
                    cancel_delay = random.uniform(2.0, 5.0)
                    await asyncio.sleep(cancel_delay)
                    await self._send_cancel_response(order_id, filled_qty)
        
        except asyncio.CancelledError:
            # 任务被取消
            logger.info("[MockOrderEngine] 订单处理被取消: %s", order_id)
            
        except Exception as e:
            logger.error(
                "[MockOrderEngine] 订单处理异常: %s error=%s",
                order_id, str(e), exc_info=True
            )
            
        finally:
            # 清理
            self.pending_orders.pop(order_id, None)
            self.active_tasks.pop(order_id, None)
    
    def _decide_order_fate(self) -> str:
        """
        决定订单命运
        
        Returns:
            str: 'reject', 'timeout', 'cancel', 'fill', 'partial_fill'
        """
        rand = random.random()
        
        if rand < self.reject_rate:
            return 'reject'
        elif rand < self.reject_rate + self.timeout_rate:
            return 'timeout'
        elif rand < self.reject_rate + self.timeout_rate + self.cancel_rate:
            return 'cancel'
        elif rand < self.reject_rate + self.timeout_rate + self.cancel_rate + self.partial_fill_rate:
            return 'partial_fill'
        else:
            return 'fill'
    
    async def _send_ack_response(self, order_id: str):
        """发送ACK响应"""
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.ACK,
            status=OrderStatus.NEW
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_acked'] += 1
        
        logger.info("[MockOrderEngine] ✅ ACK: %s", order_id)
    
    async def _send_reject_response(self, order_id: str):
        """发送REJECT响应"""
        error_messages = [
            "Insufficient balance",
            "Price not allowed",
            "Market closed",
            "Symbol not found",
            "Order size too small"
        ]
        
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.REJECT,
            status=OrderStatus.REJECTED,
            error_code="-2010",
            error_message=random.choice(error_messages)
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_rejected'] += 1
        
        logger.info("[MockOrderEngine] 🚫 REJECT: %s", order_id)
    
    async def _send_fill_response(self, order_id: str, filled_qty: Decimal):
        """发送FILL响应"""
        request = self.pending_orders.get(order_id)
        if not request:
            return
        
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.FILL,
            status=OrderStatus.FILLED,
            filled_qty=filled_qty,
            cumulative_qty=filled_qty,
            avg_price=request.price
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_filled'] += 1
        
        logger.info("[MockOrderEngine] 💰 FILLED: %s qty=%s", order_id, filled_qty)
    
    async def _send_partial_fill_response(self, order_id: str, filled_qty: Decimal, total_qty: Decimal):
        """发送部分成交响应"""
        request = self.pending_orders.get(order_id)
        if not request:
            return
        
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.FILL,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=filled_qty,
            cumulative_qty=filled_qty,
            avg_price=request.price
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_partial_filled'] += 1
        
        logger.info(
            "[MockOrderEngine] 📊 PARTIAL_FILL: %s filled=%s/%s",
            order_id, filled_qty, total_qty
        )
    
    async def _send_cancel_response(self, order_id: str, filled_qty: Decimal = Decimal(0)):
        """发送CANCEL响应"""
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.CANCEL,
            status=OrderStatus.CANCELED,
            cumulative_qty=filled_qty
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_canceled'] += 1
        
        logger.info("[MockOrderEngine] 🗑️ CANCELED: %s filled=%s", order_id, filled_qty)
    
    async def _send_timeout_response(self, order_id: str):
        """发送TIMEOUT响应"""
        response = OrderResponse(
            order_id=order_id,
            response_type=OrderResponseType.TIMEOUT,
            status=OrderStatus.EXPIRED
        )
        
        await self.closed_loop.handle_order_response(response)
        self.stats['orders_timeout'] += 1
        
        logger.warning("[MockOrderEngine] ⏰ TIMEOUT: %s", order_id)
    
    def get_engine_stats(self) -> Dict[str, any]:
        """获取引擎统计"""
        return {
            'stats': self.stats.copy(),
            'pending_orders': len(self.pending_orders),
            'active_tasks': len(self.active_tasks),
            'rates': {
                'reject_rate': self.reject_rate * 100,
                'timeout_rate': self.timeout_rate * 100,
                'cancel_rate': self.cancel_rate * 100,
                'partial_fill_rate': self.partial_fill_rate * 100
            }
        }
    
    async def shutdown(self):
        """关闭引擎"""
        logger.info("[MockOrderEngine] 开始关闭...")
        
        # 取消所有活跃任务
        for order_id, task in self.active_tasks.items():
            if not task.done():
                task.cancel()
        
        # 等待任务完成
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)
        
        logger.info("[MockOrderEngine] 关闭完成")