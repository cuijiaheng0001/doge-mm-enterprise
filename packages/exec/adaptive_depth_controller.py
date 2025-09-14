"""
B6: 双边存在约束进阶 - 深度随价差/流动性自适应，gate紧张时L0优先
基于市场微观结构和预算约束的智能槽位分配
"""
import time
import logging
from typing import Dict, Any, List, Tuple, Optional
from collections import deque

logger = logging.getLogger(__name__)


class AdaptiveDepthController:
    """B6: 自适应深度控制器 - 基于spread/liquidity/gate状态的智能槽位分配"""
    
    def __init__(self):
        # 基础配置
        self.spread_target_bps = 8.0       # 目标spread (bp)
        self.liquidity_window_size = 30.0  # 流动性观测窗口 (秒)
        self.min_samples = 5               # 最小样本数
        
        # 槽位约束
        self.max_total_slots = 12          # 最大总槽位
        self.min_l0_slots = 2              # L0最小槽位 (每侧)
        self.max_l0_slots = 4              # L0最大槽位 (每侧)
        self.base_l1_slots = 2             # L1基础槽位 (每侧)
        self.max_l1_slots = 4              # L1最大槽位 (每侧)
        
        # 历史数据
        self.spread_history = deque()       # (timestamp, spread_bps)
        self.depth_history = deque()        # (timestamp, bid_qty, ask_qty)
        self.gate_history = deque()         # (timestamp, buy_budget, sell_budget)
        
        # 自适应参数
        self.spread_sensitivity = 2.0       # spread敏感度
        self.liquidity_sensitivity = 1.5    # 流动性敏感度
        self.gate_threshold = 5.0           # gate紧张阈值
        
        logger.info(f"[B6] AdaptiveDepthController initialized: spread_target={self.spread_target_bps}bp")
    
    def update_market_data(self, spread_bps: float, bid_qty: float, ask_qty: float):
        """更新市场数据"""
        now = time.time()
        self.spread_history.append((now, spread_bps))
        self.depth_history.append((now, bid_qty, ask_qty))
        self._cleanup_old_data()
    
    def update_gate_status(self, buy_budget: float, sell_budget: float):
        """更新gate预算状态"""
        now = time.time()
        self.gate_history.append((now, buy_budget, sell_budget))
        self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """清理过期数据"""
        now = time.time()
        cutoff = now - self.liquidity_window_size
        
        while self.spread_history and self.spread_history[0][0] < cutoff:
            self.spread_history.popleft()
        while self.depth_history and self.depth_history[0][0] < cutoff:
            self.depth_history.popleft()
        while self.gate_history and self.gate_history[0][0] < cutoff:
            self.gate_history.popleft()
    
    def calculate_spread_pressure(self) -> float:
        """计算价差压力 [0,1] - 0为宽价差，1为窄价差"""
        if len(self.spread_history) < self.min_samples:
            return 0.5  # 默认中性
            
        recent_spreads = [s for _, s in list(self.spread_history)[-10:]]
        avg_spread = sum(recent_spreads) / len(recent_spreads)
        
        # spread压力：当前spread相对目标的压缩程度
        pressure = max(0.0, 1.0 - (avg_spread / self.spread_target_bps))
        return min(1.0, pressure)
    
    def calculate_liquidity_pressure(self) -> float:
        """计算流动性压力 [0,1] - 0为充足，1为稀缺"""
        if len(self.depth_history) < self.min_samples:
            return 0.5  # 默认中性
            
        recent_depths = list(self.depth_history)[-10:]
        total_depths = [(bid + ask) for _, bid, ask in recent_depths]
        
        if not total_depths:
            return 0.5
            
        avg_depth = sum(total_depths) / len(total_depths)
        
        # 基于经验设定：1000为充足，100为稀缺
        pressure = max(0.0, 1.0 - (avg_depth / 1000.0))
        return min(1.0, pressure)
    
    def calculate_gate_pressure(self) -> float:
        """计算gate压力 [0,1] - 0为充足，1为紧张"""
        if len(self.gate_history) < 2:
            return 0.0  # 默认充足
            
        # 取最近的gate状态
        _, buy_budget, sell_budget = self.gate_history[-1]
        total_budget = buy_budget + sell_budget
        
        # gate压力：预算越少压力越大
        pressure = max(0.0, 1.0 - (total_budget / (self.gate_threshold * 2)))
        return min(1.0, pressure)
    
    def calculate_adaptive_allocation(self, current_slots: Dict[int, int]) -> Dict[str, Any]:
        """
        B6: 计算自适应槽位分配
        
        策略：
        1. 价差压缩时：增加L0槽位提高成交概率
        2. 流动性稀缺时：增加总槽位提高存在感
        3. gate紧张时：优先L0，减少L1/L2
        """
        spread_pressure = self.calculate_spread_pressure()
        liquidity_pressure = self.calculate_liquidity_pressure()
        gate_pressure = self.calculate_gate_pressure()
        
        # B6: 自适应槽位计算
        # L0槽位：受价差和gate压力影响
        l0_boost = spread_pressure * self.spread_sensitivity
        l0_gate_penalty = gate_pressure * 0.5  # gate紧时适度减少L0
        target_l0_slots = self.min_l0_slots + l0_boost - l0_gate_penalty
        target_l0_slots = max(self.min_l0_slots, min(self.max_l0_slots, target_l0_slots))
        
        # L1槽位：受流动性和gate压力影响
        l1_boost = liquidity_pressure * self.liquidity_sensitivity
        l1_gate_penalty = gate_pressure * 1.0  # gate紧时重点削减L1
        target_l1_slots = self.base_l1_slots + l1_boost - l1_gate_penalty
        target_l1_slots = max(0, min(self.max_l1_slots, target_l1_slots))
        
        # gate紧急模式：强制L0优先
        if gate_pressure > 0.7:
            target_l0_slots = max(target_l0_slots, self.min_l0_slots)
            target_l1_slots = min(target_l1_slots, 1)  # L1最多1槽
            logger.debug(f"[B6] Gate emergency mode: L0={target_l0_slots} L1={target_l1_slots}")
        
        # 总槽位约束
        total_target = (target_l0_slots + target_l1_slots) * 2  # 双边
        if total_target > self.max_total_slots:
            # 优先保证L0，按比例缩减L1
            scale_factor = (self.max_total_slots - target_l0_slots * 2) / (target_l1_slots * 2)
            target_l1_slots = max(0, target_l1_slots * scale_factor)
        
        # 输出建议
        allocation = {
            'l0_slots_per_side': int(round(target_l0_slots)),
            'l1_slots_per_side': int(round(target_l1_slots)),
            'l2_slots_per_side': 0,  # B6暂不调整L2
            'pressures': {
                'spread': spread_pressure,
                'liquidity': liquidity_pressure,
                'gate': gate_pressure
            },
            'reasoning': self._generate_reasoning(spread_pressure, liquidity_pressure, gate_pressure)
        }
        
        return allocation
    
    def _generate_reasoning(self, spread_p: float, liquidity_p: float, gate_p: float) -> str:
        """生成分配逻辑推理"""
        reasons = []
        
        if spread_p > 0.6:
            reasons.append("spread压缩→增加L0")
        if liquidity_p > 0.6:
            reasons.append("流动性稀缺→增加总深度")
        if gate_p > 0.7:
            reasons.append("gate紧张→L0优先")
        elif gate_p > 0.4:
            reasons.append("gate压力→减少L1")
        
        if not reasons:
            reasons.append("市场平稳→基础配置")
        
        return " | ".join(reasons)
    
    def analyze_depth_adaptation(self) -> Dict[str, Any]:
        """分析当前深度自适应状态"""
        return {
            'pressures': {
                'spread': self.calculate_spread_pressure(),
                'liquidity': self.calculate_liquidity_pressure(),
                'gate': self.calculate_gate_pressure()
            },
            'samples': {
                'spread': len(self.spread_history),
                'depth': len(self.depth_history),
                'gate': len(self.gate_history)
            },
            'targets': {
                'spread_bps': self.spread_target_bps,
                'max_total_slots': self.max_total_slots,
                'min_l0_slots': self.min_l0_slots
            }
        }
    
    def log_analysis(self, allocation: Dict[str, Any]):
        """按照Phase 9模板输出B6状态线"""
        pressures = allocation['pressures']
        logger.info(
            f"[B6] spread_p={pressures['spread']:.2f} "
            f"liquidity_p={pressures['liquidity']:.2f} "
            f"gate_p={pressures['gate']:.2f} "
            f"L0/L1={allocation['l0_slots_per_side']}/{allocation['l1_slots_per_side']} "
            f"reason='{allocation['reasoning']}'"
        )