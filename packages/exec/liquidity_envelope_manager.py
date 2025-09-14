#!/usr/bin/env python3
"""
Phase 7.3: 流动性包络管理 - 替代"无限下单"
基于good version文档中的Phase 7设计

核心理念: "织网而非撒点，有目标的在册管理"
目标: 资金利用率95%+，防止订单堆积，零空档守卫
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
from enum import Enum

logger = logging.getLogger(__name__)

class EnvelopeAlert(Enum):
    """包络警报类型"""
    NORMAL = "normal"
    LOW_COVERAGE = "low_coverage"
    ZERO_GAP = "zero_gap"  
    EMERGENCY = "emergency"

@dataclass
class SideTarget:
    """单侧目标配置"""
    notional_target: float
    l0_slots: int
    l1_slots: int  
    l2_slots: int
    min_l0_slots: int = 8  # 硬约束

@dataclass
class EnvelopeStatus:
    """包络状态"""
    buy_side: SideTarget
    sell_side: SideTarget
    current_buy_notional: float
    current_sell_notional: float
    coverage_ratio: float
    alert_level: EnvelopeAlert

class LiquidityEnvelopeManager:
    """
    Phase 7.3: 流动性包络管理器
    实现织网式在册管理，替代无限下单模式
    """
    
    def __init__(self, alpha_base: float = 0.15, alpha_k: float = 0.20, 
                 alpha_min: float = 0.10, alpha_max: float = 0.35, min_l0_slots: int = 8):
        """
        初始化DCR确定性收敛再平衡器
        
        Args:
            alpha_base: 基础资金利用率 (15%)
            alpha_k: 失衡系数 (0.20)
            alpha_min: 最小资金利用率 (10%)
            alpha_max: 最大资金利用率 (35%)
            min_l0_slots: L0最少槽位数 (硬约束)
        """
        # DCR动态alpha参数
        self.alpha_base = alpha_base
        self.alpha_k = alpha_k 
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.min_l0_slots = min_l0_slots
        
        # 收敛轨迹跟踪
        self.error_history: List[Tuple[float, float]] = []  # [(timestamp, error)]
        self.convergence_target = 15.0  # ε_value = 15.0 USD (约4.2%失衡)
        self.trajectory_decay_rate = 0.8  # 20%递减率
        
        # 容量估算参数
        self.maker_capacity_ratio = 0.4  # 40%资金用做市
        self.taker_capacity_ratio = 0.6  # 60%资金用接单
        self.avg_fill_size = 10.0  # 平均成交单价
        self.spread_cushion = 8.0  # 价差缓冲倍数
        
        # 层级资金分配 (基于good version文档)
        self.layer_allocation = {
            'L0': 0.70,  # 70%资金用于L0（成交与补位）
            'L1': 0.25,  # 25%资金用于L1（稳定深度）
            'L2': 0.05   # 5%资金用于L2（展示深度）
        }
        
        # 包络状态跟踪
        self.current_status: Optional[EnvelopeStatus] = None
        self.last_update = 0
        self.alert_history: List[Tuple[float, EnvelopeAlert]] = []
        
        # 零空档守卫
        self.zero_gap_threshold = 8  # 任一侧L0<8即触发
        self.emergency_rebalance_cooldown = 30  # 30秒应急冷却
        self.last_emergency = 0
        
        logger.info(f"[Phase7.3-DCR] DCR确定性收敛再平衡器初始化: α_base={alpha_base}, k={alpha_k}, range=[{alpha_min}, {alpha_max}]")

    def calculate_dynamic_alpha(self, equity: float, inventory_skew: float) -> float:
        """
        计算动态资金配置系数
        公式: α(e) = clamp(α_base + k·|e|, α_min, α_max)
        
        Args:
            equity: 账户净值
            inventory_skew: 库存偏斜 (-1到1)
            
        Returns:
            动态alpha值
        """
        # 计算库存误差的绝对值
        abs_error = abs(inventory_skew)
        
        # 动态alpha计算
        dynamic_alpha = self.alpha_base + self.alpha_k * abs_error
        
        # 限制在[α_min, α_max]范围内
        clamped_alpha = max(self.alpha_min, min(self.alpha_max, dynamic_alpha))
        
        logger.debug(f"[Phase7.3-DCR] 动态Alpha计算: |e|={abs_error:.3f}, α={clamped_alpha:.3f} (base={self.alpha_base}, k={self.alpha_k})")
        
        return clamped_alpha
    
    def calculate_envelope_targets(self, equity: float, 
                                 inventory_skew: float) -> Tuple[SideTarget, SideTarget]:
        """
        计算双边包络目标(使用动态alpha)
        
        Args:
            equity: 账户净值
            inventory_skew: 库存偏斜 (-1到1)
            
        Returns:
            (buy_target, sell_target)
        """
        # 使用动态alpha计算总目标在册金额
        dynamic_alpha = self.calculate_dynamic_alpha(equity, inventory_skew)
        total_target = equity * dynamic_alpha
        
        # 根据库存偏斜分配到两侧 (35/65 ～ 65/35 动态)
        if inventory_skew > 0.1:  # DOGE过多，需要更多卖单
            buy_ratio, sell_ratio = 0.35, 0.65
        elif inventory_skew < -0.1:  # USDT过多，需要更多买单
            buy_ratio, sell_ratio = 0.65, 0.35
        else:  # 平衡状态
            buy_ratio, sell_ratio = 0.5, 0.5
        
        buy_notional = total_target * buy_ratio
        sell_notional = total_target * sell_ratio
        
        # 计算槽位配置
        buy_target = self._calculate_side_slots(buy_notional, 'BUY')
        sell_target = self._calculate_side_slots(sell_notional, 'SELL')
        
        return buy_target, sell_target

    def _calculate_side_slots(self, notional: float, side: str) -> SideTarget:
        """计算单侧槽位配置"""
        # 按层级分配资金
        l0_notional = notional * self.layer_allocation['L0']
        l1_notional = notional * self.layer_allocation['L1'] 
        l2_notional = notional * self.layer_allocation['L2']
        
        # 估算槽位数 (假设平均每个槽位$10)
        avg_slot_value = 10.0
        l0_slots = max(self.min_l0_slots, int(l0_notional / avg_slot_value))
        l1_slots = max(2, int(l1_notional / avg_slot_value))
        l2_slots = max(1, int(l2_notional / avg_slot_value))
        
        return SideTarget(
            notional_target=notional,
            l0_slots=l0_slots,
            l1_slots=l1_slots,
            l2_slots=l2_slots,
            min_l0_slots=self.min_l0_slots
        )

    def update_current_status(self, buy_notional: float, sell_notional: float,
                            buy_l0_count: int, sell_l0_count: int,
                            equity: float, inventory_skew: float):
        """更新当前包络状态"""
        # 计算目标
        buy_target, sell_target = self.calculate_envelope_targets(equity, inventory_skew)
        
        # 计算覆盖率
        total_target = buy_target.notional_target + sell_target.notional_target
        total_current = buy_notional + sell_notional
        coverage_ratio = total_current / total_target if total_target > 0 else 0
        
        # 判断警报级别
        alert_level = self._assess_alert_level(
            buy_target, sell_target, buy_notional, sell_notional,
            buy_l0_count, sell_l0_count
        )
        
        # 更新状态
        self.current_status = EnvelopeStatus(
            buy_side=buy_target,
            sell_side=sell_target,
            current_buy_notional=buy_notional,
            current_sell_notional=sell_notional,
            coverage_ratio=coverage_ratio,
            alert_level=alert_level
        )
        
        self.last_update = time.time()
        
        # 记录警报历史
        if alert_level != EnvelopeAlert.NORMAL:
            self.alert_history.append((self.last_update, alert_level))
            # 保持最近20个警报
            if len(self.alert_history) > 20:
                self.alert_history.pop(0)

    def _assess_alert_level(self, buy_target: SideTarget, sell_target: SideTarget,
                          buy_notional: float, sell_notional: float,
                          buy_l0_count: int, sell_l0_count: int) -> EnvelopeAlert:
        """评估警报级别"""
        # 检查零空档（最高优先级）
        if buy_l0_count < self.zero_gap_threshold or sell_l0_count < self.zero_gap_threshold:
            return EnvelopeAlert.ZERO_GAP
        
        # 检查覆盖率
        buy_coverage = buy_notional / buy_target.notional_target if buy_target.notional_target > 0 else 0
        sell_coverage = sell_notional / sell_target.notional_target if sell_target.notional_target > 0 else 0
        
        min_coverage = min(buy_coverage, sell_coverage)
        
        if min_coverage < 0.5:  # 50%以下覆盖率
            return EnvelopeAlert.EMERGENCY
        elif min_coverage < 0.8:  # 80%以下覆盖率
            return EnvelopeAlert.LOW_COVERAGE
        else:
            return EnvelopeAlert.NORMAL

    def get_deployment_priority(self) -> Dict[str, int]:
        """
        获取部署优先级
        返回各层级的紧急程度评分 (0-100)
        """
        if not self.current_status:
            return {}
        
        priority = {}
        
        # 零空档守卫最高优先级
        if self.current_status.alert_level == EnvelopeAlert.ZERO_GAP:
            priority['L0_BUY'] = 100
            priority['L0_SELL'] = 100
        elif self.current_status.alert_level == EnvelopeAlert.EMERGENCY:
            priority['L0_BUY'] = 90
            priority['L0_SELL'] = 90
            priority['L1_BUY'] = 70
            priority['L1_SELL'] = 70
        elif self.current_status.alert_level == EnvelopeAlert.LOW_COVERAGE:
            priority['L0_BUY'] = 60
            priority['L0_SELL'] = 60
            priority['L1_BUY'] = 40
            priority['L1_SELL'] = 40
        else:
            # 正常状态，根据缺口确定优先级
            buy_gap = max(0, self.current_status.buy_side.notional_target - self.current_status.current_buy_notional)
            sell_gap = max(0, self.current_status.sell_side.notional_target - self.current_status.current_sell_notional)
            
            if buy_gap > 0:
                priority['L0_BUY'] = min(50, int(buy_gap / 10))
                priority['L1_BUY'] = min(30, int(buy_gap / 20))
            
            if sell_gap > 0:
                priority['L0_SELL'] = min(50, int(sell_gap / 10))
                priority['L1_SELL'] = min(30, int(sell_gap / 20))
        
        return priority

    def calculate_maker_taker_capacity(self, equity: float) -> Tuple[float, float]:
        """
        计算做市商/接单商容量
        
        Args:
            equity: 账户净值
            
        Returns:
            (maker_capacity, taker_capacity)
        """
        # 做市容量: C_maker = min(0.4*equity, 240*avg_fill_size)
        maker_capacity = min(self.maker_capacity_ratio * equity, 240 * self.avg_fill_size)
        
        # 接单容量: C_taker = min(0.6*equity, 8*spread_cushion*equity)
        taker_capacity = min(self.taker_capacity_ratio * equity, 
                           self.spread_cushion * self.avg_fill_size * equity / 100)
        
        logger.debug(f"[Phase7.3-DCR] 容量估算: Maker={maker_capacity:.1f}, Taker={taker_capacity:.1f}")
        
        return maker_capacity, taker_capacity
        
    def update_error_history(self, inventory_skew: float, equity: float):
        """
        更新误差历史并检查收敛轨迹
        
        Args:
            inventory_skew: 当前库存偏斜
            equity: 账户净值
        """
        now = time.time()
        current_error = abs(inventory_skew) * equity
        
        self.error_history.append((now, current_error))
        
        # 保持最近10个误差点
        if len(self.error_history) > 10:
            self.error_history.pop(0)
        
        # 检查收敛轨迹(需要至少2个点)
        if len(self.error_history) >= 2:
            prev_error = self.error_history[-2][1]
            current_error = self.error_history[-1][1]
            
            convergence_ratio = current_error / prev_error if prev_error > 0 else 1.0
            target_ratio = self.trajectory_decay_rate
            
            logger.debug(f"[Phase7.3-DCR] 收敛轨迹: 当前误差={current_error:.1f}, 上次误差={prev_error:.1f}, 收敛率={convergence_ratio:.3f} (目标<{target_ratio})")
            
            # 收敛成功标准
            if current_error <= self.convergence_target:
                logger.info(f"[Phase7.3-DCR] ✅ 收敛成功: |e|*equity={current_error:.1f} <= {self.convergence_target}")
                
    def should_trigger_emergency_rebalance(self) -> Tuple[bool, str, float]:
        """判断是否应触发应急再平衡，返回触发原因和强度
        
        Returns:
            (should_trigger, reason, intensity)
        """
        if not self.current_status:
            return False, "no_status", 0.0
            
        now = time.time()
        
        # 冷却期检查
        if now - self.last_emergency < self.emergency_rebalance_cooldown:
            return False, "cooldown", 0.0
        
        # 零空档守卫(最高优先级)
        if self.current_status.alert_level == EnvelopeAlert.ZERO_GAP:
            self.last_emergency = now
            return True, "zero_gap", 1.0  # 最高强度
            
        # 应急模式
        elif self.current_status.alert_level == EnvelopeAlert.EMERGENCY:
            self.last_emergency = now
            return True, "emergency", 0.8  # 高强度
            
        # 低覆盖率(中等强度)
        elif self.current_status.alert_level == EnvelopeAlert.LOW_COVERAGE:
            coverage = self.current_status.coverage_ratio
            if coverage < 0.6:  # 60%以下触发中等强度再平衡
                self.last_emergency = now
                return True, "low_coverage", 0.6  # 中等强度
            
        return False, "normal", 0.0

    def get_missing_slots(self) -> Dict[str, int]:
        """
        计算缺失的槽位数
        用于drip补充
        """
        if not self.current_status:
            return {}
            
        missing = {}
        
        # 简化计算：假设当前槽位不足目标的80%时需要补充
        buy_target = self.current_status.buy_side
        sell_target = self.current_status.sell_side
        
        # L0槽位缺口（优先）
        if self.current_status.current_buy_notional < buy_target.notional_target * 0.8:
            missing['L0_BUY'] = max(0, buy_target.l0_slots - int(self.current_status.current_buy_notional / 10))
            
        if self.current_status.current_sell_notional < sell_target.notional_target * 0.8:
            missing['L0_SELL'] = max(0, sell_target.l0_slots - int(self.current_status.current_sell_notional / 10))
        
        return missing

    def get_dcr_rebalance_plan(self, equity: float, inventory_skew: float) -> Dict[str, any]:
        """
        生成DCR再平衡执行计划
        
        Args:
            equity: 账户净值
            inventory_skew: 库存偏斜
            
        Returns:
            再平衡执行计划
        """
        should_trigger, reason, intensity = self.should_trigger_emergency_rebalance()
        
        if not should_trigger:
            return {'should_execute': False, 'reason': reason}
        
        # 计算容量和预算分配
        maker_capacity, taker_capacity = self.calculate_maker_taker_capacity(equity)
        
        # 根据强度确定执行策略
        if intensity >= 0.8:  # 高强度: Burst模式
            strategy = "burst"
            target_improvement = 0.5  # 50%失衡缩减
            execution_time = 30  # 30秒内完成
        elif intensity >= 0.6:  # 中等强度: Burst+Drip混合
            strategy = "burst_drip"
            target_improvement = 0.3  # 30%失衡缩减
            execution_time = 45  # 45秒内完成
        else:  # 低强度: Drip模式
            strategy = "drip"
            target_improvement = 0.2  # 20%失衡缩减
            execution_time = 60  # 60秒内完成
            
        # 预算分配: 70%做市 + 30%再平衡 + 10%缓冲
        total_budget = equity * self.calculate_dynamic_alpha(equity, inventory_skew)
        maker_budget = total_budget * 0.70
        rebalance_budget = total_budget * 0.30
        emergency_buffer = total_budget * 0.10
        
        return {
            'should_execute': True,
            'reason': reason,
            'intensity': intensity,
            'strategy': strategy,
            'target_improvement': target_improvement,
            'execution_time_seconds': execution_time,
            'total_budget': total_budget,
            'maker_budget': maker_budget,
            'rebalance_budget': rebalance_budget,
            'emergency_buffer': emergency_buffer,
            'maker_capacity': maker_capacity,
            'taker_capacity': taker_capacity,
            'current_error': abs(inventory_skew) * equity,
            'convergence_target': self.convergence_target
        }

    def generate_rebalance_plan(self, equity: float, inventory_skew: float) -> Dict[str, any]:
        """
        生成再平衡执行计划 (兼容方法)
        与get_dcr_rebalance_plan相同，为main.py调用兼容性
        
        Args:
            equity: 账户净值
            inventory_skew: 库存偏斜
            
        Returns:
            再平衡执行计划
        """
        return self.get_dcr_rebalance_plan(equity, inventory_skew)
    
    def get_envelope_stats(self) -> Dict[str, any]:
        """获取DCR增强包络统计信息"""
        if not self.current_status:
            return {'status': 'not_initialized'}
            
        status = self.current_status
        
        # 计算当前动态alpha
        current_alpha = self.alpha_base  # 默认值，实际使用时会重新计算
        
        # 收敛轨迹分析
        convergence_info = {'trajectory': 'unknown', 'error_count': len(self.error_history)}
        if len(self.error_history) >= 2:
            recent_error = self.error_history[-1][1]
            prev_error = self.error_history[-2][1]
            convergence_ratio = recent_error / prev_error if prev_error > 0 else 1.0
            convergence_info = {
                'trajectory': 'converging' if convergence_ratio < self.trajectory_decay_rate else 'diverging',
                'convergence_ratio': convergence_ratio,
                'current_error': recent_error,
                'target_error': self.convergence_target,
                'converged': recent_error <= self.convergence_target
            }
        
        return {
            # DCR增强参数
            'dcr_mode': 'enabled',
            'alpha_base': self.alpha_base,
            'alpha_k': self.alpha_k,
            'alpha_range': f"[{self.alpha_min}, {self.alpha_max}]",
            'current_alpha': current_alpha,
            
            # 传统包络信息
            'total_target': status.buy_side.notional_target + status.sell_side.notional_target,
            'total_current': status.current_buy_notional + status.current_sell_notional,
            'coverage_ratio': status.coverage_ratio,
            'alert_level': status.alert_level.value,
            'buy_target': status.buy_side.notional_target,
            'buy_current': status.current_buy_notional,
            'sell_target': status.sell_side.notional_target,
            'sell_current': status.current_sell_notional,
            'buy_slots_target': f"L0:{status.buy_side.l0_slots}/L1:{status.buy_side.l1_slots}/L2:{status.buy_side.l2_slots}",
            'sell_slots_target': f"L0:{status.sell_side.l0_slots}/L1:{status.sell_side.l1_slots}/L2:{status.sell_side.l2_slots}",
            
            # DCR特有信息
            'convergence_info': convergence_info,
            'deployment_priority': self.get_deployment_priority(),
            'missing_slots': self.get_missing_slots(),
            'alert_history_count': len(self.alert_history),
            'last_update': self.last_update
        }

# Phase 7.3 DCR集成接口
def create_liquidity_envelope_manager(alpha_base: float = 0.15, alpha_k: float = 0.20,
                                     alpha_min: float = 0.10, alpha_max: float = 0.35) -> LiquidityEnvelopeManager:
    """创建DCR确定性收敛再平衡器实例"""
    return LiquidityEnvelopeManager(alpha_base=alpha_base, alpha_k=alpha_k, 
                                  alpha_min=alpha_min, alpha_max=alpha_max)

if __name__ == "__main__":
    # DCR测试代码
    manager = create_liquidity_envelope_manager(alpha_base=0.15, alpha_k=0.20, 
                                              alpha_min=0.10, alpha_max=0.35)
    
    # 模拟失衡状态测试
    test_equity = 355.4
    test_skew = -0.25  # 25% USDT偏多(严重失衡)
    
    print("Phase 7.3 DCR确定性收敛再平衡器测试:")
    print(f"测试参数: equity=${test_equity}, inventory_skew={test_skew}")
    
    # 测试动态alpha计算
    dynamic_alpha = manager.calculate_dynamic_alpha(test_equity, test_skew)
    print(f"\n动态Alpha: {dynamic_alpha:.3f} (失衡{abs(test_skew)*100:.1f}% → α提升至{dynamic_alpha*100:.1f}%)")
    
    # 测试容量估算
    maker_cap, taker_cap = manager.calculate_maker_taker_capacity(test_equity)
    print(f"容量估算: Maker=${maker_cap:.1f}, Taker=${taker_cap:.1f}")
    
    # 模拟状态更新
    manager.update_current_status(
        buy_notional=30.0,   # 买侧$30在册
        sell_notional=25.0,  # 卖侧$25在册  
        buy_l0_count=5,      # 买侧L0只有5个槽位（低于8的阈值）
        sell_l0_count=12,    # 卖侧L0有12个槽位
        equity=test_equity,  # 账户净值
        inventory_skew=test_skew  # 严重失衡
    )
    
    # 更新误差历史
    manager.update_error_history(test_skew, test_equity)
    
    # 测试再平衡计划
    rebalance_plan = manager.get_dcr_rebalance_plan(test_equity, test_skew)
    print(f"\nDCR再平衡计划: {rebalance_plan}")
    
    # 测试应急触发
    should_trigger, reason, intensity = manager.should_trigger_emergency_rebalance()
    print(f"\n应急再平衡: {should_trigger}, 原因: {reason}, 强度: {intensity}")
    
    # 完整统计信息
    stats = manager.get_envelope_stats()
    print(f"\nDCR包络统计: {stats}")