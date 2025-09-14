# -*- coding: utf-8 -*-
"""
TokenBucketRateLimiter - 令牌桶限流器
用于Phase 6 M1双边预算分水
"""

import time
import logging

logger = logging.getLogger(__name__)

class TokenBucketRateLimiter:
    """
    令牌桶限流器
    """
    def __init__(self, rate_limit, burst_limit, time_window=10.0):
        """
        Args:
            rate_limit: 速率限制（每秒）
            burst_limit: 突发限制
            time_window: 时间窗口（秒）
        """
        self.rate_limit = rate_limit
        self.burst_limit = burst_limit
        self.time_window = time_window
        self.budget = burst_limit
        self.tokens = burst_limit
        self.last_refill = time.time()
        self._count = 0
        self._window_start = time.time()
        
    def allow(self):
        """检查是否允许通过"""
        now = time.time()
        
        # 补充令牌
        elapsed = now - self.last_refill
        if elapsed > 0:
            refill = elapsed * self.rate_limit
            self.tokens = min(self.burst_limit, self.tokens + refill)
            self.last_refill = now
            
        # 重置窗口计数
        if now - self._window_start >= self.time_window:
            self._count = 0
            self._window_start = now
            
        # 检查是否有令牌
        if self.tokens >= 1 and self._count < self.budget:
            self.tokens -= 1
            self._count += 1
            return True
        return False
        
    def usage_pct(self):
        """返回使用率百分比"""
        return (self._count / max(1, self.budget)) * 100 if self.budget > 0 else 0
        
    def count(self):
        """返回当前计数"""
        return self._count
        
    def remaining(self):
        """返回剩余配额"""
        return max(0, self.budget - self._count)
    
    def get_stats(self):
        """返回限流器统计信息"""
        return {
            'count': self._count,
            'budget': self.budget,
            'burst': self.burst_limit,
            'tokens': self.tokens,
            'usage_pct': self.usage_pct()
        }
