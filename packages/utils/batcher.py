#!/usr/bin/env python3
"""
MicroBatcher - Phase 2 A2: 微批量处理器
20-50ms内同侧的下单/改价合并为一个flush事务
"""
from collections import deque
import time
import logging

logger = logging.getLogger(__name__)

class MicroBatcher:
    """微批量处理器，用于合并短时间内的多个操作"""
    
    def __init__(self, flush_ms=0.02, max_batch=5):
        """
        初始化微批量处理器
        
        Args:
            flush_ms: 刷新间隔（秒），默认20ms
            max_batch: 最大批量大小，默认5
        """
        self.q = deque()
        self.flush_ms = flush_ms
        self.max_batch = max_batch
        self.last_flush = time.time()
        self.stats = {
            'total_ops': 0,
            'total_batches': 0,
            'total_flushed': 0
        }
    
    def add(self, op):
        """
        添加操作到队列
        
        Args:
            op: 操作对象（字典或其他）
            
        Returns:
            list: 如果触发flush则返回操作列表，否则返回空列表
        """
        self.q.append(op)
        self.stats['total_ops'] += 1
        
        # 检查是否需要flush
        if len(self.q) >= self.max_batch or (time.time() - self.last_flush) >= self.flush_ms:
            return self.flush()
        return []
    
    def flush(self):
        """
        刷新队列，返回所有待处理操作
        
        Returns:
            list: 操作列表
        """
        ops = list(self.q)
        self.q.clear()
        self.last_flush = time.time()
        
        if ops:
            self.stats['total_batches'] += 1
            self.stats['total_flushed'] += len(ops)
            logger.debug(f"[MicroBatcher] Flushing {len(ops)} ops")
        
        return ops
    
    def pending_count(self):
        """返回待处理操作数量"""
        return len(self.q)
    
    def get_stats(self):
        """获取统计信息"""
        return {
            **self.stats,
            'pending': len(self.q),
            'avg_batch_size': self.stats['total_flushed'] / max(1, self.stats['total_batches'])
        }