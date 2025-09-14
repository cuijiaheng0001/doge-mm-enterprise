#!/usr/bin/env python3
"""
Phase 3 - Track C1: 微批量智能下单（Micro-lot Strategy）
解决问题：订单太大无法立即成交，一次成交导致另一边资金激增
"""

import time
import logging
import math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import deque

logger = logging.getLogger(__name__)

@dataclass
class MicroLotConfig:
    """微批量配置"""
    # USD上限（硬性限制）
    l0_usd_cap: float = 15.0   # L0: $8-15
    l1_usd_cap: float = 25.0   # L1: $15-25
    l2_usd_cap: float = 40.0   # L2: $20-40
    
    # USD下限（最小订单）
    l0_usd_min: float = 8.0
    l1_usd_min: float = 15.0
    l2_usd_min: float = 20.0
    
    # 权益比例上限
    l0_equity_pct: float = 0.002  # 0.2%
    l1_equity_pct: float = 0.003  # 0.3%
    l2_equity_pct: float = 0.005  # 0.5%
    
    # 层级订单数量
    l0_order_count: int = 3  # L0分成3个小单
    l1_order_count: int = 2  # L1分成2个中单
    l2_order_count: int = 1  # L2用1个大单
    
    # 成交率目标
    target_fill_rate: float = 0.4  # 40%成交率
    
    # 自适应调整参数
    size_adjust_factor: float = 0.1  # 每次调整10%
    min_adjust_interval: int = 60  # 最小调整间隔（秒）

@dataclass
class MarketMicrostructure:
    """市场微结构数据"""
    avg_trade_size: float
    queue_depth: float
    arrival_rate: float  # 订单到达率
    spread_bps: float
    volatility: float
    liquidity_score: float  # 0-1

