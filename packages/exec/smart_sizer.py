"""
SmartSizer: Phase 5 机构级智能订单拆分系统
解决"单笔过大"问题，实现micro-lots智能分发
"""
import time
import logging
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import math

logger = logging.getLogger(__name__)


class SizingStrategy(Enum):
    """拆分策略枚举"""
    UNIFORM = "uniform"           # 均匀拆分
    FIBONACCI = "fibonacci"       # 斐波那契递增
    MARKET_ADAPTIVE = "adaptive"  # 市场深度自适应
    IMBALANCE_AWARE = "imbalance" # 库存失衡感知


@dataclass
class MicroLot:
    """Micro-lot 数据结构"""
    lot_id: str
    original_order_id: str
    side: str
    quantity: float
    price: float
    priority: int
    created_time: float
    timeout: float
    strategy: SizingStrategy
    metadata: Dict[str, Any]
    
    @property
    def notional_value(self) -> float:
        """名义金额"""
        return self.quantity * self.price
    
    @property
    def is_expired(self) -> bool:
        """是否过期"""
        return time.time() > self.timeout


class SmartSizer:
    """
    Phase 5 机构级智能订单拆分器
    
    核心功能：
    1. 大订单智能拆分为micro-lots
    2. 基于市场深度动态调整拆分粒度
    3. 库存失衡感知的拆分优先级
    4. 执行队列管理和超时控制
    """
    
    def __init__(self, 
                 min_lot_usd: float = 10.0,
                 max_lot_usd: float = 100.0,
                 max_lots_per_order: int = 10,
                 lot_timeout: float = 300.0):
        """
        初始化SmartSizer
        
        Args:
            min_lot_usd: 最小micro-lot金额(USD)
            max_lot_usd: 最大micro-lot金额(USD)
            max_lots_per_order: 单个订单最大拆分数量
            lot_timeout: micro-lot超时时间(秒)
        """
        # 基础配置
        self.min_lot_usd = min_lot_usd
        self.max_lot_usd = max_lot_usd
        self.max_lots_per_order = max_lots_per_order
        self.lot_timeout = lot_timeout
        
        # 拆分阈值 - Phase 5：针对-2010错误优化
        self.split_threshold_usd = 200.0  # 超过200USD开始拆分
        self.emergency_split_threshold = 500.0  # 超过500USD强制拆分
        
        # 执行队列
        self.pending_lots: List[MicroLot] = []
        self.executing_lots: Dict[str, MicroLot] = {}
        self.completed_lots: List[MicroLot] = []
        
        # 统计信息
        self.stats = {
            'total_orders_processed': 0,
            'total_lots_created': 0,
            'split_ratio_avg': 0.0,
            'execution_success_rate': 0.0,
            'avg_lot_size_usd': 0.0
        }
        
        # 市场状态缓存
        self.market_depth_cache = {}
        self.last_depth_update = 0
        
        logger.info(f"[SmartSizer] 初始化完成: min={self.min_lot_usd}USD, "
                   f"max={self.max_lot_usd}USD, split_threshold={self.split_threshold_usd}USD")
    
    def should_split_order(self, notional_usd: float, market_data: Dict[str, Any]) -> bool:
        """
        判断是否需要拆分订单
        
        Args:
            notional_usd: 订单名义金额(USD)
            market_data: 市场数据
            
        Returns:
            bool: 是否需要拆分
        """
        # 基础阈值检查
        if notional_usd < self.split_threshold_usd:
            return False
        
        # 紧急拆分 - 大订单强制拆分
        if notional_usd > self.emergency_split_threshold:
            logger.info(f"[SmartSizer] 紧急拆分触发: {notional_usd:.2f}USD > {self.emergency_split_threshold}USD")
            return True
        
        # 市场流动性检查
        bid_depth = market_data.get('bid_depth_5', 0)
        ask_depth = market_data.get('ask_depth_5', 0)
        
        # 如果市场深度不足，拆分订单以降低市场冲击
        if notional_usd > min(bid_depth, ask_depth) * 0.3:
            logger.info(f"[SmartSizer] 流动性拆分触发: notional={notional_usd:.2f} vs depth={min(bid_depth, ask_depth):.2f}")
            return True
        
        return True  # Phase 5: 积极拆分策略
    
    def calculate_optimal_split(self, notional_usd: float, side: str, 
                              market_data: Dict[str, Any],
                              imbalance_ratio: float = 0.0) -> Tuple[int, SizingStrategy]:
        """
        计算最优拆分数量和策略
        
        Args:
            notional_usd: 订单名义金额
            side: 订单方向 (BUY/SELL)
            market_data: 市场数据
            imbalance_ratio: 库存失衡比例 [-1, 1]
            
        Returns:
            (拆分数量, 使用策略)
        """
        # 基础拆分计算
        base_splits = min(
            self.max_lots_per_order,
            max(2, int(notional_usd / self.max_lot_usd))
        )
        
        # 市场深度调整
        total_depth = market_data.get('bid_depth_5', 0) + market_data.get('ask_depth_5', 0)
        if total_depth > 0:
            # 流动性充足时减少拆分，提高效率
            if notional_usd < total_depth * 0.1:
                base_splits = max(2, base_splits // 2)
        
        # 库存失衡调整
        if abs(imbalance_ratio) > 0.2:  # 20%失衡阈值
            if ((side == 'BUY' and imbalance_ratio < -0.2) or 
                (side == 'SELL' and imbalance_ratio > 0.2)):
                # 需要紧急平衡库存，增加拆分以提高成功率
                base_splits = min(self.max_lots_per_order, base_splits + 2)
                strategy = SizingStrategy.IMBALANCE_AWARE
            else:
                strategy = SizingStrategy.MARKET_ADAPTIVE
        else:
            strategy = SizingStrategy.UNIFORM
        
        final_splits = max(2, min(self.max_lots_per_order, base_splits))
        
        logger.debug(f"[SmartSizer] 拆分计算: {notional_usd:.2f}USD → {final_splits}lots, "
                    f"strategy={strategy.value}, imbalance={imbalance_ratio:.2f}")
        
        return final_splits, strategy
    
    def create_micro_lots(self, order_id: str, side: str, total_quantity: float, 
                         price: float, strategy: SizingStrategy, 
                         num_lots: int) -> List[MicroLot]:
        """
        创建micro-lots
        
        Args:
            order_id: 原始订单ID
            side: 订单方向
            total_quantity: 总数量
            price: 价格
            strategy: 拆分策略
            num_lots: 拆分数量
            
        Returns:
            List[MicroLot]: micro-lot列表
        """
        lots = []
        now = time.time()
        
        if strategy == SizingStrategy.UNIFORM:
            # 均匀拆分
            base_qty = total_quantity / num_lots
            for i in range(num_lots):
                qty = base_qty if i < num_lots - 1 else total_quantity - base_qty * i
                lot = MicroLot(
                    lot_id=f"{order_id}_lot_{i+1}",
                    original_order_id=order_id,
                    side=side,
                    quantity=round(qty, 2),
                    price=price,
                    priority=i + 1,
                    created_time=now,
                    timeout=now + self.lot_timeout,
                    strategy=strategy,
                    metadata={'batch_size': num_lots, 'lot_index': i}
                )
                lots.append(lot)
        
        elif strategy == SizingStrategy.FIBONACCI:
            # 斐波那契递增拆分（小→大）
            fib_sequence = self._generate_fibonacci_weights(num_lots)
            total_weight = sum(fib_sequence)
            
            for i, weight in enumerate(fib_sequence):
                qty = (weight / total_weight) * total_quantity
                lot = MicroLot(
                    lot_id=f"{order_id}_fib_{i+1}",
                    original_order_id=order_id,
                    side=side,
                    quantity=round(qty, 2),
                    price=price,
                    priority=i + 1,
                    created_time=now,
                    timeout=now + self.lot_timeout,
                    strategy=strategy,
                    metadata={'fib_weight': weight, 'total_weight': total_weight}
                )
                lots.append(lot)
        
        elif strategy == SizingStrategy.IMBALANCE_AWARE:
            # 库存失衡感知拆分：前小后大，快速开始
            for i in range(num_lots):
                # 指数增长权重
                weight = 2 ** i if i < num_lots - 1 else 2 ** (num_lots - 1)
                total_weight = sum(2 ** j for j in range(num_lots - 1)) + 2 ** (num_lots - 1)
                
                qty = (weight / total_weight) * total_quantity
                lot = MicroLot(
                    lot_id=f"{order_id}_imb_{i+1}",
                    original_order_id=order_id,
                    side=side,
                    quantity=round(qty, 2),
                    price=price,
                    priority=i + 1,  # 低优先级数字 = 高优先级执行
                    created_time=now,
                    timeout=now + self.lot_timeout,
                    strategy=strategy,
                    metadata={'weight': weight, 'urgent': i == 0}
                )
                lots.append(lot)
        
        # 按优先级排序
        lots.sort(key=lambda x: x.priority)
        
        logger.info(f"[SmartSizer] 创建{len(lots)}个micro-lots: "
                   f"总量{total_quantity:.2f}, 策略={strategy.value}")
        
        return lots
    
    def _generate_fibonacci_weights(self, n: int) -> List[int]:
        """生成斐波那契权重序列"""
        if n <= 0:
            return []
        elif n == 1:
            return [1]
        elif n == 2:
            return [1, 1]
        
        fib = [1, 1]
        for i in range(2, n):
            fib.append(fib[i-1] + fib[i-2])
        
        return fib
    
    def split_order(self, order_id: str, side: str, quantity: float, 
                   price: float, market_data: Dict[str, Any],
                   imbalance_ratio: float = 0.0) -> List[MicroLot]:
        """
        智能拆分订单
        
        Args:
            order_id: 订单ID
            side: 订单方向
            quantity: 数量
            price: 价格
            market_data: 市场数据
            imbalance_ratio: 库存失衡比例
            
        Returns:
            List[MicroLot]: 拆分后的micro-lot列表
        """
        notional_usd = quantity * price
        
        # 检查是否需要拆分
        if not self.should_split_order(notional_usd, market_data):
            # 不需要拆分，返回单个lot
            lot = MicroLot(
                lot_id=f"{order_id}_single",
                original_order_id=order_id,
                side=side,
                quantity=quantity,
                price=price,
                priority=1,
                created_time=time.time(),
                timeout=time.time() + self.lot_timeout,
                strategy=SizingStrategy.UNIFORM,
                metadata={'single_order': True}
            )
            return [lot]
        
        # 计算最优拆分
        num_lots, strategy = self.calculate_optimal_split(
            notional_usd, side, market_data, imbalance_ratio
        )
        
        # 创建micro-lots
        lots = self.create_micro_lots(order_id, side, quantity, price, strategy, num_lots)
        
        # 添加到待执行队列
        self.pending_lots.extend(lots)
        
        # 更新统计
        self.stats['total_orders_processed'] += 1
        self.stats['total_lots_created'] += len(lots)
        self.stats['split_ratio_avg'] = self.stats['total_lots_created'] / self.stats['total_orders_processed']
        self.stats['avg_lot_size_usd'] = notional_usd / len(lots)
        
        logger.info(f"[SmartSizer] 订单拆分完成: {order_id} → {len(lots)}lots, "
                   f"平均{self.stats['avg_lot_size_usd']:.2f}USD/lot")
        
        return lots
    
    def get_next_lot_for_execution(self, side_filter: str = None) -> Optional[MicroLot]:
        """
        获取下一个待执行的micro-lot
        
        Args:
            side_filter: 方向过滤 (BUY/SELL)
            
        Returns:
            MicroLot or None
        """
        # 清理过期的lots
        self._cleanup_expired_lots()
        
        if not self.pending_lots:
            return None
        
        # 按优先级和创建时间排序
        available_lots = [lot for lot in self.pending_lots 
                         if not lot.is_expired and 
                         (side_filter is None or lot.side == side_filter)]
        
        if not available_lots:
            return None
        
        # 选择优先级最高的lot
        available_lots.sort(key=lambda x: (x.priority, x.created_time))
        next_lot = available_lots[0]
        
        # 从待执行队列移除，加入执行中队列
        self.pending_lots.remove(next_lot)
        self.executing_lots[next_lot.lot_id] = next_lot
        
        logger.debug(f"[SmartSizer] 分派执行: {next_lot.lot_id}, "
                    f"数量={next_lot.quantity:.2f}, 优先级={next_lot.priority}")
        
        return next_lot
    
    def mark_lot_completed(self, lot_id: str, success: bool = True):
        """
        标记micro-lot执行完成
        
        Args:
            lot_id: lot ID
            success: 是否执行成功
        """
        if lot_id in self.executing_lots:
            lot = self.executing_lots.pop(lot_id)
            lot.metadata['execution_success'] = success
            lot.metadata['completed_time'] = time.time()
            self.completed_lots.append(lot)
            
            # 更新成功率统计
            total_completed = len(self.completed_lots)
            successful = sum(1 for lot in self.completed_lots 
                           if lot.metadata.get('execution_success', False))
            self.stats['execution_success_rate'] = successful / total_completed if total_completed > 0 else 0
            
            logger.debug(f"[SmartSizer] Lot完成: {lot_id}, 成功={success}")
    
    def _cleanup_expired_lots(self):
        """清理过期的lots"""
        now = time.time()
        
        # 清理待执行队列中的过期lots
        expired_pending = [lot for lot in self.pending_lots if lot.is_expired]
        for lot in expired_pending:
            self.pending_lots.remove(lot)
            logger.warning(f"[SmartSizer] 过期Lot移除: {lot.lot_id}")
        
        # 清理执行中的过期lots
        expired_executing = [lot_id for lot_id, lot in self.executing_lots.items() 
                           if lot.is_expired]
        for lot_id in expired_executing:
            lot = self.executing_lots.pop(lot_id)
            lot.metadata['execution_success'] = False
            lot.metadata['timeout_reason'] = 'expired'
            self.completed_lots.append(lot)
            logger.warning(f"[SmartSizer] 执行超时: {lot_id}")
    
    def get_execution_summary(self) -> Dict[str, Any]:
        """获取执行摘要"""
        self._cleanup_expired_lots()
        
        return {
            'pending_lots': len(self.pending_lots),
            'executing_lots': len(self.executing_lots),
            'completed_lots': len(self.completed_lots),
            'stats': self.stats.copy(),
            'avg_execution_time': self._calculate_avg_execution_time(),
            'queue_health': 'healthy' if len(self.executing_lots) < 20 else 'congested'
        }
    
    def _calculate_avg_execution_time(self) -> float:
        """计算平均执行时间"""
        completed_with_time = [
            lot for lot in self.completed_lots 
            if 'completed_time' in lot.metadata
        ]
        
        if not completed_with_time:
            return 0.0
        
        total_time = sum(
            lot.metadata['completed_time'] - lot.created_time 
            for lot in completed_with_time
        )
        
        return total_time / len(completed_with_time)
    
    def force_clear_queue(self):
        """强制清空执行队列（用于紧急情况）"""
        cleared_pending = len(self.pending_lots)
        cleared_executing = len(self.executing_lots)
        
        self.pending_lots.clear()
        self.executing_lots.clear()
        
        logger.warning(f"[SmartSizer] 队列强制清空: pending={cleared_pending}, executing={cleared_executing}")


# 全局SmartSizer实例
_smart_sizer_instance = None


def get_smart_sizer(**kwargs) -> SmartSizer:
    """获取全局SmartSizer实例"""
    global _smart_sizer_instance
    
    if _smart_sizer_instance is None:
        _smart_sizer_instance = SmartSizer(**kwargs)
    
    return _smart_sizer_instance


def reset_smart_sizer():
    """重置全局实例（用于测试）"""
    global _smart_sizer_instance
    _smart_sizer_instance = None