#!/usr/bin/env python3
"""
Phase 7.1: 动态价差优化器 - Jane Street级别微观结构做市
基于good version文档中的Phase 7设计

目标: 0.2bp → 3-8bp动态价差，提升成交概率50%+
设计: 4bp基础价差 + 波动性/订单流调整
"""

import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

@dataclass
class SpreadConfig:
    """动态价差配置"""
    base_spread_bp: float = 4.0  # 基础4bp
    spread_range: Tuple[float, float] = (3.0, 8.0)  # 3-8bp动态范围
    min_spread_bp: float = 3.0  # 硬最小价差（DSG强化）
    volatility_sensitivity: float = 2.0  # 波动性敏感度
    order_flow_sensitivity: float = 1.5  # 订单流敏感度
    min_samples: int = 10  # 最少样本数
    # DSG世界级参数
    maker_fee_bp: float = -4.0  # Maker返佣
    taker_fee_bp: float = 10.0  # Taker费用
    adverse_selection_factor: float = 0.8  # 逆向选择保守系数
    safety_ticks: int = 2  # 安全tick数量

class DynamicSpreadOptimizer:
    """
    Phase 7.1: 动态价差优化器
    实现Jane Street式微观结构做市的价差策略
    """
    
    def __init__(self, config: Optional[SpreadConfig] = None):
        self.config = config or SpreadConfig()
        
        # 市场数据窗口
        self.price_samples = deque(maxlen=100)
        self.order_flow_samples = deque(maxlen=50)
        self.last_update = 0
        
        # 波动性计算
        self.volatility_window = deque(maxlen=20)
        self.current_volatility = 0.0
        
        # 订单流不平衡
        self.buy_pressure = 0.0
        self.sell_pressure = 0.0
        
        logger.info(f"[Phase7.1] DynamicSpreadOptimizer initialized: base={self.config.base_spread_bp}bp, range={self.config.spread_range}bp")

    def update_market_data(self, mid_price: float, best_bid: float, best_ask: float, 
                          bid_volume: float = 0, ask_volume: float = 0):
        """更新市场数据"""
        now = time.time()
        
        if mid_price <= 0:
            return
            
        # 更新价格样本
        self.price_samples.append((now, mid_price))
        
        # 计算即时波动性
        if len(self.price_samples) >= 2:
            recent_prices = [p[1] for p in list(self.price_samples)[-10:]]
            if len(recent_prices) > 1:
                price_changes = [abs(recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1] 
                               for i in range(1, len(recent_prices))]
                if price_changes:
                    self.current_volatility = sum(price_changes) / len(price_changes)
        
        # 更新订单流不平衡
        if bid_volume > 0 and ask_volume > 0:
            total_volume = bid_volume + ask_volume
            self.buy_pressure = bid_volume / total_volume
            self.sell_pressure = ask_volume / total_volume
            
            imbalance = (bid_volume - ask_volume) / total_volume
            self.order_flow_samples.append(imbalance)
        
        self.last_update = now

    def calculate_required_spread_bp(self, volatility_bp: float = 0) -> float:
        """
        DSG世界级: 计算EV门槛价差
        
        Args:
            volatility_bp: 当前波动率(bp)
            
        Returns:
            必需的最小价差(bp)以保证正期望收益
        """
        # EV门槛公式: required_spread_bp ≥ max(3, 2·fee_maker_bp + φ·vol_bp)
        fee_component = 2 * abs(self.config.maker_fee_bp)  # 2倍费用保证
        vol_component = self.config.adverse_selection_factor * volatility_bp
        
        required_spread = max(
            self.config.min_spread_bp,  # 硬最小3bp
            fee_component + vol_component  # EV约束
        )
        
        return required_spread

    def calculate_optimal_spread(self, side: str = 'both') -> float:
        """
        DSG世界级: 计算最优价差，带硬约束和EV门槛
        
        Args:
            side: 'buy', 'sell', 'both'
            
        Returns:
            优化后的价差(bp)，保证≥3bp且满足EV要求
        """
        base_spread = self.config.base_spread_bp
        
        # 波动性调整
        volatility_adjustment = 0.0
        vol_bp = self.current_volatility * 10000  # 转换为bp
        
        if self.current_volatility < 0.001:  # 低波动
            volatility_adjustment = -1.0  # 收窄1bp
        elif self.current_volatility > 0.005:  # 高波动
            volatility_adjustment = +4.0  # 扩大4bp
        else:
            # 线性插值
            vol_normalized = (self.current_volatility - 0.001) / (0.005 - 0.001)
            volatility_adjustment = vol_normalized * 5.0 - 1.0
        
        # 订单流调整
        order_flow_adjustment = 0.0
        if len(self.order_flow_samples) >= self.config.min_samples:
            recent_flow = list(self.order_flow_samples)[-10:]
            avg_imbalance = sum(recent_flow) / len(recent_flow)
            
            if side == 'buy' and avg_imbalance > 0.2:  # 买单压力大
                order_flow_adjustment = +0.5
            elif side == 'sell' and avg_imbalance < -0.2:  # 卖单压力大
                order_flow_adjustment = +0.5
            elif abs(avg_imbalance) < 0.1:  # 平衡状态
                order_flow_adjustment = -0.5
        
        # 计算基础目标价差
        target_spread = base_spread + volatility_adjustment + order_flow_adjustment
        
        # DSG硬约束: EV门槛检查
        required_spread = self.calculate_required_spread_bp(vol_bp)
        target_spread = max(target_spread, required_spread)
        
        # 限制在范围内，但不能低于硬最小值
        min_spread, max_spread = self.config.spread_range
        min_spread = max(min_spread, self.config.min_spread_bp)  # 硬约束优先
        target_spread = max(min_spread, min(max_spread, target_spread))
        
        return target_spread

    def get_pricing_adjustment(self, side: str, base_price: float) -> float:
        """
        获取价格调整建议
        
        Args:
            side: 'BUY' or 'SELL'
            base_price: 基础价格
            
        Returns:
            调整后的价格
        """
        if base_price <= 0:
            return base_price
            
        spread_bp = self.calculate_optimal_spread(side.lower())
        spread_ratio = spread_bp / 10000  # bp转比例
        
        if side == 'BUY':
            # 买单价格 = 中价 * (1 - spread/2)
            adjusted_price = base_price * (1 - spread_ratio / 2)
        else:
            # 卖单价格 = 中价 * (1 + spread/2)  
            adjusted_price = base_price * (1 + spread_ratio / 2)
            
        return adjusted_price

    def get_spread_stats(self) -> dict:
        """获取价差统计信息"""
        return {
            'current_spread_bp': self.calculate_optimal_spread(),
            'base_spread_bp': self.config.base_spread_bp,
            'volatility': self.current_volatility,
            'buy_pressure': self.buy_pressure,
            'sell_pressure': self.sell_pressure,
            'samples_count': len(self.price_samples),
            'flow_samples': len(self.order_flow_samples),
            'last_update': self.last_update
        }

    def should_update_quotes(self, threshold_bp: float = 0.5) -> bool:
        """判断是否需要更新报价"""
        if not hasattr(self, '_last_spread'):
            self._last_spread = self.config.base_spread_bp
            return True
            
        current_spread = self.calculate_optimal_spread()
        spread_change = abs(current_spread - self._last_spread)
        
        if spread_change >= threshold_bp:
            self._last_spread = current_spread
            return True
            
        return False

    def safety_ticks(self, side: str) -> int:
        """
        DSG世界级: 计算安全tick数量
        
        Args:
            side: 'BUY' or 'SELL'
            
        Returns:
            安全tick数量（1-3个）
        """
        base_ticks = self.config.safety_ticks
        
        # 根据波动性和订单流压力调整安全边距
        if self.current_volatility > 0.005:  # 高波动
            return min(base_ticks + 1, 3)
        elif len(self.order_flow_samples) > 0:
            recent_flow = list(self.order_flow_samples)[-5:]
            avg_imbalance = sum(recent_flow) / len(recent_flow) if recent_flow else 0
            
            # 订单流不平衡时增加安全边距
            if (side == 'SELL' and avg_imbalance < -0.3) or (side == 'BUY' and avg_imbalance > 0.3):
                return min(base_ticks + 1, 3)
        
        return base_ticks

    def safe_post_only_price(self, side: str, mid_price: float, best_bid: float, 
                           best_ask: float, spread_bp: float, tick_size: float) -> float:
        """
        DSG世界级: Maker-Guard护栏 - 确保价格永不被拒(-2010)
        
        Args:
            side: 'BUY' or 'SELL'
            mid_price: 中间价
            best_bid: 最优买价
            best_ask: 最优卖价  
            spread_bp: 目标价差(bp)
            tick_size: tick大小
            
        Returns:
            安全的post-only价格，保证不会立即成交
        """
        # 确保价差≥硬最小值
        spread_bp = max(spread_bp, self.config.min_spread_bp)
        spread_ratio = spread_bp / 10000.0
        
        if side.upper() == 'SELL':
            # 卖单目标价格
            target_price = mid_price * (1 + spread_ratio / 2)
            
            # Maker-Guard: 必须高于best_ask + safety_ticks
            safety_ticks_count = self.safety_ticks('SELL')
            min_safe_price = best_ask + tick_size * safety_ticks_count
            
            # 取较高者确保post-only
            final_price = max(target_price, min_safe_price)
            
            # tick对齐（向上）
            return self.quantize_up(final_price, tick_size)
            
        else:  # BUY
            # 买单目标价格
            target_price = mid_price * (1 - spread_ratio / 2)
            
            # Maker-Guard: 必须低于best_bid - safety_ticks
            safety_ticks_count = self.safety_ticks('BUY')
            max_safe_price = best_bid - tick_size * safety_ticks_count
            
            # 取较低者确保post-only
            final_price = min(target_price, max_safe_price)
            
            # tick对齐（向下）
            return self.quantize_down(final_price, tick_size)

    def quantize_up(self, price: float, tick_size: float) -> float:
        """向上对齐到tick"""
        import math
        return math.ceil(price / tick_size) * tick_size

    def quantize_down(self, price: float, tick_size: float) -> float:
        """向下对齐到tick"""
        import math
        return math.floor(price / tick_size) * tick_size

    def validate_post_only_safety(self, side: str, price: float, best_bid: float, 
                                 best_ask: float, tick_size: float) -> bool:
        """
        DSG世界级: 二次校验价格安全性
        
        Args:
            side: 'BUY' or 'SELL'
            price: 拟下单价格
            best_bid: 当前最优买价
            best_ask: 当前最优卖价
            tick_size: tick大小
            
        Returns:
            True if safe for post-only, False otherwise
        """
        safety_ticks_count = self.safety_ticks(side)
        
        if side.upper() == 'SELL':
            min_safe = best_ask + tick_size * safety_ticks_count
            return price >= min_safe
        else:  # BUY
            max_safe = best_bid - tick_size * safety_ticks_count
            return price <= max_safe

# Phase 7.1 集成接口
def create_dynamic_spread_optimizer() -> DynamicSpreadOptimizer:
    """创建动态价差优化器实例"""
    return DynamicSpreadOptimizer()

if __name__ == "__main__":
    # 测试代码
    optimizer = create_dynamic_spread_optimizer()
    
    # 模拟市场数据更新
    optimizer.update_market_data(0.26400, 0.26398, 0.26402, 1000, 800)
    
    print("Phase 7.1 动态价差优化器测试:")
    print(f"BUY侧价差: {optimizer.calculate_optimal_spread('buy'):.1f}bp")
    print(f"SELL侧价差: {optimizer.calculate_optimal_spread('sell'):.1f}bp")
    print(f"统计信息: {optimizer.get_spread_stats()}")