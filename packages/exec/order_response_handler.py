"""
Order Response Handler - SSOT预扣闭环处理器
实现ACK/REJECT/撤单/超时全路径release机制

对标Jane Street/Citadel机构级交易系统标准
目标: reserve_reject_total < 1%
"""
import asyncio
import logging
import time
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Callable, List
from dataclasses import dataclass, field
from threading import Lock
import json

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """标准化订单状态"""
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING_CANCEL = "PENDING_CANCEL"


class OrderResponseType(Enum):
    """订单响应类型"""
    ACK = "ACK"                    # 订单确认
    REJECT = "REJECT"              # 订单拒绝
    FILL = "FILL"                  # 订单成交（部分或完全）
    CANCEL = "CANCEL"              # 订单取消
    EXPIRE = "EXPIRE"              # 订单过期
    TIMEOUT = "TIMEOUT"            # 响应超时


@dataclass
class OrderState:
    """订单状态跟踪"""
    order_id: str
    side: str
    qty: Decimal
    price: Decimal
    status: OrderStatus
    cumulative_qty: Decimal = Decimal(0)
    remaining_qty: Decimal = field(init=False)
    created_time: int = field(default_factory=lambda: time.time_ns())
    last_update_time: int = field(default_factory=lambda: time.time_ns())
    is_reservation_released: bool = False
    reservation_asset: str = ""
    reservation_amount: Decimal = Decimal(0)
    
    def __post_init__(self):
        self.remaining_qty = self.qty - self.cumulative_qty
        # 计算预扣资产和金额
        if self.side == 'BUY':
            self.reservation_asset = 'USDT'
            self.reservation_amount = self.qty * self.price
        else:  # SELL
            self.reservation_asset = 'DOGE'
            self.reservation_amount = self.qty


@dataclass
class OrderResponse:
    """标准化订单响应"""
    order_id: str
    response_type: OrderResponseType
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    cumulative_qty: Decimal = Decimal(0)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: int = field(default_factory=lambda: time.time_ns())


