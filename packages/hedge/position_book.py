"""
Position Book - 统一持仓簿
管理现货和永续合约的综合Delta头寸
"""

import logging
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class PositionType(Enum):
    """持仓类型"""
    SPOT = "spot"
    PERP = "perp"


@dataclass
class PositionSnapshot:
    """持仓快照"""
    ts: float
    delta_spot: float
    delta_perp: float
    delta_total: float
    notional_spot: float
    notional_perp: float
    margin_used: float = 0.0
    margin_ratio: float = 0.0
    
    @property
    def delta_net(self) -> float:
        """净Delta（应该接近0）"""
        return self.delta_total
    
    @property
    def hedge_ratio(self) -> float:
        """对冲比率"""
        if abs(self.delta_spot) < 0.01:
            return 0.0
        return -self.delta_perp / self.delta_spot
    
    @property
    def is_balanced(self, tolerance: float = 30) -> bool:
        """是否平衡"""
        return abs(self.delta_net) <= tolerance


class PositionBook:
    """
    统一持仓簿 - FAHE核心组件
    跟踪现货和永续合约的综合Delta
    """
    
    def __init__(self, 
                 bandwidth: float = 150,
                 deadband: float = 40,
                 max_delta_error: float = 30):
        """
        初始化Position Book
        
        Args:
            bandwidth: 目标带宽（DOGE）
            deadband: 死区（DOGE）
            max_delta_error: 最大Delta误差（DOGE）
        """
        # 带宽参数
        self.bandwidth = bandwidth
        self.deadband = deadband
        self.max_delta_error = max_delta_error
        
        # 持仓状态
        self.delta_spot: float = 0.0  # 现货Delta
        self.delta_perp: float = 0.0  # 永续Delta
        self.avg_price_spot: float = 0.0  # 现货均价
        self.avg_price_perp: float = 0.0  # 永续均价
        
        # 成交记录
        self.fill_history_spot: deque = deque(maxlen=100)
        self.fill_history_perp: deque = deque(maxlen=100)
        
        # 快照历史
        self.snapshots: deque = deque(maxlen=1000)
        
        # 统计信息
        self.stats = {
            'total_spot_fills': 0,
            'total_perp_fills': 0,
            'total_spot_volume': 0.0,
            'total_perp_volume': 0.0,
            'last_update_ts': 0,
            'max_delta_seen': 0.0,
            'hedge_triggers': 0
        }
        
        logger.info(f"[PositionBook] 初始化完成: bw={bandwidth}, db={deadband}, max_error={max_delta_error}")
    
    def on_spot_fill(self, side: str, qty: float, px: float, ts: float = None) -> None:
        """
        处理现货成交
        
        Args:
            side: 买卖方向
            qty: 成交数量
            px: 成交价格
            ts: 时间戳
        """
        if ts is None:
            ts = time.time()
        
        # 更新Delta
        delta_change = qty if side == 'BUY' else -qty
        self.delta_spot += delta_change
        
        # 更新均价
        if abs(self.delta_spot) > 0.01:
            if side == 'BUY':
                total_cost = self.avg_price_spot * (self.delta_spot - delta_change) + px * qty
                self.avg_price_spot = total_cost / self.delta_spot
            else:
                # SELL时的均价计算较复杂，这里简化处理
                self.avg_price_spot = px
        
        # 记录成交
        fill_record = {
            'ts': ts,
            'side': side,
            'qty': qty,
            'px': px,
            'delta_change': delta_change,
            'delta_after': self.delta_spot
        }
        self.fill_history_spot.append(fill_record)
        
        # 更新统计
        self.stats['total_spot_fills'] += 1
        self.stats['total_spot_volume'] += qty * px
        self.stats['last_update_ts'] = ts
        
        # 记录快照
        self._take_snapshot(ts)
        
        logger.info(f"[PositionBook] 现货成交: {side} {qty:.2f}@{px:.5f}, delta_spot={self.delta_spot:.2f}")
    
    def on_perp_fill(self, side: str, qty: float, px: float, ts: float = None) -> None:
        """
        处理永续合约成交
        
        Args:
            side: 买卖方向
            qty: 成交数量（张数）
            px: 成交价格
            ts: 时间戳
        """
        if ts is None:
            ts = time.time()
        
        # 永续合约Delta（与现货相反）
        delta_change = -qty if side == 'BUY' else qty
        self.delta_perp += delta_change
        
        # 更新均价
        if abs(self.delta_perp) > 0.01:
            if side == 'BUY':
                # 做多永续，Delta为负
                total_cost = abs(self.avg_price_perp * (self.delta_perp - delta_change)) + px * qty
                self.avg_price_perp = -total_cost / self.delta_perp if self.delta_perp < 0 else px
            else:
                self.avg_price_perp = px
        
        # 记录成交
        fill_record = {
            'ts': ts,
            'side': side,
            'qty': qty,
            'px': px,
            'delta_change': delta_change,
            'delta_after': self.delta_perp
        }
        self.fill_history_perp.append(fill_record)
        
        # 更新统计
        self.stats['total_perp_fills'] += 1
        self.stats['total_perp_volume'] += qty * px
        self.stats['last_update_ts'] = ts
        
        # 记录快照
        self._take_snapshot(ts)
        
        logger.info(f"[PositionBook] 永续成交: {side} {qty:.2f}@{px:.5f}, delta_perp={self.delta_perp:.2f}")
    
    @property
    def delta_total(self) -> float:
        """总Delta"""
        return self.delta_spot + self.delta_perp
    
    @property
    def delta_target(self) -> float:
        """目标Delta（限制在带宽内）"""
        return max(-self.bandwidth, min(self.bandwidth, self.delta_total))
    
    @property
    def delta_to_hedge(self) -> float:
        """需要对冲的Delta"""
        delta_excess = self.delta_total - self.delta_target
        
        # 应用死区
        if abs(delta_excess) < self.deadband:
            return 0.0
        
        return delta_excess
    
    def get_hedge_requirement(self) -> Tuple[str, float]:
        """
        获取对冲需求
        
        Returns:
            (方向, 数量) - 方向为'BUY'或'SELL'，数量为DOGE
        """
        to_hedge = self.delta_to_hedge
        
        if abs(to_hedge) < 0.01:
            return ('NONE', 0.0)
        
        # 如果Delta过多（净多头），需要SELL永续
        # 如果Delta过少（净空头），需要BUY永续
        side = 'SELL' if to_hedge > 0 else 'BUY'
        qty = abs(to_hedge)
        
        # 检查对冲后是否会超出误差范围
        delta_after_hedge = self.delta_perp + (-qty if side == 'BUY' else qty)
        delta_error = abs(delta_after_hedge - (-self.delta_spot))
        
        if delta_error > self.max_delta_error:
            logger.warning(f"[PositionBook] 对冲后误差过大: {delta_error:.2f} > {self.max_delta_error}")
            # 调整对冲量
            qty = min(qty, self.max_delta_error)
        
        self.stats['hedge_triggers'] += 1
        
        return (side, qty)
    
    def is_hedge_needed(self) -> bool:
        """
        是否需要对冲
        
        Returns:
            是否需要对冲
        """
        return abs(self.delta_to_hedge) >= self.deadband
    
    def validate_position(self) -> Tuple[bool, str]:
        """
        验证持仓状态
        
        Returns:
            (是否有效, 错误信息)
        """
        # 检查Delta误差
        delta_error = abs(self.delta_perp - (-self.delta_spot))
        if delta_error > self.max_delta_error:
            return (False, f"Delta误差过大: {delta_error:.2f}")
        
        # 检查总Delta是否在合理范围
        if abs(self.delta_total) > self.bandwidth * 2:
            return (False, f"总Delta超出范围: {self.delta_total:.2f}")
        
        # 更新最大Delta
        self.stats['max_delta_seen'] = max(self.stats['max_delta_seen'], abs(self.delta_total))
        
        return (True, "OK")
    
    def _take_snapshot(self, ts: float = None) -> None:
        """
        记录持仓快照
        
        Args:
            ts: 时间戳
        """
        if ts is None:
            ts = time.time()
        
        snapshot = PositionSnapshot(
            ts=ts,
            delta_spot=self.delta_spot,
            delta_perp=self.delta_perp,
            delta_total=self.delta_total,
            notional_spot=self.delta_spot * self.avg_price_spot if self.avg_price_spot > 0 else 0,
            notional_perp=abs(self.delta_perp * self.avg_price_perp) if self.avg_price_perp > 0 else 0
        )
        
        self.snapshots.append(snapshot)
    
    def get_latest_snapshot(self) -> Optional[PositionSnapshot]:
        """
        获取最新快照
        
        Returns:
            最新的持仓快照
        """
        if self.snapshots:
            return self.snapshots[-1]
        
        # 创建当前快照
        self._take_snapshot()
        return self.snapshots[-1] if self.snapshots else None
    
    def get_delta_percentiles(self, window: int = 100) -> Dict[str, float]:
        """
        获取Delta分位数
        
        Args:
            window: 统计窗口大小
        
        Returns:
            Delta统计
        """
        recent_snapshots = list(self.snapshots)[-window:]
        if not recent_snapshots:
            return {'p50': 0, 'p90': 0, 'p95': 0, 'p99': 0}
        
        delta_values = [abs(s.delta_total) for s in recent_snapshots]
        delta_values.sort()
        n = len(delta_values)
        
        return {
            'p50': delta_values[int(n * 0.5)],
            'p90': delta_values[int(n * 0.9)],
            'p95': delta_values[int(n * 0.95)],
            'p99': delta_values[int(n * 0.99)] if n > 0 else delta_values[-1]
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        delta_percentiles = self.get_delta_percentiles()
        
        return {
            **self.stats,
            'delta_spot': self.delta_spot,
            'delta_perp': self.delta_perp,
            'delta_total': self.delta_total,
            'delta_to_hedge': self.delta_to_hedge,
            'avg_price_spot': self.avg_price_spot,
            'avg_price_perp': self.avg_price_perp,
            'is_balanced': abs(self.delta_total) <= self.max_delta_error,
            'hedge_ratio': -self.delta_perp / self.delta_spot if abs(self.delta_spot) > 0.01 else 0,
            **delta_percentiles
        }
    
    def reset(self) -> None:
        """
        重置持仓簿
        """
        self.delta_spot = 0.0
        self.delta_perp = 0.0
        self.avg_price_spot = 0.0
        self.avg_price_perp = 0.0
        self.fill_history_spot.clear()
        self.fill_history_perp.clear()
        self.snapshots.clear()
        
        logger.info("[PositionBook] 持仓簿已重置")