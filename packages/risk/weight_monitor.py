#!/usr/bin/env python3
"""
REST API权重监控器 - 保险丝机制
监控API权重使用，超限时自动进入冷却模式
"""
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class WeightMonitor:
    """API权重监控器"""
    
    def __init__(self, danger_threshold=900, cooldown_seconds=60):
        """
        Args:
            danger_threshold: 危险阈值(默认900/1200)
            cooldown_seconds: 冷却时间(秒)
        """
        self.danger_threshold = danger_threshold
        self.cooldown_seconds = cooldown_seconds
        
        # 状态
        self.last_weight = 0
        self.max_weight_seen = 0
        self.cooldown_until = 0
        self.trip_count = 0  # 熔断次数
        
        # 统计
        self.stats = {
            "total_requests": 0,
            "total_weight_used": 0,
            "max_weight_1m": 0,
            "max_delta": 0,
            "trip_events": []
        }
        
    def is_in_cooldown(self):
        """是否在冷却中"""
        return time.time() < self.cooldown_until
        
    def check_response_headers(self, headers):
        """检查响应头中的权重信息"""
        if not headers:
            return
            
        # 提取权重信息
        used_weight_str = headers.get("X-MBX-USED-WEIGHT-1M", "")
        if not used_weight_str or not used_weight_str.isdigit():
            return
            
        current_weight = int(used_weight_str)
        delta = current_weight - self.last_weight if self.last_weight > 0 else 0
        
        # 更新统计
        self.stats["total_requests"] += 1
        if delta > 0:
            self.stats["total_weight_used"] += delta
        self.stats["max_weight_1m"] = max(self.stats["max_weight_1m"], current_weight)
        self.stats["max_delta"] = max(self.stats["max_delta"], delta)
        
        # 日志记录
        if delta > 10:  # 单次消耗超过10权重时记录
            logger.warning(f"⚠️ API权重: 当前{current_weight}/1200 (+{delta})")
        
        # 检查是否需要熔断
        if current_weight >= self.danger_threshold:
            self.trigger_cooldown(current_weight)
            
        self.last_weight = current_weight
        self.max_weight_seen = max(self.max_weight_seen, current_weight)
        
    def trigger_cooldown(self, weight):
        """触发冷却模式"""
        self.cooldown_until = time.time() + self.cooldown_seconds
        self.trip_count += 1
        
        # 记录熔断事件
        trip_event = {
            "timestamp": time.time(),
            "weight": weight,
            "threshold": self.danger_threshold,
            "cooldown_s": self.cooldown_seconds
        }
        self.stats["trip_events"].append(trip_event)
        
        logger.error(f"🔴 API权重熔断! 权重{weight}≥{self.danger_threshold}, "
                    f"进入{self.cooldown_seconds}秒冷却模式")
        
    def should_allow_request(self, critical=False):
        """是否允许发起请求
        
        Args:
            critical: 是否是关键请求(如撤单)
        """
        if self.is_in_cooldown():
            if critical:
                # 关键请求在冷却期也允许，但记录警告
                logger.warning("⚠️ 冷却期执行关键请求")
                return True
            else:
                remaining = int(self.cooldown_until - time.time())
                logger.debug(f"🧊 冷却中，剩余{remaining}秒")
                return False
        return True
        
    def get_status(self):
        """获取当前状态"""
        return {
            "current_weight": self.last_weight,
            "max_weight": self.max_weight_seen,
            "in_cooldown": self.is_in_cooldown(),
            "trip_count": self.trip_count,
            "stats": self.stats
        }
        
    def reset_stats(self):
        """重置统计(每小时调用)"""
        self.stats["total_requests"] = 0
        self.stats["total_weight_used"] = 0
        # 保留max和trip_events作为历史记录