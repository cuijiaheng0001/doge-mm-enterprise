#!/usr/bin/env python3
"""
AWG Pro - API Weight Governor Professional Version
集成Circuit Breaker状态机的增强版权重管理器
"""

import time
import threading
import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    NORMAL = "NORMAL"
    THROTTLED = "THROTTLED" 
    DEGRADED = "DEGRADED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    RECOVERING = "RECOVERING"


class AWGPro:
    """增强版AWG with Circuit Breaker"""
    
    def __init__(self, caps: Dict[str, int], config: Dict = None):
        """
        初始化AWG Pro
        
        Args:
            caps: 权重配额 {'1s': 100, '10s': 500, '1m': 2500}
            config: 额外配置
        """
        self.caps = caps
        self.win = {'1s': 1, '10s': 10, '1m': 60}
        self.usage = {k: deque() for k in self.win}
        self.lock = threading.Lock()
        
        # 端点成本配置
        self.costs = defaultdict(lambda: 1)
        self._init_default_costs()
        
        # Circuit Breaker配置 - Phase4紧急优化: 放宽AWG健康度检查阈值
        config = config or {}
        self.state = CircuitState.NORMAL
        self.consecutive_errors = 0
        self.last_state_change = time.time()
        self.error_threshold = config.get('error_threshold', 5)    # Phase4: 3→5 容错更多错误
        self.recovery_period = config.get('recovery_period', 30)   # Phase4: 60→30 更快恢复
        self.throttle_factor = config.get('throttle_factor', 0.8)  # Phase4: 0.7→0.8 更宽松限流
        self.degrade_factor = config.get('degrade_factor', 0.7)    # Phase4: 0.5→0.7 更宽松降级
        
        # 状态转换规则
        self.transitions = {
            CircuitState.NORMAL: [CircuitState.THROTTLED],
            CircuitState.THROTTLED: [CircuitState.DEGRADED, CircuitState.NORMAL],
            CircuitState.DEGRADED: [CircuitState.CIRCUIT_OPEN, CircuitState.THROTTLED],
            CircuitState.CIRCUIT_OPEN: [CircuitState.RECOVERING],
            CircuitState.RECOVERING: [CircuitState.NORMAL, CircuitState.THROTTLED]
        }
        
        # 统计
        self.stats = {
            'total_requests': 0,
            'approved_requests': 0,
            'denied_requests': 0,
            'circuit_trips': 0,
            'state_transitions': defaultdict(int)
        }
        
        # Phase 9: 预算切片统计
        self.slice_usage = {
            'new': deque(),      # (timestamp, cost)
            'cancel': deque(),   # (timestamp, cost) 
            'replace': deque(),  # (timestamp, cost)
            'reprice': deque()   # (timestamp, cost)
        }
        
        # Phase 9: API错误统计
        self.api_errors = {
            '1003': deque(),    # (timestamp, endpoint)
            '1015': deque(),    # (timestamp, endpoint)
            'other': deque()    # (timestamp, code, endpoint)
        }
        
        # Phase 9: 状态切换时间跟踪
        self.cool_down_start = None
        self.last_status_log = 0
        
        # Phase 9 B Fix: 通道子预算配置（对标顶级做市商）
        self.channel_budgets = {
            # mm_* 通道：常规做市
            'mm_new': {'10s': 8},      # 常规新单预算
            'mm_cancel': {'10s': 8},   # 常规撤单预算  
            'mm_replace': {'10s': 3},  # 常规换单预算
            
            # rb_* 通道：再平衡保障预算（独立分账）
            'rb_new': {'10s': 2},      # 再平衡新单保障
            'rb_cancel': {'10s': 2},   # 再平衡撤单保障
            'rb_replace': {'10s': 2},  # 再平衡换单保障
        }
        
        # Phase 9 B Fix: Taker-POV限额配置
        self.pov_config = {
            'max_notional_per_min': 20.0,  # USD/min
            'current_notional': 0.0,
            'notional_history': deque()  # (timestamp, notional)
        }
        
        # Phase 9 B Fix: 通道使用跟踪
        self.channel_usage = {
            channel: {window: deque() for window in self.win}
            for channel in self.channel_budgets.keys()
        }
        
    def _init_default_costs(self):
        """初始化默认端点成本"""
        defaults = {
            'new_order': 1,
            'cancelReplace': 1,
            'cancel': 1,
            'openOrders': 10,
            'account': 10,
            'depth': 2,
            'exchangeInfo': 20,
            'myTrades': 10,
            'klines': 1,
            'ticker24hr': 1
        }
        self.costs.update(defaults)
        
    def set_cost(self, endpoint: str, cost: int):
        """设置端点成本"""
        self.costs[endpoint] = cost
        
    def _get_effective_caps(self) -> Dict[str, int]:
        """获取当前状态下的有效配额"""
        if self.state == CircuitState.CIRCUIT_OPEN:
            return {k: 0 for k in self.caps}
        elif self.state == CircuitState.DEGRADED:
            return {k: int(v * self.degrade_factor) for k, v in self.caps.items()}
        elif self.state == CircuitState.THROTTLED:
            return {k: int(v * self.throttle_factor) for k, v in self.caps.items()}
        elif self.state == CircuitState.RECOVERING:
            # ✅ RECOVERING状态85%限流
            return {k: int(v * 0.85) for k, v in self.caps.items()}
        else:
            return self.caps.copy()
            
    def _available(self) -> Dict[str, int]:
        """计算当前可用权重"""
        now = time.time()
        effective_caps = self._get_effective_caps()
        
        avail = {}
        for window, duration in self.win.items():
            queue = self.usage[window]
            # 清理过期记录
            while queue and now - queue[0][0] > duration:
                queue.popleft()
                
            # 计算已使用权重
            used = sum(cost for _, cost in queue)
            cap = effective_caps.get(window, float('inf'))
            avail[window] = max(0, cap - used)
            
        return avail
    
    def _channel_available(self, channel: str) -> Dict[str, int]:
        """Phase 9 B Fix: 计算通道可用预算"""
        if channel not in self.channel_budgets:
            return {}
            
        now = time.time()
        avail = {}
        
        for window, budget in self.channel_budgets[channel].items():
            if window not in self.channel_usage[channel]:
                continue
                
            queue = self.channel_usage[channel][window]
            duration = self.win.get(window, 10)
            
            # 清理过期记录
            while queue and now - queue[0][0] > duration:
                queue.popleft()
                
            # 计算已使用预算
            used = sum(cost for _, cost in queue)
            avail[window] = max(0, budget - used)
            
        return avail
    
    def _check_pov_limit(self, notional: float) -> bool:
        """Phase 9 B Fix: 检查Taker-POV限额"""
        now = time.time()
        
        # 清理1分钟前的记录
        while (self.pov_config['notional_history'] and 
               now - self.pov_config['notional_history'][0][0] > 60):
            self.pov_config['notional_history'].popleft()
        
        # 计算当前1分钟内使用量
        current_usage = sum(n for _, n in self.pov_config['notional_history'])
        
        return current_usage + notional <= self.pov_config['max_notional_per_min']
    
    def acquire_with_channel(self, endpoint: str, channel: str = "mm_new", 
                           cost: Optional[int] = None, notional: float = 0.0) -> bool:
        """Phase 9 B Fix: 带通道检查的权重获取"""
        cost = cost or self.costs[endpoint]
        
        with self.lock:
            self.stats['total_requests'] += 1
            
            # 1. 全局权重检查（原有逻辑）
            avail = self._available()
            if not all(avail[window] >= cost for window in self.win):
                self.stats['denied_requests'] += 1
                return False
            
            # 2. 通道预算检查
            if channel in self.channel_budgets:
                channel_avail = self._channel_available(channel)
                for window, budget in self.channel_budgets[channel].items():
                    if channel_avail.get(window, 0) < cost:
                        self.stats['denied_requests'] += 1
                        logger.debug(f"[AWG Pro] 通道 {channel} {window} 预算不足: {channel_avail.get(window, 0)} < {cost}")
                        return False
            
            # 3. Taker-POV限额检查
            if notional > 0 and not self._check_pov_limit(notional):
                self.stats['denied_requests'] += 1
                logger.debug(f"[AWG Pro] POV限额不足: 当前+{notional} > {self.pov_config['max_notional_per_min']}")
                return False
            
            # 4. 执行分配
            now = time.time()
            
            # 分配全局权重
            for window in self.win:
                self.usage[window].append((now, cost))
            
            # 分配通道预算
            if channel in self.channel_budgets:
                for window in self.channel_budgets[channel].keys():
                    if window in self.channel_usage[channel]:
                        self.channel_usage[channel][window].append((now, cost))
            
            # 记录POV使用
            if notional > 0:
                self.pov_config['notional_history'].append((now, notional))
            
            self.stats['approved_requests'] += 1
            return True
        
    def acquire(self, endpoint: str, cost: Optional[int] = None) -> bool:
        """
        获取权重配额
        
        Args:
            endpoint: 端点名称
            cost: 权重成本，None则使用默认
            
        Returns:
            是否获得配额
        """
        cost = cost or self.costs[endpoint]
        
        with self.lock:
            self.stats['total_requests'] += 1
            
            # Circuit Open状态：拒绝高成本端点，允许低成本端点
            if self.state == CircuitState.CIRCUIT_OPEN:
                self._try_recover()
                if self.state == CircuitState.CIRCUIT_OPEN:
                    # Phase 5: 高成本端点直接拒绝，低成本端点允许通过
                    high_cost_endpoints = ['openOrders', 'account', 'exchangeInfo']
                    if endpoint in high_cost_endpoints or cost >= 5:
                        self.stats['denied_requests'] += 1
                        logger.debug(f"[AWG Pro] CIRCUIT_OPEN状态拒绝高成本端点: {endpoint} (cost={cost})")
                        return False
                    else:
                        logger.debug(f"[AWG Pro] CIRCUIT_OPEN状态允许低成本端点: {endpoint} (cost={cost})")
                    
            # 检查配额
            avail = self._available()
            
            # 需要所有窗口都有足够配额
            if all(avail[window] >= cost for window in self.win):
                now = time.time()
                for window in self.win:
                    self.usage[window].append((now, cost))
                    
                self.stats['approved_requests'] += 1
                
                # 成功时重置错误计数
                if self.consecutive_errors > 0:
                    self.consecutive_errors = 0
                    if self.state != CircuitState.NORMAL:
                        self._transition_state(CircuitState.NORMAL)
                        
                return True
            else:
                self.stats['denied_requests'] += 1
                return False
                
    def on_error(self, error_code: int, endpoint: str = "unknown"):
        """
        处理API错误
        
        Args:
            error_code: 错误代码
            endpoint: 端点名称
        """
        # 关键错误代码
        critical_errors = [-1003, 418, 429, -1021]  # IP banned, rate limit, etc
        
        if error_code in critical_errors:
            with self.lock:
                self.consecutive_errors += 1
                logger.warning(
                    f"[AWG Pro] API错误 {error_code} on {endpoint}, "
                    f"连续错误: {self.consecutive_errors}"
                )
                
                if self.consecutive_errors >= self.error_threshold:
                    if self.state == CircuitState.NORMAL:
                        self._transition_state(CircuitState.THROTTLED)
                    elif self.state == CircuitState.THROTTLED:
                        self._transition_state(CircuitState.DEGRADED)
                    elif self.state == CircuitState.DEGRADED:
                        self._transition_state(CircuitState.CIRCUIT_OPEN)
                        
    def _transition_state(self, new_state: CircuitState):
        """状态转换"""
        if new_state not in self.transitions[self.state]:
            logger.warning(f"[AWG Pro] 非法状态转换: {self.state} -> {new_state}")
            return
            
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.time()
        self.stats['state_transitions'][f"{old_state.value}_to_{new_state.value}"] += 1
        
        if new_state == CircuitState.CIRCUIT_OPEN:
            self.stats['circuit_trips'] += 1
            
        logger.info(f"[AWG Pro] 状态转换: {old_state.value} -> {new_state.value}")
        
    def _try_recover(self):
        """尝试恢复"""
        if self.state != CircuitState.CIRCUIT_OPEN:
            return
            
        if time.time() - self.last_state_change >= self.recovery_period:
            self._transition_state(CircuitState.RECOVERING)
            self.consecutive_errors = 0
            
    def force_state(self, state: CircuitState):
        """强制设置状态（用于测试）"""
        with self.lock:
            self.state = state
            self.last_state_change = time.time()
            
    def get_status(self) -> Dict:
        """获取状态信息"""
        with self.lock:
            avail = self._available()
            effective_caps = self._get_effective_caps()
            
            # 计算使用率
            usage_pct = {}
            for window in self.win:
                cap = effective_caps[window]
                used = cap - avail[window]
                usage_pct[window] = (used / cap * 100) if cap > 0 else 0
                
            return {
                'state': self.state.value,
                'consecutive_errors': self.consecutive_errors,
                'time_in_state': time.time() - self.last_state_change,
                'effective_caps': effective_caps,
                'available': avail,
                'usage_pct': usage_pct,
                'stats': self.stats.copy()
            }
            
    def get_usage_stats(self) -> str:
        """获取使用率统计（单行格式）"""
        status = self.get_status()
        usage_pct = status['usage_pct']
        
        return (
            f"state={status['state']} "
            f"1s={usage_pct['1s']:.0f}% "
            f"10s={usage_pct['10s']:.0f}% "
            f"1m={usage_pct['1m']:.0f}% "
            f"errors={status['consecutive_errors']}"
        )
        
    def reset_stats(self):
        """重置统计"""
        with self.lock:
            self.stats = {
                'total_requests': 0,
                'approved_requests': 0,
                'denied_requests': 0,
                'circuit_trips': 0,
                'state_transitions': defaultdict(int)
            }
    
    # ===== Phase 9: 预算切片增强方法 =====
    
    def track_slice_usage(self, slice_type: str, cost: int = 1):
        """跟踪预算切片使用情况"""
        if slice_type not in self.slice_usage:
            return
            
        now = time.time()
        with self.lock:
            self.slice_usage[slice_type].append((now, cost))
            # 清理10秒窗口外的数据
            while (self.slice_usage[slice_type] and 
                   now - self.slice_usage[slice_type][0][0] > 10):
                self.slice_usage[slice_type].popleft()
    
    def track_api_error(self, error_code: str, endpoint: str = "unknown"):
        """跟踪API错误"""
        now = time.time()
        with self.lock:
            if error_code in ['1003', '1015']:
                self.api_errors[error_code].append((now, endpoint))
                # 清理1分钟窗口外的数据
                while (self.api_errors[error_code] and 
                       now - self.api_errors[error_code][0][0] > 60):
                    self.api_errors[error_code].popleft()
            else:
                self.api_errors['other'].append((now, error_code, endpoint))
                # 清理1分钟窗口外的数据
                while (self.api_errors['other'] and 
                       now - self.api_errors['other'][0][0] > 60):
                    self.api_errors['other'].popleft()
    
    def get_slice_usage_stats(self) -> Dict[str, int]:
        """获取10秒窗口内的切片使用统计"""
        now = time.time()
        stats = {}
        
        with self.lock:
            for slice_type, queue in self.slice_usage.items():
                # 计算10秒内的使用量
                usage = sum(cost for ts, cost in queue if now - ts <= 10)
                stats[slice_type] = usage
                
        return stats
    
    def get_api_error_stats(self) -> Dict[str, int]:
        """获取1分钟窗口内的API错误统计"""
        now = time.time()
        stats = {}
        
        with self.lock:
            for error_code, queue in self.api_errors.items():
                if error_code in ['1003', '1015']:
                    count = len([1 for ts, ep in queue if now - ts <= 60])
                    stats[error_code] = count
                    
        return stats
    
    def get_cool_down_ms(self) -> int:
        """获取冷却时间（毫秒）"""
        if self.cool_down_start is None:
            return 0
        return max(0, int((time.time() - self.cool_down_start) * 1000))
    
    def set_cool_down(self):
        """设置冷却开始时间"""
        self.cool_down_start = time.time()
    
    def clear_cool_down(self):
        """清除冷却状态"""
        self.cool_down_start = None
    
    def _get_channel_usage_summary(self, channels: List[str]) -> Dict[str, int]:
        """Phase 9 B Fix: 获取通道使用摘要"""
        now = time.time()
        summary = {'new': 0, 'cancel': 0, 'replace': 0}
        
        # 映射通道到操作类型
        channel_mapping = {
            'mm_new': 'new', 'rb_new': 'new',
            'mm_cancel': 'cancel', 'rb_cancel': 'cancel', 
            'mm_replace': 'replace', 'rb_replace': 'replace'
        }
        
        for channel in channels:
            if channel not in self.channel_usage:
                continue
                
            # 使用10s窗口统计
            if '10s' in self.channel_usage[channel]:
                queue = self.channel_usage[channel]['10s']
                # 清理过期记录
                while queue and now - queue[0][0] > 10:
                    queue.popleft()
                    
                # 累加使用量
                op_type = channel_mapping.get(channel, 'new')
                usage_count = len(queue)
                summary[op_type] += usage_count
                
        return summary
    
    def log_awg_status(self):
        """Phase 9 B Fix: 按照验收标准输出AWG状态线"""
        # 限制日志频率：每5秒最多输出一次
        now = time.time()
        if now - self.last_status_log < 5:
            return
            
        self.last_status_log = now
        
        # 获取传统切片使用统计
        slice_stats = self.get_slice_usage_stats()
        error_stats = self.get_api_error_stats()
        cool_down_ms = self.get_cool_down_ms()
        
        # Phase 9 B Fix: 获取通道使用统计
        mm_usage = self._get_channel_usage_summary(['mm_new', 'mm_cancel', 'mm_replace'])
        rb_usage = self._get_channel_usage_summary(['rb_new', 'rb_cancel', 'rb_replace'])
        
        # 符合验收标准的日志格式
        logger.info(
            f"[AWG] state={self.state.value} "
            f"slice usage mm(new/cancel/replace)={mm_usage['new']}/{mm_usage['cancel']}/{mm_usage['replace']} "
            f"rb(new/cancel/replace)={rb_usage['new']}/{rb_usage['cancel']}/{rb_usage['replace']} "
            f"errors(1003/1015)={error_stats.get('1003', 0)}/{error_stats.get('1015', 0)} "
            f"cool_down_ms={cool_down_ms}"
        )
    
    def enhance_acquire_with_tracking(self, endpoint: str, cost: Optional[int] = None) -> bool:
        """增强版acquire，包含切片跟踪"""
        # 映射端点到切片类型
        slice_mapping = {
            'new_order': 'new',
            'cancel': 'cancel', 
            'cancelReplace': 'replace',
            'reprice': 'reprice'
        }
        
        # 执行原有的acquire逻辑
        result = self.acquire(endpoint, cost)
        
        # 如果成功，跟踪切片使用
        if result and endpoint in slice_mapping:
            actual_cost = cost or self.costs[endpoint]
            self.track_slice_usage(slice_mapping[endpoint], actual_cost)
            
        return result


