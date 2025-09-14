"""
LIMIT_MAKER安全距离守护：Phase 5 机构级价格安全机制
防止LIMIT_MAKER订单在市场快速移动时出现安全问题
"""
import time
import logging
from typing import Dict, Tuple, Optional, Any, NamedTuple
from dataclasses import dataclass
from enum import Enum
import threading

logger = logging.getLogger(__name__)


class SafetyLevel(Enum):
    """安全级别枚举"""
    CONSERVATIVE = "conservative"    # 保守模式：大安全距离
    BALANCED = "balanced"           # 平衡模式：标准安全距离
    AGGRESSIVE = "aggressive"       # 激进模式：最小安全距离
    EMERGENCY = "emergency"         # 紧急模式：极大安全距离


class MarketCondition(Enum):
    """市场状态枚举"""
    STABLE = "stable"               # 稳定市场
    VOLATILE = "volatile"           # 波动市场
    TRENDING = "trending"           # 趋势市场
    CRISIS = "crisis"              # 危机状态


@dataclass
class SafetyConfiguration:
    """安全配置"""
    min_bps_distance: float         # 最小基点距离
    max_bps_distance: float         # 最大基点距离
    volatility_multiplier: float    # 波动率乘数
    trend_adjustment: float         # 趋势调整
    emergency_multiplier: float     # 紧急乘数


class PriceLevel(NamedTuple):
    """价格水平"""
    price: float
    timestamp: float
    confidence: float               # 置信度 [0,1]