class MicroLotEngine:
    """
    微批量下单引擎
    - 动态sizing基于市场微结构
    - 多层分布提高成交率
    - 硬性上限防止超大订单
    """
    
    def __init__(self, config: Optional[MicroLotConfig] = None):
        self.config = config or MicroLotConfig()
        
        # 市场微结构跟踪
        self.trade_sizes = deque(maxlen=100)
        self.fill_times = deque(maxlen=100)
        self.queue_depths = deque(maxlen=50)
        
        # 成交率跟踪
        self.orders_placed = {'L0': 0, 'L1': 0, 'L2': 0}
        self.orders_filled = {'L0': 0, 'L1': 0, 'L2': 0}
        self.last_adjustment = 0
        
        # 当前size调整因子
        self.size_multipliers = {'L0': 1.0, 'L1': 1.0, 'L2': 1.0}
        
        # 统计
        self.stats = {
            'total_orders': 0,
            'total_filled': 0,
            'avg_fill_time': 0,
            'avg_order_size': 0,
            'size_adjustments': 0
        }
        
        logger.info(f"[Phase3-C1] MicroLotEngine initialized with caps: L0=${self.config.l0_usd_cap}, L1=${self.config.l1_usd_cap}, L2=${self.config.l2_usd_cap}")
        
    def update_trade(self, size: float, timestamp: float):
        """更新成交数据"""
        self.trade_sizes.append(size)
        
    def update_fill(self, layer: str, fill_time: float):
        """更新成交信息"""
        self.orders_filled[layer] = self.orders_filled.get(layer, 0) + 1
        self.fill_times.append(fill_time)
        
    def update_queue_depth(self, depth: float):
        """更新队列深度"""
        self.queue_depths.append(depth)
        
    def get_market_microstructure(self) -> MarketMicrostructure:
        """获取市场微结构"""
        # 计算平均成交大小
        if self.trade_sizes:
            avg_trade_size = sum(self.trade_sizes) / len(self.trade_sizes)
        else:
            avg_trade_size = 100  # 默认值
            
        # 计算队列深度
        if self.queue_depths:
            queue_depth = sum(self.queue_depths) / len(self.queue_depths)
        else:
            queue_depth = 1000  # 默认值
            
        # 计算订单到达率（简化）
        if len(self.fill_times) > 1:
            time_diffs = [self.fill_times[i] - self.fill_times[i-1] for i in range(1, len(self.fill_times))]
            avg_interval = sum(time_diffs) / len(time_diffs) if time_diffs else 10
            arrival_rate = 1 / max(0.1, avg_interval)
        else:
            arrival_rate = 0.1
            
        # 其他指标（简化）
        spread_bps = 4  # 默认4bp
        volatility = 0.01  # 1%
        liquidity_score = min(1.0, queue_depth / 10000)  # 基于队列深度
        
        return MarketMicrostructure(
            avg_trade_size=avg_trade_size,
            queue_depth=queue_depth,
            arrival_rate=arrival_rate,
            spread_bps=spread_bps,
            volatility=volatility,
            liquidity_score=liquidity_score
        )
        
    def calculate_optimal_sizes(self, equity: float, mid_price: float) -> Dict[str, List[float]]:
        """
        计算最优订单大小分布
        返回: {layer: [size1, size2, ...]} in DOGE units
        """
        micro = self.get_market_microstructure()
        sizes = {}
        
        # L0: 小单快速成交
        l0_size_usd = self._calculate_layer_size(
            layer='L0',
            equity=equity,
            micro=micro,
            min_usd=self.config.l0_usd_min,
            max_usd=self.config.l0_usd_cap,
            equity_pct=self.config.l0_equity_pct
        )
        
        # 转换为DOGE数量并分割
        l0_size_doge = l0_size_usd / mid_price if mid_price > 0 else 0
        sizes['L0'] = [l0_size_doge / self.config.l0_order_count] * self.config.l0_order_count
        
        # L1: 中等大小
        l1_size_usd = self._calculate_layer_size(
            layer='L1',
            equity=equity,
            micro=micro,
            min_usd=self.config.l1_usd_min,
            max_usd=self.config.l1_usd_cap,
            equity_pct=self.config.l1_equity_pct
        )
        
        l1_size_doge = l1_size_usd / mid_price if mid_price > 0 else 0
        sizes['L1'] = [l1_size_doge / self.config.l1_order_count] * self.config.l1_order_count
        
        # L2: 深度流动性
        l2_size_usd = self._calculate_layer_size(
            layer='L2',
            equity=equity,
            micro=micro,
            min_usd=self.config.l2_usd_min,
            max_usd=self.config.l2_usd_cap,
            equity_pct=self.config.l2_equity_pct
        )
        
        l2_size_doge = l2_size_usd / mid_price if mid_price > 0 else 0
        sizes['L2'] = [l2_size_doge / self.config.l2_order_count] * self.config.l2_order_count
        
        # 更新统计
        self.stats['total_orders'] += sum(len(v) for v in sizes.values())
        all_sizes = [s for layer_sizes in sizes.values() for s in layer_sizes]
        if all_sizes:
            self.stats['avg_order_size'] = sum(all_sizes) / len(all_sizes)
            
        return sizes
        
    def _calculate_layer_size(self, layer: str, equity: float, micro: MarketMicrostructure, 
                              min_usd: float, max_usd: float, equity_pct: float) -> float:
        """计算单层订单大小（USD）"""
        # 基础大小：略小于平均成交
        base_size = micro.avg_trade_size * 0.8
        
        # 权益限制
        equity_limit = equity * equity_pct
        
        # 流动性调整
        liquidity_factor = 0.5 + micro.liquidity_score * 0.5  # 0.5-1.0
        
        # 成交率自适应调整
        multiplier = self.size_multipliers.get(layer, 1.0)
        
        # 综合计算
        optimal_size = min(
            base_size * liquidity_factor * multiplier,
            equity_limit,
            max_usd  # 硬上限
        )
        
        # 确保不低于最小值
        optimal_size = max(optimal_size, min_usd)
        
        return optimal_size
        
    def adjust_sizes_by_fill_rate(self):
        """根据成交率调整订单大小"""
        # 检查调整间隔
        now = time.time()
        if now - self.last_adjustment < self.config.min_adjust_interval:
            return
            
        for layer in ['L0', 'L1', 'L2']:
            placed = self.orders_placed.get(layer, 0)
            filled = self.orders_filled.get(layer, 0)
            
            if placed > 10:  # 至少10个订单才调整
                fill_rate = filled / placed
                
                # 根据成交率调整
                if fill_rate < self.config.target_fill_rate * 0.8:
                    # 成交率太低，减小订单
                    self.size_multipliers[layer] *= (1 - self.config.size_adjust_factor)
                    self.size_multipliers[layer] = max(0.5, self.size_multipliers[layer])
                    logger.info(f"[Phase3-C1] {layer} fill_rate={fill_rate:.1%}, reducing size to {self.size_multipliers[layer]:.2f}x")
                    
                elif fill_rate > self.config.target_fill_rate * 1.2:
                    # 成交率太高，可以增大订单
                    self.size_multipliers[layer] *= (1 + self.config.size_adjust_factor)
                    self.size_multipliers[layer] = min(1.5, self.size_multipliers[layer])
                    logger.info(f"[Phase3-C1] {layer} fill_rate={fill_rate:.1%}, increasing size to {self.size_multipliers[layer]:.2f}x")
                    
        self.last_adjustment = now
        self.stats['size_adjustments'] += 1
        
    def detect_market_impact(self, recent_fills: List[Dict]) -> bool:
        """检测大单冲击"""
        if len(recent_fills) < 3:
            return False
            
        # 检查最近的成交是否导致价格大幅移动
        price_moves = []
        for i in range(1, len(recent_fills)):
            price_change = abs(recent_fills[i]['price'] - recent_fills[i-1]['price']) / recent_fills[i-1]['price']
            price_moves.append(price_change)
            
        # 如果价格移动超过0.1%，认为有冲击
        max_move = max(price_moves) if price_moves else 0
        return max_move > 0.001
        
    def apply_anti_slippage(self, sizes: Dict[str, List[float]], impact_detected: bool) -> Dict[str, List[float]]:
        """应用防滑点调整"""
        if not impact_detected:
            return sizes
            
        # 检测到冲击，缩减所有订单50%
        adjusted = {}
        for layer, layer_sizes in sizes.items():
            adjusted[layer] = [s * 0.5 for s in layer_sizes]
            
        logger.warning("[Phase3-C1] Market impact detected, reducing all sizes by 50%")
        return adjusted
        
    def get_fill_rate(self, layer: str) -> float:
        """获取层级成交率"""
        placed = self.orders_placed.get(layer, 0)
        filled = self.orders_filled.get(layer, 0)
        
        if placed > 0:
            return filled / placed
        return 0
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total_placed = sum(self.orders_placed.values())
        total_filled = sum(self.orders_filled.values())
        
        return {
            'total_orders': self.stats['total_orders'],
            'total_filled': total_filled,
            'overall_fill_rate': total_filled / max(1, total_placed),
            'l0_fill_rate': self.get_fill_rate('L0'),
            'l1_fill_rate': self.get_fill_rate('L1'),
            'l2_fill_rate': self.get_fill_rate('L2'),
            'avg_order_size': self.stats['avg_order_size'],
            'size_adjustments': self.stats['size_adjustments'],
            'l0_multiplier': self.size_multipliers['L0'],
            'l1_multiplier': self.size_multipliers['L1'],
            'l2_multiplier': self.size_multipliers['L2']
        }
        
    def validate_order_size(self, size_doge: float, price: float, layer: str) -> Tuple[bool, str]:
        """
        验证订单大小是否合规
        返回: (is_valid, reason)
        """
        size_usd = size_doge * price
        
        # 检查硬上限
        if layer == 'L0' and size_usd > self.config.l0_usd_cap:
            return False, f"L0 size ${size_usd:.1f} exceeds cap ${self.config.l0_usd_cap}"
        elif layer == 'L1' and size_usd > self.config.l1_usd_cap:
            return False, f"L1 size ${size_usd:.1f} exceeds cap ${self.config.l1_usd_cap}"
        elif layer == 'L2' and size_usd > self.config.l2_usd_cap:
            return False, f"L2 size ${size_usd:.1f} exceeds cap ${self.config.l2_usd_cap}"
            
        # 检查最小值
        min_usd = 5.0  # 绝对最小值
        if size_usd < min_usd:
            return False, f"Size ${size_usd:.1f} below minimum ${min_usd}"
            
        return True, "OK"

# 单例实例
_micro_lot_instance = None

def get_micro_lot_engine(config=None) -> MicroLotEngine:
    """获取微批量引擎单例"""
    global _micro_lot_instance
    if _micro_lot_instance is None:
        _micro_lot_instance = MicroLotEngine(config)
    return _micro_lot_instance