# 全局AWG实例
_awg_instance = None
_awg_lock = threading.Lock()


def get_awg_pro(caps: Dict[str,int]=None, config: Dict=None) -> AWGPro:
    """获取AWG Pro实例，从环境变量读取配置"""
    import os
    global _awg_instance
    with _awg_lock:
        if _awg_instance is None:
            # 从环境变量读取配额
            caps = caps or {
                '1s': int(os.getenv('AWG_CAP_1S', '100')),
                '10s': int(os.getenv('AWG_CAP_10S', '500')),
                '1m': int(os.getenv('AWG_CAP_1M', '2500')),
            }
            config = config or {
                'error_threshold': int(os.getenv('AWG_ERROR_THRESHOLD', '3')),
                'recovery_period': int(os.getenv('AWG_RECOVERY_PERIOD', '60')),
                'throttle_factor': float(os.getenv('AWG_THROTTLE_FACTOR', '0.7')),
                'degrade_factor': float(os.getenv('AWG_DEGRADE_FACTOR', '0.5')),
            }
            _awg_instance = AWGPro(caps, config)
            
            # 设置端点成本
            endpoint_costs = {
                'new_order': int(os.getenv('AWG_COST_new_order', '1')),
                'cancel': int(os.getenv('AWG_COST_cancel', '1')),
                'cancelReplace': int(os.getenv('AWG_COST_cancelReplace', '1')),
                'openOrders': int(os.getenv('AWG_COST_openOrders', '10')),  # openOrders 高成本
                'account': int(os.getenv('AWG_COST_account', '10')),        # account 高成本
            }
            for ep, cost in endpoint_costs.items():
                _awg_instance.set_cost(ep, cost)
        return _awg_instance


def reset_awg_pro():
    """重置全局AWG实例（用于测试）"""
    global _awg_instance
    with _awg_lock:
        _awg_instance = None


if __name__ == "__main__":
    # 简单测试
    awg = AWGPro({'1s': 10, '10s': 50, '1m': 200})
    
    # 正常获取
    for i in range(5):
        result = awg.acquire('test', 2)
        print(f"请求 {i+1}: {result}")
        
    # 模拟错误
    awg.on_error(-1003, 'test_endpoint')
    awg.on_error(-1003, 'test_endpoint') 
    awg.on_error(-1003, 'test_endpoint')
    
    print(f"状态: {awg.get_status()}")