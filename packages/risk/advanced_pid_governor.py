"""
C7: PID Governor进阶 - 自适应调节fill/reprice/cancel预算根据市场状态
基于市场微观结构、波动率、流动性的智能预算分配
"""
import time
import math
import logging
from typing import Dict, Any, List, Tuple, Optional
from collections import deque

logger = logging.getLogger(__name__)


class AdvancedPIDGovernor:
    """C7: 进阶PID控制器 - 基于市场微观结构的自适应预算管理"""
    
    def __init__(self, usage_target_pct=10.0, usage_safe_pct=15.0):
        # 基础配置
        self.usage_target = usage_target_pct
        self.usage_safe = usage_safe_pct
        
        # PID参数（自适应）
        self.base_kp = 0.06
        self.base_ki = 0.015
        self.base_kd = 0.001
        
        # 市场状态感知参数
        self.volatility_window = 30.0       # 波动率观测窗口(秒)
        self.liquidity_window = 30.0        # 流动性观测窗口(秒) 
        self.min_samples = 5                # 最小样本数
        
        # 预算限制
        self.min_budgets = {'fill': 2, 'reprice': 2, 'cancel': 20}
        self.max_budgets = {'fill': 25, 'reprice': 25, 'cancel': 100}  # C7增加上限
        
        # 历史数据
        self.price_history = deque()         # (timestamp, mid_price)
        self.volume_history = deque()        # (timestamp, trade_volume)
        self.spread_history = deque()        # (timestamp, spread_bps)
        self.usage_history = deque()         # (timestamp, usage_pct)
        
        # PID状态
        self.err_integral = 0.0
        self.prev_error = 0.0
        self.last_ts = 0.0
        
        # 自适应因子
        self.volatility_factor = 1.0         # [0.5, 2.0]
        self.liquidity_factor = 1.0          # [0.5, 2.0]
        self.market_stress_factor = 1.0      # [0.5, 3.0]
        
        logger.info(f"[C7] AdvancedPIDGovernor initialized: target={usage_target_pct}%, safe={usage_safe_pct}%")
    
    def update_market_data(self, mid_price: float, trade_volume: float, spread_bps: float):
        """更新市场数据用于自适应计算"""
        now = time.time()
        self.price_history.append((now, mid_price))
        self.volume_history.append((now, trade_volume))
        self.spread_history.append((now, spread_bps))
        self._cleanup_old_data()
    
    def update_usage_data(self, usage_pct: float):
        """更新使用率数据"""
        now = time.time()
        self.usage_history.append((now, usage_pct))
        self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """清理过期数据"""
        now = time.time()
        cutoff = now - max(self.volatility_window, self.liquidity_window)
        
        while self.price_history and self.price_history[0][0] < cutoff:
            self.price_history.popleft()
        while self.volume_history and self.volume_history[0][0] < cutoff:
            self.volume_history.popleft()
        while self.spread_history and self.spread_history[0][0] < cutoff:
            self.spread_history.popleft()
        while self.usage_history and self.usage_history[0][0] < cutoff:
            self.usage_history.popleft()
    
    def calculate_volatility_factor(self) -> float:
        """计算波动率调节因子 [0.5, 2.0]"""
        if len(self.price_history) < self.min_samples:
            return 1.0
            
        prices = [p for _, p in self.price_history]
        if len(prices) < 2:
            return 1.0
            
        # 计算价格对数收益率的标准差
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0 and prices[i] > 0:
                log_returns.append(math.log(prices[i] / prices[i-1]))
        
        if len(log_returns) < 2:
            return 1.0
            
        mean_return = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_return) ** 2 for r in log_returns) / len(log_returns)
        volatility = math.sqrt(variance)
        
        # 将波动率映射到调节因子：高波动率需要更多预算
        # 经验值：0.001为低波动率，0.005为高波动率
        vol_norm = min(1.0, volatility / 0.005)
        factor = 0.5 + 1.5 * vol_norm  # [0.5, 2.0]
        
        return factor
    
    def calculate_liquidity_factor(self) -> float:
        """计算流动性调节因子 [0.5, 2.0]"""
        if len(self.volume_history) < self.min_samples or len(self.spread_history) < self.min_samples:
            return 1.0
            
        # 流动性指标：成交量/价差比率
        recent_volumes = [v for _, v in list(self.volume_history)[-10:]]
        recent_spreads = [s for _, s in list(self.spread_history)[-10:]]
        
        if not recent_volumes or not recent_spreads:
            return 1.0
            
        avg_volume = sum(recent_volumes) / len(recent_volumes)
        avg_spread = sum(recent_spreads) / len(recent_spreads)
        
        if avg_spread <= 0:
            return 1.0
            
        # 流动性指标：成交量越高、价差越小 → 流动性越好 → 需要更多预算
        liquidity_score = avg_volume / max(1.0, avg_spread)
        
        # 归一化到[0.5, 2.0]：高流动性需要更多预算
        # 经验值：1000为中等流动性
        liq_norm = min(1.0, liquidity_score / 1000.0)
        factor = 0.5 + 1.5 * liq_norm
        
        return factor
    
    def calculate_market_stress_factor(self) -> float:
        """计算市场压力因子 [0.5, 3.0]"""
        # 综合价差、波动率、成交突增判断市场压力
        stress_signals = []
        
        # 信号1：价差扩张
        if len(self.spread_history) >= 5:
            recent_spreads = [s for _, s in list(self.spread_history)[-5:]]
            older_spreads = [s for _, s in list(self.spread_history)[-10:-5]] if len(self.spread_history) >= 10 else recent_spreads
            
            if older_spreads:
                recent_avg = sum(recent_spreads) / len(recent_spreads)
                older_avg = sum(older_spreads) / len(older_spreads)
                spread_expansion = recent_avg / max(1.0, older_avg)
                stress_signals.append(min(2.0, max(0.5, spread_expansion)))
        
        # 信号2：成交量突增
        if len(self.volume_history) >= 5:
            recent_volumes = [v for _, v in list(self.volume_history)[-5:]]
            older_volumes = [v for _, v in list(self.volume_history)[-10:-5]] if len(self.volume_history) >= 10 else recent_volumes
            
            if older_volumes:
                recent_avg = sum(recent_volumes) / len(recent_volumes)
                older_avg = sum(older_volumes) / len(older_volumes)
                volume_surge = recent_avg / max(1.0, older_avg)
                stress_signals.append(min(2.0, max(0.5, volume_surge)))
        
        # 信号3：使用率偏差
        if len(self.usage_history) >= 3:
            recent_usage = [u for _, u in list(self.usage_history)[-3:]]
            avg_usage = sum(recent_usage) / len(recent_usage)
            usage_stress = abs(avg_usage - self.usage_target) / self.usage_target
            stress_signals.append(1.0 + min(1.0, usage_stress))
        
        if not stress_signals:
            return 1.0
            
        # 取平均值作为综合压力因子
        avg_stress = sum(stress_signals) / len(stress_signals)
        return max(0.5, min(3.0, avg_stress))
    
    def calculate_adaptive_pid_params(self) -> Tuple[float, float, float]:
        """C7: 根据市场状态自适应调整PID参数"""
        # 更新市场因子
        self.volatility_factor = self.calculate_volatility_factor()
        self.liquidity_factor = self.calculate_liquidity_factor()
        self.market_stress_factor = self.calculate_market_stress_factor()
        
        # 自适应PID参数
        # 高波动率 → 增加Kp快速响应
        # 高流动性 → 增加Ki稳态精度
        # 高压力 → 增加Kd抑制震荡
        
        adaptive_kp = self.base_kp * self.volatility_factor * self.market_stress_factor
        adaptive_ki = self.base_ki * self.liquidity_factor
        adaptive_kd = self.base_kd * self.market_stress_factor
        
        return (
            max(0.01, min(0.20, adaptive_kp)),
            max(0.001, min(0.05, adaptive_ki)),
            max(0.0, min(0.01, adaptive_kd))
        )
    
    def step_advanced_pid(self, current_usage: float, dt: float) -> float:
        """C7: 进阶PID控制步骤"""
        error = current_usage - self.usage_target
        
        # 积分项（抗饱和）
        self.err_integral += error * dt
        self.err_integral = max(-50.0, min(50.0, self.err_integral))  # 抗饱和
        
        # 微分项
        derivative = (error - self.prev_error) / max(dt, 1e-3)
        
        # 自适应PID参数
        kp, ki, kd = self.calculate_adaptive_pid_params()
        
        # PID输出
        pid_output = -(kp * error + ki * self.err_integral + kd * derivative)
        
        # 限制单次调整幅度
        max_adjustment = 0.3  # ±30%
        pid_output = max(-max_adjustment, min(max_adjustment, pid_output))
        
        # 缩放因子
        scale_factor = 1.0 + pid_output
        
        # 安全墙：超过安全上限强制缩减
        if current_usage >= self.usage_safe:
            scale_factor = min(scale_factor, 0.7)
        
        # 更新状态
        self.prev_error = error
        
        # 记录调试信息
        logger.debug(f"[C7-PID] error={error:.2f} kp={kp:.3f} ki={ki:.4f} kd={kd:.4f} "
                    f"vol_f={self.volatility_factor:.2f} liq_f={self.liquidity_factor:.2f} "
                    f"stress_f={self.market_stress_factor:.2f} scale={scale_factor:.3f}")
        
        return max(0.3, min(2.0, scale_factor))
    
    def calculate_budget_allocation(self, base_budgets: Dict[str, int], 
                                  market_conditions: Dict[str, float]) -> Dict[str, int]:
        """
        C7: 基于市场条件的智能预算分配
        
        Args:
            base_budgets: {'fill': int, 'reprice': int, 'cancel': int}
            market_conditions: {'volatility': float, 'liquidity': float, 'stress': float}
        """
        allocated = {}
        
        # 根据市场条件调整各类型预算
        for budget_type, base_value in base_budgets.items():
            if budget_type == 'fill':
                # fill预算：流动性好时增加，压力大时减少
                factor = self.liquidity_factor * (2.0 - self.market_stress_factor * 0.5)
            elif budget_type == 'reprice':
                # reprice预算：波动率高时增加
                factor = self.volatility_factor
            elif budget_type == 'cancel':
                # cancel预算：压力大时增加（更多撤单）
                factor = self.market_stress_factor
            else:
                factor = 1.0
            
            # 应用调整
            adjusted_value = int(base_value * factor)
            
            # 限制在合理范围
            min_val = self.min_budgets.get(budget_type, 2)
            max_val = self.max_budgets.get(budget_type, 20)
            allocated[budget_type] = max(min_val, min(max_val, adjusted_value))
        
        return allocated
    
    def analyze_market_regime(self) -> Dict[str, Any]:
        """分析当前市场状态"""
        return {
            'volatility_factor': self.volatility_factor,
            'liquidity_factor': self.liquidity_factor, 
            'market_stress_factor': self.market_stress_factor,
            'adaptive_pid_params': self.calculate_adaptive_pid_params(),
            'samples': {
                'price': len(self.price_history),
                'volume': len(self.volume_history),
                'spread': len(self.spread_history),
                'usage': len(self.usage_history)
            },
            'regime_classification': self._classify_market_regime()
        }
    
    def _classify_market_regime(self) -> str:
        """分类市场状态"""
        if self.market_stress_factor > 2.0:
            return "HIGH_STRESS"
        elif self.volatility_factor > 1.5:
            return "HIGH_VOLATILITY"
        elif self.liquidity_factor < 0.7:
            return "LOW_LIQUIDITY"
        elif self.market_stress_factor < 0.8 and self.volatility_factor < 1.2:
            return "CALM"
        else:
            return "NORMAL"
    
    def log_analysis(self, analysis: Dict[str, Any]):
        """按照Phase 9模板输出C7状态线"""
        regime = analysis['regime_classification']
        kp, ki, kd = analysis['adaptive_pid_params']
        
        logger.info(
            f"[C7] regime={regime} "
            f"vol_f={analysis['volatility_factor']:.2f} "
            f"liq_f={analysis['liquidity_factor']:.2f} "
            f"stress_f={analysis['market_stress_factor']:.2f} "
            f"pid_params=({kp:.3f},{ki:.4f},{kd:.4f})"
        )