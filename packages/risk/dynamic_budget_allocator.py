"""
C8: 预算分配优化 - 动态burst管理和预算借调机制
基于实时利用率和系统状态的智能预算重分配
"""
import time
import math
import logging
from typing import Dict, Any, List, Tuple, Optional
from collections import deque

logger = logging.getLogger(__name__)


class DynamicBudgetAllocator:
    """C8: 动态预算分配器 - 智能burst管理和预算借调"""
    
    def __init__(self):
        # 基础配置
        self.window_size = 60.0          # 观测窗口(秒)
        self.min_samples = 5             # 最小样本数
        
        # burst管理参数
        self.base_burst_ratio = 1.0      # 基础burst倍数
        self.max_burst_ratio = 3.0       # 最大burst倍数
        self.burst_decay_rate = 0.95     # burst衰减率
        
        # 预算借调参数
        self.max_borrow_ratio = 0.5      # 最大借调比例
        self.borrow_priority = ['cancel', 'reprice', 'fill']  # 借调优先级
        self.payback_rate = 0.1          # 归还率
        
        # 历史数据
        self.usage_history = deque()     # (timestamp, budget_type, usage_ratio)
        self.burst_history = deque()     # (timestamp, budget_type, burst_used)
        self.emergency_history = deque() # (timestamp, emergency_level)
        
        # 预算状态
        self.current_budgets = {'fill': 10, 'reprice': 10, 'cancel': 25}
        self.current_bursts = {'fill': 10, 'reprice': 10, 'cancel': 25}
        self.borrowed_amounts = {'fill': 0, 'reprice': 0, 'cancel': 0}
        self.lent_amounts = {'fill': 0, 'reprice': 0, 'cancel': 0}
        
        # 自适应因子
        self.urgency_factors = {'fill': 1.0, 'reprice': 1.0, 'cancel': 1.0}
        self.efficiency_scores = {'fill': 1.0, 'reprice': 1.0, 'cancel': 1.0}
        
        logger.info(f"[C8] DynamicBudgetAllocator initialized: burst_ratio={self.base_burst_ratio}-{self.max_burst_ratio}")
    
    def update_usage_data(self, budget_type: str, used: int, total: int):
        """更新预算使用情况"""
        now = time.time()
        usage_ratio = used / max(1, total)
        self.usage_history.append((now, budget_type, usage_ratio))
        self._cleanup_old_data()
    
    def update_burst_data(self, budget_type: str, burst_used: int):
        """更新burst使用情况"""
        now = time.time()
        self.burst_history.append((now, budget_type, burst_used))
        self._cleanup_old_data()
    
    def update_emergency_level(self, emergency_level: float):
        """更新系统紧急程度 [0,1]"""
        now = time.time()
        self.emergency_history.append((now, emergency_level))
        self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """清理过期数据"""
        now = time.time()
        cutoff = now - self.window_size
        
        while self.usage_history and self.usage_history[0][0] < cutoff:
            self.usage_history.popleft()
        while self.burst_history and self.burst_history[0][0] < cutoff:
            self.burst_history.popleft()
        while self.emergency_history and self.emergency_history[0][0] < cutoff:
            self.emergency_history.popleft()
    
    def calculate_usage_efficiency(self, budget_type: str) -> float:
        """计算预算使用效率 [0,2]"""
        recent_usage = [(ts, bt, ur) for ts, bt, ur in self.usage_history 
                       if bt == budget_type and time.time() - ts <= 30.0]
        
        if len(recent_usage) < self.min_samples:
            return 1.0  # 默认效率
            
        usage_ratios = [ur for _, _, ur in recent_usage]
        avg_usage = sum(usage_ratios) / len(usage_ratios)
        
        # 效率评分：理想使用率85%左右
        if 0.8 <= avg_usage <= 0.9:
            efficiency = 2.0  # 高效
        elif 0.6 <= avg_usage <= 1.0:
            efficiency = 1.5  # 良好
        elif 0.3 <= avg_usage <= 0.6:
            efficiency = 1.0  # 一般
        else:
            efficiency = 0.5  # 低效
            
        return efficiency
    
    def calculate_urgency_factor(self, budget_type: str) -> float:
        """计算紧急程度因子 [0.5, 3.0]"""
        # 因子1：最近使用率突增
        recent_usage = [(ts, bt, ur) for ts, bt, ur in self.usage_history 
                       if bt == budget_type and time.time() - ts <= 10.0]
        
        usage_surge = 1.0
        if len(recent_usage) >= 3:
            recent_ratios = [ur for _, _, ur in recent_usage[-3:]]
            older_ratios = [ur for _, _, ur in recent_usage[:-3]] if len(recent_usage) > 3 else recent_ratios
            
            if older_ratios:
                recent_avg = sum(recent_ratios) / len(recent_ratios)
                older_avg = sum(older_ratios) / len(older_ratios)
                usage_surge = min(3.0, recent_avg / max(0.1, older_avg))
        
        # 因子2：burst使用频率
        recent_bursts = [(ts, bt, bu) for ts, bt, bu in self.burst_history 
                        if bt == budget_type and time.time() - ts <= 20.0]
        
        burst_frequency = len(recent_bursts) / 20.0  # 每秒burst次数
        burst_factor = 1.0 + min(1.0, burst_frequency * 10)  # [1.0, 2.0]
        
        # 因子3：系统紧急程度
        emergency_factor = 1.0
        if self.emergency_history:
            recent_emergency = self.emergency_history[-1][1]
            emergency_factor = 1.0 + recent_emergency  # [1.0, 2.0]
        
        # 综合紧急程度
        total_urgency = usage_surge * burst_factor * emergency_factor
        return max(0.5, min(3.0, total_urgency))
    
    def calculate_dynamic_burst(self, budget_type: str, base_budget: int) -> int:
        """C8: 计算动态burst大小"""
        efficiency = self.calculate_usage_efficiency(budget_type)
        urgency = self.calculate_urgency_factor(budget_type)
        
        # 基础burst比例
        base_ratio = self.base_burst_ratio
        
        # 根据效率调整：高效率允许更大burst
        efficiency_bonus = (efficiency - 1.0) * 0.5  # [-0.5, 0.5]
        
        # 根据紧急程度调整：高紧急程度需要更大burst
        urgency_bonus = (urgency - 1.0) * 0.3  # [-0.15, 0.6]
        
        # 计算最终burst比例
        dynamic_ratio = base_ratio + efficiency_bonus + urgency_bonus
        dynamic_ratio = max(self.base_burst_ratio, min(self.max_burst_ratio, dynamic_ratio))
        
        # 计算burst大小
        dynamic_burst = int(base_budget * dynamic_ratio)
        
        # 记录因子用于日志
        self.efficiency_scores[budget_type] = efficiency
        self.urgency_factors[budget_type] = urgency
        
        return dynamic_burst
    
    def calculate_budget_borrowing(self, budgets: Dict[str, int]) -> Dict[str, int]:
        """C8: 计算预算借调分配"""
        adjusted_budgets = budgets.copy()
        
        # 重置借调状态
        self.borrowed_amounts = {k: 0 for k in budgets.keys()}
        self.lent_amounts = {k: 0 for k in budgets.keys()}
        
        # 计算各类型的紧急程度
        urgencies = {bt: self.calculate_urgency_factor(bt) for bt in budgets.keys()}
        efficiencies = {bt: self.calculate_usage_efficiency(bt) for bt in budgets.keys()}
        
        # 识别需要借调的类型（高紧急程度 + 高效率）
        borrow_candidates = []
        for budget_type in budgets.keys():
            if urgencies[budget_type] > 2.0 and efficiencies[budget_type] > 1.2:
                borrow_score = urgencies[budget_type] * efficiencies[budget_type]
                borrow_candidates.append((budget_type, borrow_score))
        
        # 按需求程度排序
        borrow_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # 执行借调
        for borrow_type, borrow_score in borrow_candidates:
            max_borrow = int(budgets[borrow_type] * self.max_borrow_ratio)
            current_borrow = 0
            
            # 按优先级顺序尝试借调
            for lend_type in self.borrow_priority:
                if lend_type == borrow_type:
                    continue
                    
                # 检查是否可以出借
                lend_urgency = urgencies[lend_type]
                lend_efficiency = efficiencies[lend_type]
                
                if lend_urgency < 1.5 and lend_efficiency < 1.2:  # 低需求
                    available_to_lend = int(budgets[lend_type] * self.max_borrow_ratio)
                    already_lent = self.lent_amounts[lend_type]
                    can_lend = max(0, available_to_lend - already_lent)
                    
                    if can_lend > 0:
                        borrow_amount = min(can_lend, max_borrow - current_borrow)
                        
                        # 执行借调
                        adjusted_budgets[borrow_type] += borrow_amount
                        adjusted_budgets[lend_type] -= borrow_amount
                        
                        self.borrowed_amounts[borrow_type] += borrow_amount
                        self.lent_amounts[lend_type] += borrow_amount
                        
                        current_borrow += borrow_amount
                        
                        logger.debug(f"[C8] 预算借调: {lend_type}→{borrow_type} 数量={borrow_amount}")
                        
                        if current_borrow >= max_borrow:
                            break
        
        return adjusted_budgets
    
    def optimize_budget_allocation(self, base_budgets: Dict[str, int]) -> Dict[str, Any]:
        """
        C8: 综合预算分配优化
        
        Returns:
            {
                'budgets': Dict[str, int],
                'bursts': Dict[str, int], 
                'borrowing_info': Dict[str, Any]
            }
        """
        # 1. 计算动态burst
        dynamic_bursts = {}
        for budget_type, base_budget in base_budgets.items():
            dynamic_bursts[budget_type] = self.calculate_dynamic_burst(budget_type, base_budget)
        
        # 2. 计算预算借调
        adjusted_budgets = self.calculate_budget_borrowing(base_budgets)
        
        # 3. 更新内部状态
        self.current_budgets = adjusted_budgets.copy()
        self.current_bursts = dynamic_bursts.copy()
        
        return {
            'budgets': adjusted_budgets,
            'bursts': dynamic_bursts,
            'borrowing_info': {
                'borrowed': self.borrowed_amounts.copy(),
                'lent': self.lent_amounts.copy(),
                'efficiency_scores': self.efficiency_scores.copy(),
                'urgency_factors': self.urgency_factors.copy()
            }
        }
    
    def analyze_allocation_performance(self) -> Dict[str, Any]:
        """分析分配性能"""
        return {
            'current_state': {
                'budgets': self.current_budgets,
                'bursts': self.current_bursts,
                'borrowed': self.borrowed_amounts,
                'lent': self.lent_amounts
            },
            'performance_metrics': {
                'efficiency_scores': self.efficiency_scores,
                'urgency_factors': self.urgency_factors
            },
            'samples': {
                'usage': len(self.usage_history),
                'burst': len(self.burst_history),
                'emergency': len(self.emergency_history)
            }
        }
    
    def log_analysis(self, analysis: Dict[str, Any]):
        """按照Phase 9模板输出C8状态线"""
        performance = analysis['performance_metrics']
        state = analysis['current_state']
        
        # 计算总借调量
        total_borrowed = sum(state['borrowed'].values())
        total_lent = sum(state['lent'].values())
        
        logger.info(
            f"[C8] borrowed={total_borrowed} lent={total_lent} "
            f"eff_scores=({performance['efficiency_scores']['fill']:.1f},"
            f"{performance['efficiency_scores']['reprice']:.1f},"
            f"{performance['efficiency_scores']['cancel']:.1f}) "
            f"urgency=({performance['urgency_factors']['fill']:.1f},"
            f"{performance['urgency_factors']['reprice']:.1f},"
            f"{performance['urgency_factors']['cancel']:.1f})"
        )