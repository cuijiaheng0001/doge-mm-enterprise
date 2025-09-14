"""
Hedge Governor - 对冲预算管理器
独立的预算管理，不占用现货额度
"""

import logging
import time
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from collections import deque
from enum import Enum
import math

logger = logging.getLogger(__name__)


class BudgetType(Enum):
    """预算类型"""
    HEDGE_FILL = "hedge_fill"
    HEDGE_REPRICE = "hedge_reprice"
    HEDGE_CANCEL = "hedge_cancel"


class GateLevel(Enum):
    """闸门级别"""
    SOFT = "soft"  # 软闸：只记录
    MEDIUM = "medium"  # 中等：警告
    HARD = "hard"  # 硬闸：限制
    EMERGENCY = "emergency"  # 紧急：停止


@dataclass
class BudgetStatus:
    """预算状态"""
    budget_type: BudgetType
    current_usage: int
    budget_limit: int
    burst_limit: int
    usage_pct: float
    gate_level: GateLevel
    reset_ts: float
    
    @property
    def is_available(self) -> bool:
        """是否可用"""
        return self.current_usage < self.budget_limit
    
    @property
    def remaining(self) -> int:
        """剩余额度"""
        return max(0, self.budget_limit - self.current_usage)


class HedgeGovernor:
    """
    对冲预算管理器 - FAHE风控组件
    独立管理对冲订单的API预算
    """
    
    def __init__(self,
                 fill_budget: int = 12,
                 reprice_budget: int = 12,
                 cancel_budget: int = 40,
                 window_seconds: int = 10,
                 target_usage_pct: float = 0.07,
                 safe_usage_pct: float = 0.15):
        """
        初始化对冲预算管理器
        
        Args:
            fill_budget: 成交预算（每窗口）
            reprice_budget: 改价预算（每窗口）
            cancel_budget: 撤单预算（每窗口）
            window_seconds: 窗口大小（秒）
            target_usage_pct: 目标使用率
            safe_usage_pct: 安全使用率上限
        """
        # 基础预算
        self.base_budgets = {
            BudgetType.HEDGE_FILL: fill_budget,
            BudgetType.HEDGE_REPRICE: reprice_budget,
            BudgetType.HEDGE_CANCEL: cancel_budget
        }
        
        # 当前预算（可动态调整）
        self.current_budgets = self.base_budgets.copy()
        
        # Burst预算（等于基础预算）
        self.burst_budgets = self.base_budgets.copy()
        
        # 窗口参数
        self.window_seconds = window_seconds
        
        # PID控制参数
        self.target_usage_pct = target_usage_pct
        self.safe_usage_pct = safe_usage_pct
        self.kp = 0.5  # 比例系数
        self.ki = 0.1  # 积分系数
        self.kd = 0.2  # 微分系数
        
        # PID状态
        self.error_integral = 0.0
        self.last_error = 0.0
        
        # 使用记录
        self.usage_windows = {
            BudgetType.HEDGE_FILL: deque(maxlen=100),
            BudgetType.HEDGE_REPRICE: deque(maxlen=100),
            BudgetType.HEDGE_CANCEL: deque(maxlen=100)
        }
        
        # 当前窗口使用量
        self.current_window_usage = {
            BudgetType.HEDGE_FILL: 0,
            BudgetType.HEDGE_REPRICE: 0,
            BudgetType.HEDGE_CANCEL: 0
        }
        
        # 窗口开始时间
        self.window_start_ts = time.time()
        
        # 租约管理
        self.active_leases = {}  # lease_id -> (budget_type, tokens, ts)
        self.lease_counter = 0
        
        # 统计信息
        self.stats = {
            'total_requests': 0,
            'approved_requests': 0,
            'rejected_requests': 0,
            'lease_rollbacks': 0,
            'gate_triggers': {
                GateLevel.SOFT: 0,
                GateLevel.MEDIUM: 0,
                GateLevel.HARD: 0,
                GateLevel.EMERGENCY: 0
            }
        }
        
        # 动态黑名单（临时限制）
        self.blacklist = {}  # budget_type -> expire_ts
        
        logger.info(f"[HedgeGovernor] 初始化完成: fill={fill_budget}, reprice={reprice_budget}, "
                   f"cancel={cancel_budget}, window={window_seconds}s")
    
    def try_acquire(self, budget_type: BudgetType, tokens: int = 1) -> Optional[str]:
        """
        尝试获取预算租约
        
        Args:
            budget_type: 预算类型
            tokens: 请求的令牌数
        
        Returns:
            租约ID（成功）或None（失败）
        """
        self.stats['total_requests'] += 1
        
        # 检查黑名单
        if self._is_blacklisted(budget_type):
            logger.warning(f"[HedgeGovernor] {budget_type.value}在黑名单中")
            self.stats['rejected_requests'] += 1
            return None
        
        # 更新窗口
        self._update_window()
        
        # 检查可用额度
        current_usage = self.current_window_usage[budget_type]
        budget_limit = self.current_budgets[budget_type]
        burst_limit = self.burst_budgets[budget_type]
        
        # 检查burst
        if current_usage < budget_limit:
            # 正常额度
            available = budget_limit - current_usage
        elif current_usage < burst_limit:
            # Burst额度
            available = burst_limit - current_usage
            logger.debug(f"[HedgeGovernor] 使用burst额度: {budget_type.value}")
        else:
            # 无额度
            available = 0
        
        if tokens > available:
            # 额度不足
            self.stats['rejected_requests'] += 1
            
            # 检查闸门级别
            gate_level = self._check_gate_level(budget_type, current_usage, budget_limit)
            self._handle_gate_trigger(gate_level, budget_type)
            
            logger.warning(f"[HedgeGovernor] 额度不足: {budget_type.value} "
                          f"requested={tokens}, available={available}")
            return None
        
        # 分配租约
        lease_id = f"lease_{self.lease_counter}"
        self.lease_counter += 1
        
        self.active_leases[lease_id] = (budget_type, tokens, time.time())
        self.current_window_usage[budget_type] += tokens
        
        self.stats['approved_requests'] += 1
        
        logger.debug(f"[HedgeGovernor] 租约批准: {lease_id} {budget_type.value} tokens={tokens}")
        
        return lease_id
    
    def commit_lease(self, lease_id: str) -> bool:
        """
        提交租约（确认使用）
        
        Args:
            lease_id: 租约ID
        
        Returns:
            是否成功
        """
        if lease_id not in self.active_leases:
            logger.warning(f"[HedgeGovernor] 租约不存在: {lease_id}")
            return False
        
        # 移除租约（已使用）
        del self.active_leases[lease_id]
        
        logger.debug(f"[HedgeGovernor] 租约提交: {lease_id}")
        return True
    
    def rollback_lease(self, lease_id: str) -> bool:
        """
        回滚租约（释放额度）
        
        Args:
            lease_id: 租约ID
        
        Returns:
            是否成功
        """
        if lease_id not in self.active_leases:
            logger.warning(f"[HedgeGovernor] 租约不存在: {lease_id}")
            return False
        
        budget_type, tokens, _ = self.active_leases[lease_id]
        
        # 释放额度
        self.current_window_usage[budget_type] -= tokens
        del self.active_leases[lease_id]
        
        self.stats['lease_rollbacks'] += 1
        
        logger.info(f"[HedgeGovernor] 租约回滚: {lease_id} {budget_type.value} tokens={tokens}")
        return True
    
    def set_dynamic_budgets(self, fill: Optional[int] = None, 
                          reprice: Optional[int] = None,
                          cancel: Optional[int] = None) -> None:
        """
        设置动态预算
        
        Args:
            fill: 成交预算
            reprice: 改价预算
            cancel: 撤单预算
        """
        if fill is not None:
            self.current_budgets[BudgetType.HEDGE_FILL] = fill
            self.burst_budgets[BudgetType.HEDGE_FILL] = fill
        
        if reprice is not None:
            self.current_budgets[BudgetType.HEDGE_REPRICE] = reprice
            self.burst_budgets[BudgetType.HEDGE_REPRICE] = reprice
        
        if cancel is not None:
            self.current_budgets[BudgetType.HEDGE_CANCEL] = cancel
            self.burst_budgets[BudgetType.HEDGE_CANCEL] = cancel
        
        logger.info(f"[HedgeGovernor] 动态预算更新: fill={fill}, reprice={reprice}, cancel={cancel}")
    
    def apply_pid_control(self) -> None:
        """
        应用PID控制调整预算
        """
        # 计算当前使用率
        total_usage = sum(self.current_window_usage.values())
        total_budget = sum(self.current_budgets.values())
        current_usage_pct = total_usage / total_budget if total_budget > 0 else 0
        
        # 计算误差
        error = self.target_usage_pct - current_usage_pct
        
        # PID计算
        p_term = self.kp * error
        self.error_integral += error
        i_term = self.ki * self.error_integral
        d_term = self.kd * (error - self.last_error)
        
        # 总调整量
        adjustment = p_term + i_term + d_term
        
        # 应用调整
        for budget_type in BudgetType:
            base = self.base_budgets[budget_type]
            new_budget = int(base * (1 + adjustment))
            
            # 限制范围[0.5x, 2x]
            new_budget = max(int(base * 0.5), min(int(base * 2), new_budget))
            
            self.current_budgets[budget_type] = new_budget
        
        # 更新状态
        self.last_error = error
        
        logger.debug(f"[HedgeGovernor] PID调整: usage={current_usage_pct:.3f}, "
                    f"error={error:.3f}, adjustment={adjustment:.3f}")
    
    def _update_window(self) -> None:
        """
        更新时间窗口
        """
        now = time.time()
        
        # 检查是否需要重置窗口
        if now - self.window_start_ts >= self.window_seconds:
            # 记录历史
            for budget_type in BudgetType:
                self.usage_windows[budget_type].append(self.current_window_usage[budget_type])
            
            # 应用PID控制
            self.apply_pid_control()
            
            # 重置窗口
            self.current_window_usage = {
                BudgetType.HEDGE_FILL: 0,
                BudgetType.HEDGE_REPRICE: 0,
                BudgetType.HEDGE_CANCEL: 0
            }
            self.window_start_ts = now
            
            logger.debug("[HedgeGovernor] 窗口重置")
    
    def _check_gate_level(self, budget_type: BudgetType, usage: int, limit: int) -> GateLevel:
        """
        检查闸门级别
        
        Args:
            budget_type: 预算类型
            usage: 当前使用量
            limit: 预算限制
        
        Returns:
            闸门级别
        """
        usage_pct = usage / limit if limit > 0 else 1.0
        
        if usage_pct < 0.08:
            return GateLevel.SOFT
        elif usage_pct < 0.12:
            return GateLevel.MEDIUM
        elif usage_pct < self.safe_usage_pct:
            return GateLevel.HARD
        else:
            return GateLevel.EMERGENCY
    
    def _handle_gate_trigger(self, gate_level: GateLevel, budget_type: BudgetType) -> None:
        """
        处理闸门触发
        
        Args:
            gate_level: 闸门级别
            budget_type: 预算类型
        """
        self.stats['gate_triggers'][gate_level] += 1
        
        if gate_level == GateLevel.SOFT:
            logger.debug(f"[HedgeGovernor] 软闸触发: {budget_type.value}")
        elif gate_level == GateLevel.MEDIUM:
            logger.info(f"[HedgeGovernor] 中等闸门触发: {budget_type.value}")
        elif gate_level == GateLevel.HARD:
            logger.warning(f"[HedgeGovernor] 硬闸触发: {budget_type.value}")
            # 临时限制
            self.blacklist[budget_type] = time.time() + 1.0  # 1秒黑名单
        else:  # EMERGENCY
            logger.error(f"[HedgeGovernor] 紧急闸门触发: {budget_type.value}")
            # 长时间限制
            self.blacklist[budget_type] = time.time() + 10.0  # 10秒黑名单
    
    def _is_blacklisted(self, budget_type: BudgetType) -> bool:
        """
        检查是否在黑名单中
        
        Args:
            budget_type: 预算类型
        
        Returns:
            是否在黑名单
        """
        if budget_type not in self.blacklist:
            return False
        
        expire_ts = self.blacklist[budget_type]
        
        if time.time() >= expire_ts:
            # 过期，移除
            del self.blacklist[budget_type]
            return False
        
        return True
    
    def get_status(self, budget_type: BudgetType) -> BudgetStatus:
        """
        获取预算状态
        
        Args:
            budget_type: 预算类型
        
        Returns:
            预算状态
        """
        self._update_window()
        
        current_usage = self.current_window_usage[budget_type]
        budget_limit = self.current_budgets[budget_type]
        burst_limit = self.burst_budgets[budget_type]
        usage_pct = current_usage / budget_limit if budget_limit > 0 else 0
        
        gate_level = self._check_gate_level(budget_type, current_usage, budget_limit)
        
        return BudgetStatus(
            budget_type=budget_type,
            current_usage=current_usage,
            budget_limit=budget_limit,
            burst_limit=burst_limit,
            usage_pct=usage_pct,
            gate_level=gate_level,
            reset_ts=self.window_start_ts + self.window_seconds
        )
    
    def get_all_status(self) -> Dict[BudgetType, BudgetStatus]:
        """
        获取所有预算状态
        
        Returns:
            预算状态字典
        """
        return {bt: self.get_status(bt) for bt in BudgetType}
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        total_usage = sum(self.current_window_usage.values())
        total_budget = sum(self.current_budgets.values())
        
        return {
            **self.stats,
            'current_usage_pct': total_usage / total_budget if total_budget > 0 else 0,
            'approval_rate': self.stats['approved_requests'] / self.stats['total_requests']
                           if self.stats['total_requests'] > 0 else 0,
            'active_leases': len(self.active_leases),
            'blacklisted_types': list(self.blacklist.keys()),
            'current_budgets': {bt.value: self.current_budgets[bt] for bt in BudgetType}
        }
    
    def reset(self) -> None:
        """
        重置管理器
        """
        self.current_budgets = self.base_budgets.copy()
        self.burst_budgets = self.base_budgets.copy()
        self.current_window_usage = {bt: 0 for bt in BudgetType}
        self.window_start_ts = time.time()
        self.active_leases.clear()
        self.blacklist.clear()
        self.error_integral = 0.0
        self.last_error = 0.0
        
        logger.info("[HedgeGovernor] 管理器已重置")