class LimitMakerGuard:
    """
    Phase 5 机构级LIMIT_MAKER安全距离守护
    
    核心功能：
    1. 动态计算安全价格距离
    2. 基于市场波动率调整安全阈值
    3. 实时监控价格变化速度
    4. 紧急情况下的价格保护
    """
    
    def __init__(self, 
                 base_bps_distance: float = 5.0,
                 max_price_deviation: float = 0.02,
                 volatility_window: float = 60.0,
                 trend_detection_window: float = 30.0):
        """
        初始化LIMIT_MAKER守护
        
        Args:
            base_bps_distance: 基础安全距离(基点)
            max_price_deviation: 最大价格偏离比例
            volatility_window: 波动率计算窗口(秒)
            trend_detection_window: 趋势检测窗口(秒)
        """
        self.base_bps_distance = base_bps_distance
        self.max_price_deviation = max_price_deviation
        self.volatility_window = volatility_window
        self.trend_detection_window = trend_detection_window
        
        # 安全配置预设
        self.safety_configs = {
            SafetyLevel.CONSERVATIVE: SafetyConfiguration(
                min_bps_distance=10.0,
                max_bps_distance=50.0,
                volatility_multiplier=3.0,
                trend_adjustment=2.0,
                emergency_multiplier=5.0
            ),
            SafetyLevel.BALANCED: SafetyConfiguration(
                min_bps_distance=5.0,
                max_bps_distance=25.0,
                volatility_multiplier=2.0,
                trend_adjustment=1.5,
                emergency_multiplier=3.0
            ),
            SafetyLevel.AGGRESSIVE: SafetyConfiguration(
                min_bps_distance=2.0,
                max_bps_distance=15.0,
                volatility_multiplier=1.5,
                trend_adjustment=1.0,
                emergency_multiplier=2.0
            ),
            SafetyLevel.EMERGENCY: SafetyConfiguration(
                min_bps_distance=20.0,
                max_bps_distance=100.0,
                volatility_multiplier=5.0,
                trend_adjustment=3.0,
                emergency_multiplier=10.0
            )
        }
        
        # 市场数据历史
        self.price_history = []
        self.bid_history = []
        self.ask_history = []
        self.spread_history = []
        
        # 当前市场状态
        self.current_safety_level = SafetyLevel.BALANCED
        self.current_market_condition = MarketCondition.STABLE
        self.last_price_update = 0
        
        # 统计信息
        self.stats = {
            'total_price_checks': 0,
            'safety_violations': 0,
            'emergency_activations': 0,
            'avg_safety_distance_bps': 0.0,
            'volatility_score': 0.0
        }
        
        # 线程锁
        self.lock = threading.Lock()
        
        logger.info(f"[LimitMakerGuard] 初始化: base_distance={self.base_bps_distance}bps, "
                   f"max_deviation={self.max_price_deviation:.2%}")
    
    def update_market_data(self, bid: float, ask: float, last_price: float = None):
        """
        更新市场数据
        
        Args:
            bid: 最佳买价
            ask: 最佳卖价
            last_price: 最新成交价（可选）
        """
        with self.lock:
            now = time.time()
            
            # 计算中价
            mid_price = (bid + ask) / 2
            spread = ask - bid
            spread_bps = (spread / mid_price) * 10000 if mid_price > 0 else 0
            
            # 更新历史数据
            cutoff_time = now - max(self.volatility_window, self.trend_detection_window)
            
            self.price_history.append(PriceLevel(mid_price, now, 1.0))
            self.bid_history.append(PriceLevel(bid, now, 1.0))
            self.ask_history.append(PriceLevel(ask, now, 1.0))
            self.spread_history.append((spread_bps, now))
            
            # 清理旧数据
            self.price_history = [p for p in self.price_history if p.timestamp > cutoff_time]
            self.bid_history = [p for p in self.bid_history if p.timestamp > cutoff_time]
            self.ask_history = [p for p in self.ask_history if p.timestamp > cutoff_time]
            self.spread_history = [(s, t) for s, t in self.spread_history if t > cutoff_time]
            
            self.last_price_update = now
            
            # 更新市场状态评估
            self._assess_market_condition()
    
    def _assess_market_condition(self):
        """评估当前市场状态"""
        if len(self.price_history) < 5:
            return
        
        # 计算价格波动率
        recent_prices = [p.price for p in self.price_history[-20:]]
        if len(recent_prices) < 2:
            return
        
        volatility = self._calculate_volatility(recent_prices)
        self.stats['volatility_score'] = volatility
        
        # 检测趋势强度
        trend_strength = self._calculate_trend_strength()
        
        # 检测价格变化速度
        price_velocity = self._calculate_price_velocity()
        
        # 综合评估市场状态
        if volatility > 0.005 or price_velocity > 0.01:  # 高波动或快速变化
            if trend_strength > 0.7:
                self.current_market_condition = MarketCondition.TRENDING
            else:
                self.current_market_condition = MarketCondition.VOLATILE
        elif volatility > 0.01 or price_velocity > 0.02:  # 极高波动
            self.current_market_condition = MarketCondition.CRISIS
        else:
            self.current_market_condition = MarketCondition.STABLE
        
        # 根据市场状态调整安全级别
        if self.current_market_condition == MarketCondition.CRISIS:
            self.current_safety_level = SafetyLevel.EMERGENCY
        elif self.current_market_condition == MarketCondition.VOLATILE:
            self.current_safety_level = SafetyLevel.CONSERVATIVE
        elif self.current_market_condition == MarketCondition.TRENDING:
            self.current_safety_level = SafetyLevel.BALANCED
        else:
            self.current_safety_level = SafetyLevel.BALANCED
    
    def _calculate_volatility(self, prices: list) -> float:
        """计算价格波动率（标准差）"""
        if len(prices) < 2:
            return 0.0
        
        mean_price = sum(prices) / len(prices)
        variance = sum((p - mean_price) ** 2 for p in prices) / len(prices)
        std_dev = variance ** 0.5
        
        return std_dev / mean_price if mean_price > 0 else 0.0
    
    def _calculate_trend_strength(self) -> float:
        """计算趋势强度 [0,1]"""
        if len(self.price_history) < 10:
            return 0.0
        
        recent_prices = [p.price for p in self.price_history[-10:]]
        
        # 简单线性回归斜率
        n = len(recent_prices)
        x_mean = (n - 1) / 2
        y_mean = sum(recent_prices) / n
        
        numerator = sum((i - x_mean) * (p - y_mean) for i, p in enumerate(recent_prices))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        
        if denominator == 0:
            return 0.0
        
        slope = numerator / denominator
        trend_strength = abs(slope) / y_mean if y_mean > 0 else 0.0
        
        return min(1.0, trend_strength * 1000)  # 缩放到合理范围
    
    def _calculate_price_velocity(self) -> float:
        """计算价格变化速度"""
        if len(self.price_history) < 2:
            return 0.0
        
        latest = self.price_history[-1]
        previous = self.price_history[-2]
        
        time_diff = latest.timestamp - previous.timestamp
        if time_diff <= 0:
            return 0.0
        
        price_change = abs(latest.price - previous.price) / previous.price
        velocity = price_change / time_diff
        
        return velocity
    
    def calculate_safe_price(self, side: str, reference_price: float, 
                           order_size_usd: float = 0.0) -> Tuple[float, Dict[str, Any]]:
        """
        计算安全的LIMIT_MAKER价格
        
        Args:
            side: 订单方向 (BUY/SELL)
            reference_price: 参考价格（通常是中价或对手价）
            order_size_usd: 订单金额（用于风险调整）
            
        Returns:
            (安全价格, 计算详情)
        """
        with self.lock:
            self.stats['total_price_checks'] += 1
            
            # 获取当前安全配置
            config = self.safety_configs[self.current_safety_level]
            
            # 基础安全距离
            base_distance_bps = config.min_bps_distance
            
            # 波动率调整
            volatility_adjustment = self.stats['volatility_score'] * config.volatility_multiplier
            
            # 趋势调整
            trend_adjustment = 0.0
            if self.current_market_condition == MarketCondition.TRENDING:
                trend_strength = self._calculate_trend_strength()
                trend_adjustment = trend_strength * config.trend_adjustment
            
            # 订单大小调整
            size_adjustment = 0.0
            if order_size_usd > 100:  # 大订单需要更大安全距离
                size_adjustment = min(5.0, (order_size_usd - 100) / 100)
            
            # 紧急情况调整
            emergency_adjustment = 0.0
            if self.current_market_condition == MarketCondition.CRISIS:
                emergency_adjustment = config.min_bps_distance * (config.emergency_multiplier - 1)
                self.stats['emergency_activations'] += 1
            
            # 计算总安全距离
            total_distance_bps = min(
                config.max_bps_distance,
                base_distance_bps + volatility_adjustment + trend_adjustment + 
                size_adjustment + emergency_adjustment
            )
            
            # 转换为价格距离
            price_distance = reference_price * (total_distance_bps / 10000)
            
            # 计算安全价格
            if side.upper() == 'BUY':
                # BUY订单：价格应该低于参考价格
                safe_price = reference_price - price_distance
            else:
                # SELL订单：价格应该高于参考价格  
                safe_price = reference_price + price_distance
            
            # 更新统计
            self.stats['avg_safety_distance_bps'] = (
                (self.stats['avg_safety_distance_bps'] * (self.stats['total_price_checks'] - 1) + 
                 total_distance_bps) / self.stats['total_price_checks']
            )
            
            # 计算详情
            calculation_details = {
                'reference_price': reference_price,
                'safe_price': safe_price,
                'total_distance_bps': total_distance_bps,
                'price_distance': price_distance,
                'safety_level': self.current_safety_level.value,
                'market_condition': self.current_market_condition.value,
                'adjustments': {
                    'base_distance_bps': base_distance_bps,
                    'volatility_adjustment': volatility_adjustment,
                    'trend_adjustment': trend_adjustment,
                    'size_adjustment': size_adjustment,
                    'emergency_adjustment': emergency_adjustment
                },
                'volatility_score': self.stats['volatility_score'],
                'timestamp': time.time()
            }
            
            logger.debug(f"[LimitMakerGuard] 安全价格计算: {side} {reference_price:.5f} → {safe_price:.5f} "
                        f"(距离{total_distance_bps:.1f}bps, 级别={self.current_safety_level.value})")
            
            return safe_price, calculation_details
    
    def validate_limit_order_safety(self, side: str, price: float, 
                                   current_bid: float, current_ask: float) -> Tuple[bool, str]:
        """
        验证限价订单安全性
        
        Args:
            side: 订单方向
            price: 订单价格
            current_bid: 当前最佳买价
            current_ask: 当前最佳卖价
            
        Returns:
            (是否安全, 原因说明)
        """
        mid_price = (current_bid + current_ask) / 2
        
        if side.upper() == 'BUY':
            # BUY订单不应该高于当前ask（避免立即成交）
            if price >= current_ask:
                self.stats['safety_violations'] += 1
                return False, f"BUY价格{price:.5f}高于ASK{current_ask:.5f}，将立即成交"
            
            # 检查是否距离中价太近
            distance_bps = ((mid_price - price) / mid_price) * 10000
            min_distance = self.safety_configs[self.current_safety_level].min_bps_distance
            
            if distance_bps < min_distance:
                self.stats['safety_violations'] += 1
                return False, f"BUY距离中价太近：{distance_bps:.1f}bps < {min_distance:.1f}bps"
            
        else:  # SELL
            # SELL订单不应该低于当前bid（避免立即成交）
            if price <= current_bid:
                self.stats['safety_violations'] += 1
                return False, f"SELL价格{price:.5f}低于BID{current_bid:.5f}，将立即成交"
            
            # 检查是否距离中价太近
            distance_bps = ((price - mid_price) / mid_price) * 10000
            min_distance = self.safety_configs[self.current_safety_level].min_bps_distance
            
            if distance_bps < min_distance:
                self.stats['safety_violations'] += 1
                return False, f"SELL距离中价太近：{distance_bps:.1f}bps < {min_distance:.1f}bps"
        
        return True, "价格安全"
    
    def get_recommended_safety_level(self) -> SafetyLevel:
        """获取推荐的安全级别"""
        return self.current_safety_level
    
    def override_safety_level(self, level: SafetyLevel, duration: float = 300.0):
        """
        临时覆盖安全级别
        
        Args:
            level: 新的安全级别
            duration: 覆盖持续时间（秒）
        """
        self.current_safety_level = level
        logger.warning(f"[LimitMakerGuard] 安全级别手动设置为: {level.value}, "
                      f"持续{duration:.0f}秒")
        
        # TODO: 可以添加定时器自动恢复
    
    def get_market_assessment(self) -> Dict[str, Any]:
        """获取市场评估报告"""
        with self.lock:
            latest_prices = [p.price for p in self.price_history[-10:]]
            latest_spreads = [s for s, t in self.spread_history[-10:]]
            
            return {
                'market_condition': self.current_market_condition.value,
                'safety_level': self.current_safety_level.value,
                'volatility_score': self.stats['volatility_score'],
                'trend_strength': self._calculate_trend_strength(),
                'price_velocity': self._calculate_price_velocity(),
                'avg_spread_bps': sum(latest_spreads) / len(latest_spreads) if latest_spreads else 0,
                'price_range_5min': {
                    'min': min(latest_prices) if latest_prices else 0,
                    'max': max(latest_prices) if latest_prices else 0,
                    'current': latest_prices[-1] if latest_prices else 0
                },
                'data_freshness': time.time() - self.last_price_update,
                'stats': self.stats.copy()
            }
    
    def get_safety_summary(self) -> str:
        """获取安全状态摘要（单行）"""
        assessment = self.get_market_assessment()
        
        return (f"safety=[{self.current_safety_level.value.upper()}] "
               f"market=[{self.current_market_condition.value.upper()}] "
               f"volatility={self.stats['volatility_score']:.4f} "
               f"avg_distance={self.stats['avg_safety_distance_bps']:.1f}bps "
               f"violations={self.stats['safety_violations']}")
    
    def emergency_shutdown(self):
        """紧急停机模式"""
        self.current_safety_level = SafetyLevel.EMERGENCY
        self.current_market_condition = MarketCondition.CRISIS
        self.stats['emergency_activations'] += 1
        
        logger.critical("[LimitMakerGuard] 紧急停机模式激活！所有安全距离提升至最大值")


# 全局LimitMakerGuard实例
_limit_maker_guard_instance = None
_guard_lock = threading.Lock()


def get_limit_maker_guard(**kwargs) -> LimitMakerGuard:
    """获取全局LimitMakerGuard实例"""
    global _limit_maker_guard_instance
    
    with _guard_lock:
        if _limit_maker_guard_instance is None:
            _limit_maker_guard_instance = LimitMakerGuard(**kwargs)
        
        return _limit_maker_guard_instance


def reset_limit_maker_guard():
    """重置全局实例（用于测试）"""
    global _limit_maker_guard_instance
    with _guard_lock:
        _limit_maker_guard_instance = None