class SSOTReservationClosedLoop:
    """
    SSOT预扣闭环处理器
    
    核心功能:
    1. 追踪所有订单从创建到结束的完整生命周期
    2. 确保每个订单的预扣资金最终都被正确释放
    3. 处理ACK/REJECT/FILL/CANCEL/TIMEOUT全路径
    4. 提供预扣释放的审计追踪
    """
    
    def __init__(self, reservation_model, timeout_seconds: int = 30):
        """
        初始化SSOT预扣闭环处理器
        
        Args:
            reservation_model: 预扣模型实例
            timeout_seconds: 订单响应超时时间
        """
        self.reservation_model = reservation_model
        self.timeout_seconds = timeout_seconds
        self.lock = Lock()
        
        # 订单状态追踪
        self.order_states: Dict[str, OrderState] = {}
        
        # 性能指标
        self.metrics = {
            'total_orders': 0,
            'ack_received': 0,
            'rejects_handled': 0,
            'fills_processed': 0,
            'cancels_processed': 0,
            'timeouts_handled': 0,
            'reservations_released': 0,
            'reservation_release_errors': 0,
            'reserve_reject_total': 0,  # 预扣拒绝总数
            'reserve_reject_rate': 0.0,  # 预扣拒绝率
        }
        
        # 定时任务
        self.cleanup_interval = 60  # 60秒清理一次
        self.last_cleanup_time = time.time()
        
        logger.info("[SSOTReservationClosedLoop] 初始化完成, timeout=%ds", timeout_seconds)
    
    def register_order(self, order_id: str, side: str, qty: Decimal, price: Decimal) -> bool:
        """
        注册新订单到闭环系统
        
        Args:
            order_id: 订单ID
            side: 买卖方向
            qty: 数量
            price: 价格
            
        Returns:
            bool: 注册是否成功
        """
        with self.lock:
            if order_id in self.order_states:
                logger.warning("[SSOTClosedLoop] 订单已存在: %s", order_id)
                return False
            
            # 创建订单状态
            order_state = OrderState(
                order_id=order_id,
                side=side,
                qty=qty,
                price=price,
                status=OrderStatus.NEW
            )
            
            # Phase 7.1 关键修复: 实际创建预扣记录 (之前只计算了金额，没有创建!)
            reservation_success = self.reservation_model.reserve_for_order(
                order_id=order_id,
                side=side,
                qty=qty,
                price=price
            )
            
            if not reservation_success:
                logger.error(
                    "[SSOTClosedLoop] ❌ 预扣创建失败: %s side=%s qty=%s price=%s",
                    order_id, side, qty, price
                )
                self.metrics['reserve_reject_total'] += 1
                return False
            
            self.order_states[order_id] = order_state
            self.metrics['total_orders'] += 1
            
            logger.info(
                "[SSOTClosedLoop] 📝 注册订单: %s side=%s qty=%s price=%s reservation_amount=%s ✅预扣已创建",
                order_id, side, qty, price, order_state.reservation_amount
            )
            
            return True
    
    async def handle_order_response(self, response: OrderResponse) -> bool:
        """
        处理订单响应
        
        Args:
            response: 标准化订单响应
            
        Returns:
            bool: 处理是否成功
        """
        order_id = response.order_id
        
        with self.lock:
            order_state = self.order_states.get(order_id)
            if not order_state:
                logger.error("[SSOTClosedLoop] 未找到订单状态: %s", order_id)
                return False
            
            # 更新订单状态
            order_state.status = response.status
            order_state.cumulative_qty = response.cumulative_qty
            order_state.remaining_qty = order_state.qty - response.cumulative_qty
            order_state.last_update_time = response.timestamp
            
            success = True
            
            # 根据响应类型处理
            if response.response_type == OrderResponseType.ACK:
                success = await self._handle_ack(order_state, response)
                
            elif response.response_type == OrderResponseType.REJECT:
                success = await self._handle_reject(order_state, response)
                
            elif response.response_type == OrderResponseType.FILL:
                success = await self._handle_fill(order_state, response)
                
            elif response.response_type == OrderResponseType.CANCEL:
                success = await self._handle_cancel(order_state, response)
                
            elif response.response_type == OrderResponseType.EXPIRE:
                success = await self._handle_expire(order_state, response)
                
            elif response.response_type == OrderResponseType.TIMEOUT:
                success = await self._handle_timeout(order_state, response)
            
            # 计算预扣拒绝率
            self._update_reject_rate()
            
            return success
    
    async def _handle_ack(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理ACK确认"""
        self.metrics['ack_received'] += 1
        
        logger.info(
            "[SSOTClosedLoop] ✅ ACK: %s status=%s (预扣保持活跃)",
            order_state.order_id, response.status.value
        )
        
        return True
    
    async def _handle_reject(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理REJECT拒绝"""
        self.metrics['rejects_handled'] += 1
        self.metrics['reserve_reject_total'] += 1
        
        # REJECT时需要立即释放预扣
        success = self._release_reservation_safe(
            order_state, 
            reason=f"REJECT: {response.error_message or response.error_code}"
        )
        
        if success:
            logger.info(
                "[SSOTClosedLoop] 🚫 REJECT: %s 预扣已释放 error=%s",
                order_state.order_id, response.error_message or response.error_code
            )
        else:
            logger.error(
                "[SSOTClosedLoop] ❌ REJECT: %s 预扣释放失败",
                order_state.order_id
            )
        
        return success
    
    async def _handle_fill(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理FILL成交"""
        self.metrics['fills_processed'] += 1
        
        if response.status == OrderStatus.FILLED:
            # 完全成交：释放全部预扣
            success = self._release_reservation_safe(
                order_state,
                actual_filled_qty=order_state.qty,
                reason="FILLED"
            )
            
            logger.info(
                "[SSOTClosedLoop] 💰 FILLED: %s 完全成交 预扣已释放",
                order_state.order_id
            )
            
        elif response.status == OrderStatus.PARTIALLY_FILLED:
            # 部分成交：保持预扣活跃，等待后续成交或撤单
            logger.info(
                "[SSOTClosedLoop] 📊 PARTIAL_FILL: %s filled=%s/%s 预扣保持活跃",
                order_state.order_id, response.cumulative_qty, order_state.qty
            )
            success = True
        
        else:
            logger.warning(
                "[SSOTClosedLoop] ⚠️ FILL响应状态异常: %s status=%s",
                order_state.order_id, response.status.value
            )
            success = False
        
        return success
    
    async def _handle_cancel(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理CANCEL撤单"""
        self.metrics['cancels_processed'] += 1
        
        # 撤单时释放未成交部分的预扣
        success = self._release_reservation_safe(
            order_state,
            actual_filled_qty=order_state.cumulative_qty,
            reason="CANCELED"
        )
        
        if success:
            logger.info(
                "[SSOTClosedLoop] 🗑️ CANCELED: %s filled=%s/%s 未成交预扣已释放",
                order_state.order_id, order_state.cumulative_qty, order_state.qty
            )
        else:
            logger.error(
                "[SSOTClosedLoop] ❌ CANCELED: %s 预扣释放失败",
                order_state.order_id
            )
        
        return success
    
    async def _handle_expire(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理EXPIRE过期"""
        # 过期等同于撤单
        return await self._handle_cancel(order_state, response)
    
    async def _handle_timeout(self, order_state: OrderState, response: OrderResponse) -> bool:
        """处理TIMEOUT超时"""
        self.metrics['timeouts_handled'] += 1
        
        # 超时时强制释放预扣（保守处理）
        success = self._release_reservation_safe(
            order_state,
            reason="TIMEOUT"
        )
        
        if success:
            logger.warning(
                "[SSOTClosedLoop] ⏰ TIMEOUT: %s 超时，预扣已强制释放",
                order_state.order_id
            )
        else:
            logger.error(
                "[SSOTClosedLoop] ❌ TIMEOUT: %s 预扣释放失败",
                order_state.order_id
            )
        
        return success
    
    def _release_reservation_safe(self, order_state: OrderState, actual_filled_qty: Optional[Decimal] = None, reason: str = "") -> bool:
        """
        安全释放预扣资金
        
        Args:
            order_state: 订单状态
            actual_filled_qty: 实际成交数量
            reason: 释放原因
            
        Returns:
            bool: 释放是否成功
        """
        if order_state.is_reservation_released:
            logger.debug(
                "[SSOTClosedLoop] 预扣已释放，跳过: %s",
                order_state.order_id
            )
            return True
        
        try:
            success = self.reservation_model.release_reservation(
                order_state.order_id,
                actual_filled_qty
            )
            
            if success:
                order_state.is_reservation_released = True
                self.metrics['reservations_released'] += 1
                
                logger.info(
                    "[SSOTClosedLoop] ✅ 预扣释放成功: %s reason=%s asset=%s amount=%s",
                    order_state.order_id, reason, order_state.reservation_asset, order_state.reservation_amount
                )
            else:
                self.metrics['reservation_release_errors'] += 1
                logger.error(
                    "[SSOTClosedLoop] ❌ 预扣释放失败: %s reason=%s",
                    order_state.order_id, reason
                )
            
            return success
            
        except Exception as e:
            self.metrics['reservation_release_errors'] += 1
            logger.error(
                "[SSOTClosedLoop] 预扣释放异常: %s reason=%s error=%s",
                order_state.order_id, reason, str(e), exc_info=True
            )
            return False
    
    def _update_reject_rate(self):
        """更新预扣拒绝率"""
        if self.metrics['total_orders'] > 0:
            self.metrics['reserve_reject_rate'] = (
                self.metrics['reserve_reject_total'] / self.metrics['total_orders'] * 100.0
            )
    
    def cleanup_completed_orders(self, max_age_seconds: int = 300) -> int:
        """
        清理已完成的订单状态
        
        Args:
            max_age_seconds: 最大保留时间（秒）
            
        Returns:
            int: 清理的订单数量
        """
        current_time = time.time_ns()
        completed_statuses = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
        
        orders_to_cleanup = []
        
        with self.lock:
            for order_id, order_state in self.order_states.items():
                if order_state.status in completed_statuses:
                    age_seconds = (current_time - order_state.last_update_time) / 1e9
                    if age_seconds > max_age_seconds:
                        orders_to_cleanup.append(order_id)
            
            # 执行清理
            for order_id in orders_to_cleanup:
                del self.order_states[order_id]
            
            self.last_cleanup_time = time.time()
        
        if orders_to_cleanup:
            logger.info(
                "[SSOTClosedLoop] 🧹 清理完成订单: %d个订单, 超时时间=%ds",
                len(orders_to_cleanup), max_age_seconds
            )
        
        return len(orders_to_cleanup)
    
    def get_system_health(self) -> Dict[str, any]:
        """获取系统健康状态"""
        with self.lock:
            active_orders = len(self.order_states)
            unreleased_reservations = sum(
                1 for state in self.order_states.values()
                if not state.is_reservation_released
            )
            
            return {
                'active_orders': active_orders,
                'unreleased_reservations': unreleased_reservations,
                'metrics': self.metrics.copy(),
                'reserve_reject_rate': self.metrics['reserve_reject_rate'],
                'target_achieved': self.metrics['reserve_reject_rate'] < 1.0,  # 目标<1%
                'last_cleanup_age': time.time() - self.last_cleanup_time
            }
    
    def force_release_all_reservations(self) -> int:
        """
        紧急情况：强制释放所有未释放的预扣
        
        Returns:
            int: 释放的预扣数量
        """
        released_count = 0
        
        with self.lock:
            for order_state in self.order_states.values():
                if not order_state.is_reservation_released:
                    success = self._release_reservation_safe(
                        order_state,
                        reason="FORCE_RELEASE_ALL"
                    )
                    if success:
                        released_count += 1
        
        logger.warning(
            "[SSOTClosedLoop] 🚨 强制释放所有预扣: %d个预扣已释放",
            released_count
        )
        
        return released_count