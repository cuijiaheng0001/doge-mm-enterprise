#!/usr/bin/env python3
"""
API Weight Governor (AWG) - 全局API权重管理器
防止API请求权重超限导致IP被封
"""

import time
import threading
from collections import defaultdict, deque
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class AWG:
    """API权重管理器 - 使用令牌桶算法控制API请求速率"""
    
    def __init__(self, caps: Dict[str, int]):
        """
        初始化AWG
        
        Args:
            caps: 窗口容量配置，如 {'1s': 1200, '10s': 6000, '1m': 12000}
        """
        self.caps = caps
        self.win = {'1s': 1, '10s': 10, '1m': 60}  # 窗口时长（秒）
        self.usage = {k: deque() for k in self.win}  # 每个窗口的使用记录
        self.lock = threading.Lock()
        self.costs = defaultdict(lambda: 1)  # 端点默认成本
        self.degraded = False  # 降级模式标志
        self.degraded_until = 0  # 降级持续到的时间戳
        
        # 预设端点成本
        self._init_costs()
        
    def _init_costs(self):
        """初始化各端点的权重成本"""
        self.costs.update({
            'exchangeInfo': 20,
            'account': 10,
            'openOrders': 10,
            'depth': 2,
            'new_order': 1,
            'cancelReplace': 1,
            'cancel': 1,
            'myTrades': 10,
            'allOrders': 10,
        })
        
    def set_cost(self, endpoint: str, cost: int):
        """设置端点的权重成本"""
        self.costs[endpoint] = cost
        
    def enter_degraded(self, duration_s: int = 60):
        """进入降级模式"""
        self.degraded = True
        self.degraded_until = time.time() + duration_s
        logger.warning(f"[AWG] 进入降级模式，持续{duration_s}秒")
        
    def exit_degraded(self):
        """退出降级模式"""
        if time.time() > self.degraded_until:
            self.degraded = False
            logger.info("[AWG] 退出降级模式")
            
    def _available(self) -> Dict[str, int]:
        """计算各窗口的可用权重"""
        now = time.time()
        avail = {}
        
        for k, w in self.win.items():
            q = self.usage[k]
            # 清理过期记录
            while q and now - q[0][0] > w:
                q.popleft()
            # 计算已用权重
            used = sum(c for _, c in q)
            cap = self.caps.get(k, float('inf'))
            avail[k] = max(0, cap - used)
            
        return avail
        
    def acquire(self, endpoint: str, cost: Optional[int] = None) -> bool:
        """
        尝试获取权重配额
        
        Args:
            endpoint: API端点名称
            cost: 权重成本（None则使用预设值）
            
        Returns:
            bool: 是否成功获取配额
        """
        # 降级模式检查
        if self.degraded:
            self.exit_degraded()
            if self.degraded:
                # 降级模式下只允许关键写操作
                if endpoint not in ['new_order', 'cancel', 'cancelReplace']:
                    logger.debug(f"[AWG] 降级模式拒绝: {endpoint}")
                    return False
                    
        c = cost or self.costs[endpoint]
        
        with self.lock:
            avail = self._available()
            
            # 检查所有窗口是否都有足够配额
            if all(avail[k] >= c for k in self.win):
                now = time.time()
                # 记录使用
                for k in self.win:
                    self.usage[k].append((now, c))
                return True
                
            # 配额不足，记录日志
            logger.debug(f"[AWG] 配额不足: {endpoint} cost={c} avail={avail}")
            return False
            
    def get_usage_stats(self) -> Dict[str, float]:
        """获取使用率统计"""
        with self.lock:
            avail = self._available()
            stats = {}
            for k in self.win:
                cap = self.caps.get(k, float('inf'))
                if cap != float('inf'):
                    used = cap - avail[k]
                    stats[f'awg_{k}_pct'] = (used / cap) * 100
            return stats
            
    def handle_rate_limit_error(self, error_code: int, error_msg: str):
        """处理限速错误"""
        if error_code in [-1003, 418, 429]:
            # 触发限速，进入降级模式
            logger.error(f"[AWG] 触发限速: code={error_code} msg={error_msg}")
            self.enter_degraded(120)  # 降级2分钟


# 全局AWG实例
_awg_instance = None


def get_awg() -> AWG:
    """获取全局AWG实例"""
    global _awg_instance
    if _awg_instance is None:
        import os
        caps = {
            '1s': int(os.getenv('AWG_CAP_1S', '120')),
            '10s': int(os.getenv('AWG_CAP_10S', '600')),
            '1m': int(os.getenv('AWG_CAP_1M', '3000')),
        }
        _awg_instance = AWG(caps)
        logger.info(f"[AWG] 初始化完成: caps={caps}")
    return _awg_instance