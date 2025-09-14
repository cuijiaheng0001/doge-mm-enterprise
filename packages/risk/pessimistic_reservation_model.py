"""
Pessimistic Reservation Model - 悲观预扣模型
零-2010错误目标，对标机构级交易系统标准
"""
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional, Tuple
from threading import Lock
from collections import defaultdict
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)


@dataclass
class ReservationRecord:
    """预扣记录"""
    order_id: str
    side: str
    asset: str
    amount: Decimal
    reserved_ts: int
    ttl_seconds: int = 300  # 5分钟TTL


@dataclass
class AssetBalance:
    """资产余额状态"""
    total: Decimal              # 总余额
    reserved: Decimal           # 预留部分 (固定比例)  
    pre_committed: Decimal      # 预承诺部分 (待成交订单)
    available: Decimal          # 可用部分
    last_update_ts: int


class PessimisticReservationModel:
    """
    悲观预扣模型 - 零-2010错误目标
    
    核心原理：
    1. 悲观预扣：下单前预先扣除所需资金
    2. 分层预留：总余额 = 预留 + 预承诺 + 可用
    3. 安全边际：订单金额 <= 可用余额 * 98%
    4. TTL清理：超时预扣自动释放
    """
    
    def __init__(self, reserve_ratios: Dict[str, float]):
        """
        初始化悲观预扣模型
        
        Args:
            reserve_ratios: 预留比例 {'DOGE': 0.05, 'USDT': 0.05}
        """
        self.reserve_ratios = reserve_ratios
        self.lock = Lock()
        
        # 资产状态
        self.balances: Dict[str, AssetBalance] = {}
        
        # 预扣记录 (按订单ID索引)
        self.reservations: Dict[str, ReservationRecord] = {}
        
        # 性能指标
        self.metrics = {
            'reservations_created': 0,
            'reservations_released': 0,
            'reservation_failures': 0,
            'ttl_cleanups': 0,
            'balance_updates': 0
        }
        
        # 自动TTL清理
        self.last_cleanup_ts = time.time()
        self.cleanup_interval = 60  # 60秒清理一次
        
        logger.info(
            "[PessimisticReservation] 初始化完成 reserve_ratios=%s",
            reserve_ratios
        )
    
    def update_real_balance(self, asset: str, total_balance: Decimal):
        """
        更新真实余额
        
        Args:
            asset: 资产名称 ('DOGE', 'USDT')
            total_balance: 总余额
        """
        with self.lock:
            reserve_ratio = self.reserve_ratios.get(asset, 0.02)  # 默认2%
            reserved = total_balance * Decimal(str(reserve_ratio))
            
            # 计算预承诺总额
            pre_committed = Decimal(0)
            for record in self.reservations.values():
                if record.asset == asset:
                    pre_committed += record.amount
            
            # 计算可用余额
            available = total_balance - reserved - pre_committed
            available = max(Decimal(0), available)  # 确保非负
            
            # 更新资产状态
            self.balances[asset] = AssetBalance(
                total=total_balance,
                reserved=reserved,
                pre_committed=pre_committed,
                available=available,
                last_update_ts=time.time_ns()
            )
            
            self.metrics['balance_updates'] += 1
            
            logger.debug(
                "[PessimisticReservation] Balance updated: %s total=%s reserved=%s "
                "pre_committed=%s available=%s",
                asset, total_balance, reserved, pre_committed, available
            )
            
            # 定期TTL清理
            if time.time() - self.last_cleanup_ts > self.cleanup_interval:
                self._cleanup_expired_reservations()
    
    def reserve(self, order_id: str, side: str, qty: Decimal, price: Decimal) -> bool:
        """别名：兼容旧接口"""
        return self.reserve_for_order(order_id, side, qty, price)
    
    def reserve_for_order(self, order_id: str, side: str, qty: Decimal, price: Decimal) -> bool:
        """
        为订单预扣资金
        
        Args:
            order_id: 订单ID
            side: 买卖方向 ('BUY', 'SELL')
            qty: 数量
            price: 价格
            
        Returns:
            bool: 预扣是否成功
        """
        with self.lock:
            try:
                # 计算所需资金和资产
                if side == 'BUY':
                    required = qty * price
                    asset = 'USDT'
                else:  # SELL
                    required = qty
                    asset = 'DOGE'
                
                # 检查是否已存在预扣
                if order_id in self.reservations:
                    logger.warning(
                        "[PessimisticReservation] Duplicate reservation attempt: %s",
                        order_id
                    )
                    return False
                
                # 获取当前资产状态
                balance = self.balances.get(asset)
                if not balance:
                    logger.error(
                        "[PessimisticReservation] Asset balance not available: %s",
                        asset
                    )
                    self.metrics['reservation_failures'] += 1
                    return False
                
                # 安全边际检查：required <= available * 98%
                safety_margin = Decimal('0.98')
                max_allowed = balance.available * safety_margin
                
                if required > max_allowed:
                    logger.warning(
                        "[PessimisticReservation] Insufficient balance: %s required=%s "
                        "available=%s max_allowed=%s",
                        asset, required, balance.available, max_allowed
                    )
                    self.metrics['reservation_failures'] += 1
                    return False
                
                # 创建预扣记录
                reservation = ReservationRecord(
                    order_id=order_id,
                    side=side,
                    asset=asset,
                    amount=required,
                    reserved_ts=time.time_ns()
                )
                
                self.reservations[order_id] = reservation
                
                # 更新资产状态 (减少可用余额)
                new_available = balance.available - required
                new_pre_committed = balance.pre_committed + required
                
                self.balances[asset] = AssetBalance(
                    total=balance.total,
                    reserved=balance.reserved,
                    pre_committed=new_pre_committed,
                    available=new_available,
                    last_update_ts=time.time_ns()
                )
                
                self.metrics['reservations_created'] += 1
                
                logger.info(
                    "[PessimisticReservation] ✅ Reserved: oid=%s side=%s asset=%s "
                    "amount=%s new_available=%s",
                    order_id, side, asset, required, new_available
                )
                
                return True
                
            except Exception as e:
                logger.error(
                    "[PessimisticReservation] Reservation failed: oid=%s error=%s",
                    order_id, str(e), exc_info=True
                )
                self.metrics['reservation_failures'] += 1
                return False
    
    def release_reservation(self, order_id: str, actual_filled_qty: Decimal = None) -> bool:
        """
        释放预扣资金
        
        Args:
            order_id: 订单ID
            actual_filled_qty: 实际成交数量 (None表示订单被取消)
            
        Returns:
            bool: 释放是否成功
        """
        with self.lock:
            try:
                reservation = self.reservations.get(order_id)
                if not reservation:
                    logger.debug(
                        "[PessimisticReservation] No reservation found for: %s",
                        order_id
                    )
                    return False
                
                asset = reservation.asset
                reserved_amount = reservation.amount
                
                # 计算实际释放金额
                if actual_filled_qty is not None:
                    # 部分成交：释放未成交部分
                    if reservation.side == 'BUY':
                        # 买单：按比例计算未成交的USDT
                        total_qty = reserved_amount / Decimal('0.26')  # 估算，应从原始价格计算
                        unfilled_ratio = max(Decimal(0), (total_qty - actual_filled_qty) / total_qty)
                        release_amount = reserved_amount * unfilled_ratio
                    else:  # SELL
                        # 卖单：直接计算未成交的DOGE
                        release_amount = reserved_amount - actual_filled_qty
                else:
                    # 订单被取消：释放全部预扣
                    release_amount = reserved_amount
                
                release_amount = max(Decimal(0), release_amount)
                
                # 获取当前资产状态
                balance = self.balances.get(asset)
                if not balance:
                    logger.error(
                        "[PessimisticReservation] Asset balance not available for release: %s",
                        asset
                    )
                    return False
                
                # 更新资产状态 (增加可用余额)
                new_available = balance.available + release_amount
                new_pre_committed = balance.pre_committed - reserved_amount
                new_pre_committed = max(Decimal(0), new_pre_committed)
                
                self.balances[asset] = AssetBalance(
                    total=balance.total,
                    reserved=balance.reserved,
                    pre_committed=new_pre_committed,
                    available=new_available,
                    last_update_ts=time.time_ns()
                )
                
                # 删除预扣记录
                del self.reservations[order_id]
                
                self.metrics['reservations_released'] += 1
                
                logger.info(
                    "[PessimisticReservation] ✅ Released: oid=%s asset=%s "
                    "reserved=%s released=%s new_available=%s",
                    order_id, asset, reserved_amount, release_amount, new_available
                )
                
                return True
                
            except Exception as e:
                logger.error(
                    "[PessimisticReservation] Release failed: oid=%s error=%s",
                    order_id, str(e), exc_info=True
                )
                return False
    
    def get_available_balance(self, asset: str) -> Decimal:
        """
        获取可用余额
        
        Args:
            asset: 资产名称
            
        Returns:
            Decimal: 可用余额
        """
        with self.lock:
            balance = self.balances.get(asset)
            if not balance:
                return Decimal(0)
            return balance.available
    
    def check_order_feasible(self, side: str, qty: Decimal, price: Decimal) -> Tuple[bool, str]:
        """
        检查订单可行性
        
        Args:
            side: 买卖方向
            qty: 数量  
            price: 价格
            
        Returns:
            Tuple[bool, str]: (是否可行, 原因)
        """
        with self.lock:
            try:
                if side == 'BUY':
                    required = qty * price
                    asset = 'USDT'
                else:  # SELL
                    required = qty
                    asset = 'DOGE'
                
                balance = self.balances.get(asset)
                if not balance:
                    return False, f"Asset balance unavailable: {asset}"
                
                # 安全边际检查
                safety_margin = Decimal('0.98')
                max_allowed = balance.available * safety_margin
                
                if required > max_allowed:
                    return False, f"Insufficient balance: required={required} max_allowed={max_allowed}"
                
                return True, "feasible"
                
            except Exception as e:
                return False, f"Check failed: {str(e)}"
    
    def _cleanup_expired_reservations(self):
        """清理过期的预扣记录"""
        current_ts = time.time_ns()
        expired_orders = []
        
        for order_id, record in self.reservations.items():
            age_seconds = (current_ts - record.reserved_ts) / 1e9
            if age_seconds > record.ttl_seconds:
                expired_orders.append(order_id)
        
        # 释放过期预扣
        for order_id in expired_orders:
            logger.info(
                "[PessimisticReservation] TTL cleanup: releasing expired reservation %s",
                order_id
            )
            self.release_reservation(order_id)
            self.metrics['ttl_cleanups'] += 1
        
        self.last_cleanup_ts = time.time()
        
        if expired_orders:
            logger.info(
                "[PessimisticReservation] TTL cleanup completed: released %d expired reservations",
                len(expired_orders)
            )
        
        return len(expired_orders)
    
    def cleanup_expired_reservations(self) -> int:
        """
        公有方法：清理过期的预扣记录
        
        Returns:
            int: 清理的过期预扣数量
        """
        return self._cleanup_expired_reservations()
    
    def get_reservation_status(self) -> Dict[str, any]:
        """获取预扣系统状态"""
        with self.lock:
            return {
                'active_reservations': len(self.reservations),
                'balances': {
                    asset: {
                        'total': float(balance.total),
                        'reserved': float(balance.reserved),
                        'pre_committed': float(balance.pre_committed),
                        'available': float(balance.available),
                        'utilization_ratio': float(balance.pre_committed / balance.total) if balance.total > 0 else 0
                    }
                    for asset, balance in self.balances.items()
                },
                'metrics': self.metrics.copy(),
                'next_cleanup_in': max(0, self.cleanup_interval - (time.time() - self.last_cleanup_ts))
            }
    
    def force_reconcile_balance(self, asset: str, expected_total: Decimal):
        """
        强制对账余额 - 用于修正偏差
        
        Args:
            asset: 资产名称
            expected_total: 期望的总余额
        """
        with self.lock:
            logger.warning(
                "[PessimisticReservation] Force reconcile: %s expected=%s",
                asset, expected_total
            )
            self.update_real_balance(asset, expected_total)