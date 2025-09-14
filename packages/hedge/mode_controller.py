"""
Mode Controller - 模式控制器
根据市场状态动态决定Passive/Active权重
"""

import logging
import math
import time
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """市场状态"""
    CALM = "calm"  # 平静
    NORMAL = "normal"  # 正常
    VOLATILE = "volatile"  # 波动
    STRESSED = "stressed"  # 压力


@dataclass
class MarketSignals:
    """市场信号"""
    lambda_delta: float  # 净Delta到达频率（事件/秒）
    sigma_30s: float  # 30秒波动率
    queue_toxicity: float  # 队列毒性 [0,1]
    funding_pred: float  # 预测资金费（正=付费，负=收费）
    maker_rebate: float  # Maker返佣（负值表示返佣）
    spread_bps: float  # 当前价差（基点）
    queue_depth: float  # 队列深度
    market_impact: float  # 市场冲击估计
    ts: float = 0.0  # 时间戳


class ModeController:
    """
    模式控制器 - FAHE核心决策组件
    动态决定Passive/Active对冲比例
    """
    
    def __init__(self,
                 a0: float = 0.6,  # 基础passive权重
                 a1: float = 0.5,  # 毒性系数
                 a2: float = 0.4,  # 成本系数
                 a3: float = 0.3,  # 波动系数
                 a4: float = 0.2,  # 资金费系数
                 hysteresis: float = 0.15):  # 滞回带宽
        """
        初始化模式控制器
        
        Args:
            a0-a4: 权重计算系数
            hysteresis: 滞回带宽，防止频繁切换
        """
        # 权重系数
        self.a0 = a0
        self.a1 = a1
        self.a2 = a2
        self.a3 = a3
        self.a4 = a4
        
        # 滞回控制
        self.hysteresis = hysteresis
        self.last_weight = a0  # 上次权重
        self.weight_history = deque(maxlen=100)
        
        # 阈值参数
        self.sigma_threshold = 0.002  # 波动率阈值（0.2%）
        self.lambda_threshold = 5.0  # Delta频率阈值（5次/秒）
        self.toxicity_threshold = 0.6  # 毒性阈值
        self.funding_window = 300  # 资金费窗口（5分钟）
        
        # 成本模型参数
        self.k_delay = 0.6  # 延迟成本系数
        self.taker_fee_bps = 0.04  # Taker费率（4bp）
        self.slip_bps_base = 0.02  # 基础滑点（2bp）
        
        # 统计信息
        self.stats = {
            'mode_changes': 0,
            'avg_passive_weight': a0,
            'current_regime': MarketRegime.NORMAL,
            'last_update_ts': 0
        }
        
        # EWMA参数
        self.ewma_alpha = 0.3  # EWMA平滑系数
        self.signal_history = deque(maxlen=50)
        
        logger.info(f"[ModeController] 初始化完成: a0={a0}, hysteresis={hysteresis}")
    
    def mode_weights(self, signals: MarketSignals, delta_to_hedge: float) -> float:
        """
        计算Passive权重
        
        Args:
            signals: 市场信号
            delta_to_hedge: 需要对冲的Delta量
        
        Returns:
            w_passive: Passive权重 [0,1]
        """
        # 更新信号历史
        self.signal_history.append(signals)
        
        # 计算延迟成本
        cost_delay = self._calculate_delay_cost(signals.sigma_30s, abs(delta_to_hedge))
        
        # 计算Taker成本
        cost_taker = self._calculate_taker_cost(abs(delta_to_hedge), signals.market_impact)
        
        # 计算各项影响因子
        toxicity_penalty = self.a1 * min(1.0, signals.queue_toxicity / self.toxicity_threshold)
        
        # 成本差异项（Taker成本 vs Maker返佣）
        cost_diff = max(0, cost_taker - max(-signals.maker_rebate, 0))
        cost_penalty = self.a2 * cost_diff
        
        # 波动/频率惩罚
        is_volatile = (signals.sigma_30s > self.sigma_threshold or 
                      signals.lambda_delta > self.lambda_threshold)
        volatility_penalty = self.a3 if is_volatile else 0
        
        # 资金费奖励（如果对我们有利）
        funding_bonus = self.a4 * max(0, -signals.funding_pred)
        
        # 计算原始权重
        raw_weight = (self.a0 
                     - toxicity_penalty 
                     - cost_penalty 
                     - volatility_penalty 
                     + funding_bonus)
        
        # 限制在[0,1]范围
        raw_weight = max(0.0, min(1.0, raw_weight))
        
        # 应用滞回控制
        w_passive = self._apply_hysteresis(raw_weight)
        
        # 特殊情况处理
        w_passive = self._apply_special_rules(w_passive, signals)
        
        # 更新统计
        self._update_stats(w_passive, signals)
        
        logger.debug(f"[ModeController] w_passive={w_passive:.3f} "
                    f"(tox={signals.queue_toxicity:.2f}, σ={signals.sigma_30s:.4f}, "
                    f"λ={signals.lambda_delta:.1f})")
        
        return w_passive
    
    def _calculate_delay_cost(self, sigma: float, delta_size: float, delay_ms: float = 100) -> float:
        """
        计算延迟成本
        
        Args:
            sigma: 波动率
            delta_size: Delta大小
            delay_ms: 延迟（毫秒）
        
        Returns:
            延迟成本（基点）
        """
        delay_s = delay_ms / 1000.0
        cost = self.k_delay * sigma * delta_size * math.sqrt(delay_s)
        return cost * 10000  # 转换为基点
    
    def _calculate_taker_cost(self, size: float, impact: float) -> float:
        """
        计算Taker成本
        
        Args:
            size: 订单大小
            impact: 市场冲击
        
        Returns:
            Taker成本（基点）
        """
        # 基础费率 + 滑点
        slip_bps = self.slip_bps_base * (1 + impact)
        return self.taker_fee_bps + slip_bps
    
    def _apply_hysteresis(self, raw_weight: float) -> float:
        """
        应用滞回控制
        
        Args:
            raw_weight: 原始权重
        
        Returns:
            带滞回的权重
        """
        # 如果变化不超过滞回带宽，保持原值
        if abs(raw_weight - self.last_weight) < self.hysteresis:
            return self.last_weight
        
        # 更新权重
        self.last_weight = raw_weight
        self.stats['mode_changes'] += 1
        
        return raw_weight
    
    def _apply_special_rules(self, w_passive: float, signals: MarketSignals) -> float:
        """
        应用特殊规则
        
        Args:
            w_passive: 基础权重
            signals: 市场信号
        
        Returns:
            调整后的权重
        """
        # 资金费结算窗口特殊处理
        time_to_funding = self._time_to_next_funding()
        if time_to_funding < self.funding_window and signals.funding_pred > 0:
            # 要付资金费，降低passive权重
            funding_factor = max(0.2, 1.0 - time_to_funding / self.funding_window)
            w_passive *= funding_factor
            logger.debug(f"[ModeController] 资金费窗口调整: {w_passive:.3f}")
        
        # 极端市场条件
        if signals.queue_toxicity > 0.9 or signals.sigma_30s > 0.01:
            # 极端毒性或波动，强制使用Active
            w_passive = min(w_passive, 0.1)
            logger.warning("[ModeController] 极端市场，强制Active模式")
        
        # 队列深度不足
        if signals.queue_depth < 100:
            # 队列太浅，降低passive
            w_passive *= 0.5
            logger.debug("[ModeController] 队列深度不足，降低passive")
        
        return w_passive
    
    def _time_to_next_funding(self) -> float:
        """
        计算到下次资金费结算的时间
        
        Returns:
            秒数
        """
        # 资金费每8小时结算一次（UTC 0:00, 8:00, 16:00）
        current_hour = time.gmtime().tm_hour
        hours_to_next = min((8 - current_hour % 8) % 8, 
                           (16 - current_hour % 8) % 8,
                           (24 - current_hour % 8) % 8)
        if hours_to_next == 0:
            hours_to_next = 8
        
        return hours_to_next * 3600
    
    def _update_stats(self, w_passive: float, signals: MarketSignals) -> None:
        """
        更新统计信息
        
        Args:
            w_passive: Passive权重
            signals: 市场信号
        """
        self.weight_history.append(w_passive)
        
        # 更新平均权重
        if self.weight_history:
            self.stats['avg_passive_weight'] = sum(self.weight_history) / len(self.weight_history)
        
        # 判断市场状态
        if signals.sigma_30s < 0.001 and signals.queue_toxicity < 0.3:
            self.stats['current_regime'] = MarketRegime.CALM
        elif signals.sigma_30s > 0.005 or signals.queue_toxicity > 0.7:
            self.stats['current_regime'] = MarketRegime.STRESSED
        elif signals.sigma_30s > 0.002 or signals.queue_toxicity > 0.5:
            self.stats['current_regime'] = MarketRegime.VOLATILE
        else:
            self.stats['current_regime'] = MarketRegime.NORMAL
        
        self.stats['last_update_ts'] = time.time()
    
    def split_hedge_quantity(self, total_qty: float, w_passive: float) -> Tuple[float, float]:
        """
        拆分对冲数量
        
        Args:
            total_qty: 总对冲量
            w_passive: Passive权重
        
        Returns:
            (passive_qty, active_qty)
        """
        passive_qty = round(total_qty * w_passive, 2)
        active_qty = total_qty - passive_qty
        
        # 确保至少有最小量
        min_qty = 10.0  # 最小10 DOGE
        if passive_qty > 0 and passive_qty < min_qty:
            passive_qty = min_qty if total_qty >= min_qty else 0
        if active_qty > 0 and active_qty < min_qty:
            active_qty = min_qty if total_qty >= min_qty else 0
        
        # 重新平衡确保总量正确
        if passive_qty + active_qty != total_qty:
            diff = total_qty - (passive_qty + active_qty)
            if w_passive > 0.5:
                passive_qty += diff
            else:
                active_qty += diff
        
        return (passive_qty, active_qty)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'current_weight': self.last_weight,
            'weight_std': self._calculate_weight_std(),
            'time_to_funding': self._time_to_next_funding() / 3600  # 小时
        }
    
    def _calculate_weight_std(self) -> float:
        """
        计算权重标准差
        
        Returns:
            标准差
        """
        if len(self.weight_history) < 2:
            return 0.0
        
        mean = sum(self.weight_history) / len(self.weight_history)
        variance = sum((w - mean) ** 2 for w in self.weight_history) / len(self.weight_history)
        return math.sqrt(variance)
    
    def reset(self) -> None:
        """
        重置控制器
        """
        self.last_weight = self.a0
        self.weight_history.clear()
        self.signal_history.clear()
        self.stats['mode_changes'] = 0
        self.stats['avg_passive_weight'] = self.a0
        self.stats['current_regime'] = MarketRegime.NORMAL
        
        logger.info("[ModeController] 控制器已重置")