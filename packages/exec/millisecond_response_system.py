"""
Millisecond Response System - 毫秒响应系统
fill→repost事件优先级（毫秒级）+ TTL撤换

实现Jane Street/Citadel级别毫秒响应，包含：
- Fill→Repost Priority Queue: 成交优先响应（目标p99 ≤ 50ms）
- TTL Manager: 动态TTL管理与过期撤换
- Micro-batch Rhythm: 20-50ms微批节奏
- Event Priority System: fills > cancels > replaces > creates
"""

import time
import asyncio
import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import statistics
from collections import deque

logger = logging.getLogger(__name__)


class EventPriority(Enum):
    FILL = 1         # 最高优先级：成交响应
    CANCEL = 2       # 次高优先级：撤单
    REPLACE = 3      # 中等优先级：改单
    CREATE = 4       # 最低优先级：新建订单


class OrderLevel(Enum):
    L0 = "L0"        # 最优层级
    L1 = "L1"        # 次优层级  
    L2 = "L2"        # 深度层级


@dataclass
class PriorityEvent:
    """优先级事件"""
    priority: EventPriority
    event_type: str
    order_id: str
    data: Dict[str, Any]
    timestamp: float
    callback: Optional[Callable] = None


@dataclass
class TTLConfig:
    """TTL配置"""
    l0_min: float = 1.8      # L0最小TTL (秒)
    l0_max: float = 2.5      # L0最大TTL (秒)
    l1_ttl: float = 8.0      # L1 TTL (秒)
    l2_ttl: float = 20.0     # L2 TTL (秒)
    jitter_min: float = 0.5  # 抖动最小值 (秒)
    jitter_max: float = 1.0  # 抖动最大值 (秒)


@dataclass
class ResponseMetrics:
    """响应指标"""
    fill_to_repost_times: List[float]     # Fill到Repost延迟
    event_queue_sizes: List[int]          # 事件队列大小
    ttl_violations: int                   # TTL违规次数
    priority_inversions: int              # 优先级倒置次数
    micro_batch_intervals: List[float]    # 微批间隔


class MillisecondResponseSystem:
    """毫秒响应系统核心"""
    
    def __init__(self):
        # 优先级队列 (按优先级排序)
        self.priority_queue: List[PriorityEvent] = []
        
        # TTL配置与跟踪
        self.ttl_config = TTLConfig()
        self.active_orders: Dict[str, Dict] = {}  # order_id -> {ttl, created_time, level}
        
        # 微批节奏控制
        self.micro_batch_interval = 0.035  # 35ms默认间隔
        self.last_batch_time = 0.0
        
        # 性能指标
        self.metrics = ResponseMetrics(
            fill_to_repost_times=[],
            event_queue_sizes=[],
            ttl_violations=0,
            priority_inversions=0,
            micro_batch_intervals=[]
        )
        
        # 系统状态
        self.running = False
        self.fill_events_count = 0
        self.repost_success_count = 0
        
        logger.info("[MillisecondResponse] 毫秒响应系统初始化完成")
    
    def add_priority_event(self, event: PriorityEvent):
        """添加优先级事件到队列"""
        # 按优先级插入（维持排序）
        inserted = False
        for i, existing_event in enumerate(self.priority_queue):
            if event.priority.value < existing_event.priority.value:
                self.priority_queue.insert(i, event)
                inserted = True
                break
        
        if not inserted:
            self.priority_queue.append(event)
        
        # 记录队列大小指标
        self.metrics.event_queue_sizes.append(len(self.priority_queue))
        
        # 检测优先级倒置
        if len(self.priority_queue) > 1 and event.priority != EventPriority.FILL:
            if any(e.priority == EventPriority.FILL for e in self.priority_queue[1:]):
                self.metrics.priority_inversions += 1
                logger.warning(
                    "[MillisecondResponse] 检测到优先级倒置: %s在FILL前执行",
                    event.event_type
                )
    
    def register_fill_event(self, order_id: str, fill_price: Decimal, 
                           fill_qty: Decimal, side: str, callback: Callable):
        """注册成交事件 - 最高优先级"""
        fill_event = PriorityEvent(
            priority=EventPriority.FILL,
            event_type="FILL",
            order_id=order_id,
            data={
                'price': fill_price,
                'qty': fill_qty,
                'side': side,
                'timestamp': time.time()
            },
            timestamp=time.time(),
            callback=callback
        )
        
        self.add_priority_event(fill_event)
        self.fill_events_count += 1
        
        logger.debug(
            "[MillisecondResponse] 🔥 FILL事件注册: %s %s@%s (优先级=1)",
            order_id, fill_qty, fill_price
        )
    
    def register_cancel_event(self, order_id: str, callback: Callable):
        """注册撤单事件"""
        cancel_event = PriorityEvent(
            priority=EventPriority.CANCEL,
            event_type="CANCEL",
            order_id=order_id,
            data={'timestamp': time.time()},
            timestamp=time.time(),
            callback=callback
        )
        
        self.add_priority_event(cancel_event)
        
        logger.debug(
            "[MillisecondResponse] 🚫 CANCEL事件注册: %s (优先级=2)",
            order_id
        )
    
    def register_replace_event(self, order_id: str, new_price: Decimal, 
                             new_qty: Decimal, callback: Callable):
        """注册改单事件"""
        replace_event = PriorityEvent(
            priority=EventPriority.REPLACE,
            event_type="REPLACE",
            order_id=order_id,
            data={
                'new_price': new_price,
                'new_qty': new_qty,
                'timestamp': time.time()
            },
            timestamp=time.time(),
            callback=callback
        )
        
        self.add_priority_event(replace_event)
        
        logger.debug(
            "[MillisecondResponse] 🔄 REPLACE事件注册: %s %s@%s (优先级=3)",
            order_id, new_qty, new_price
        )
    
    def register_create_event(self, order_id: str, side: str, 
                            qty: Decimal, price: Decimal, level: OrderLevel, callback: Callable):
        """注册创建订单事件"""
        create_event = PriorityEvent(
            priority=EventPriority.CREATE,
            event_type="CREATE",
            order_id=order_id,
            data={
                'side': side,
                'qty': qty,
                'price': price,
                'level': level,
                'timestamp': time.time()
            },
            timestamp=time.time(),
            callback=callback
        )
        
        self.add_priority_event(create_event)
        
        # 注册到TTL跟踪
        ttl = self._calculate_ttl(level)
        self.active_orders[order_id] = {
            'ttl': ttl,
            'created_time': time.time(),
            'level': level
        }
        
        logger.debug(
            "[MillisecondResponse] 📝 CREATE事件注册: %s %s %s@%s TTL=%.1fs (优先级=4)",
            order_id, side, qty, price, ttl
        )
    
    def _calculate_ttl(self, level: OrderLevel) -> float:
        """计算动态TTL"""
        import random
        
        if level == OrderLevel.L0:
            # L0: 1.8-2.5s + 抖动
            base_ttl = random.uniform(self.ttl_config.l0_min, self.ttl_config.l0_max)
            jitter = random.uniform(self.ttl_config.jitter_min, self.ttl_config.jitter_max)
            return base_ttl + jitter
        elif level == OrderLevel.L1:
            # L1: 8s + 抖动
            jitter = random.uniform(self.ttl_config.jitter_min, self.ttl_config.jitter_max)
            return self.ttl_config.l1_ttl + jitter
        else:  # L2
            # L2: 20s + 抖动
            jitter = random.uniform(self.ttl_config.jitter_min, self.ttl_config.jitter_max)
            return self.ttl_config.l2_ttl + jitter
    
    async def process_priority_queue(self):
        """处理优先级队列 - 核心执行循环"""
        while self.running:
            current_time = time.time()
            
            # 1. 检查微批间隔
            if current_time - self.last_batch_time < self.micro_batch_interval:
                await asyncio.sleep(0.001)  # 1ms短暂等待
                continue
            
            # 2. 处理TTL过期订单
            await self._check_ttl_violations(current_time)
            
            # 3. 处理优先级队列中的事件
            if self.priority_queue:
                event = self.priority_queue.pop(0)  # 取出最高优先级事件
                
                try:
                    # 执行事件回调
                    if event.callback:
                        start_time = time.time()
                        await event.callback(event)
                        execution_time = (time.time() - start_time) * 1000  # ms
                        
                        # 记录Fill→Repost延迟
                        if event.event_type == "FILL":
                            fill_to_repost_delay = (time.time() - event.timestamp) * 1000
                            self.metrics.fill_to_repost_times.append(fill_to_repost_delay)
                            self.repost_success_count += 1
                            
                            logger.info(
                                "[MillisecondResponse] ⚡ FILL→REPOST: %s 延迟=%.1fms 执行=%.1fms",
                                event.order_id, fill_to_repost_delay, execution_time
                            )
                        
                        # 清理已处理的订单
                        if event.event_type in ["CANCEL", "FILL"] and event.order_id in self.active_orders:
                            del self.active_orders[event.order_id]
                        
                except Exception as e:
                    logger.error(
                        "[MillisecondResponse] 事件处理失败: %s %s - %s",
                        event.event_type, event.order_id, str(e)
                    )
            
            # 4. 记录微批间隔
            batch_interval = current_time - self.last_batch_time
            self.metrics.micro_batch_intervals.append(batch_interval * 1000)  # ms
            self.last_batch_time = current_time
            
            # 5. 短暂休眠保持微批节奏
            await asyncio.sleep(0.001)  # 1ms基础间隔
    
    async def _check_ttl_violations(self, current_time: float):
        """检查TTL违规并触发撤单"""
        expired_orders = []
        
        for order_id, order_info in self.active_orders.items():
            age = current_time - order_info['created_time']
            if age > order_info['ttl']:
                expired_orders.append(order_id)
                self.metrics.ttl_violations += 1
        
        # 处理过期订单（撤单）
        for order_id in expired_orders:
            order_info = self.active_orders[order_id]
            logger.warning(
                "[MillisecondResponse] 🕒 TTL过期: %s 存活=%.1fs TTL=%.1fs Level=%s",
                order_id, current_time - order_info['created_time'], 
                order_info['ttl'], order_info['level'].value
            )
            
            # 注册高优先级撤单事件
            self.register_cancel_event(order_id, self._handle_ttl_cancel)
    
    async def _handle_ttl_cancel(self, event: PriorityEvent):
        """处理TTL触发的撤单"""
        order_id = event.order_id
        logger.info(
            "[MillisecondResponse] 🚫 执行TTL撤单: %s",
            order_id
        )
        # 实际撤单逻辑将由调用方提供
        return True
    
    def get_response_metrics(self) -> Dict[str, Any]:
        """获取毫秒响应系统指标"""
        fill_times = self.metrics.fill_to_repost_times
        queue_sizes = self.metrics.event_queue_sizes
        batch_intervals = self.metrics.micro_batch_intervals
        
        return {
            'fill_to_repost_p50': statistics.median(fill_times) if fill_times else 0.0,
            'fill_to_repost_p95': self._percentile(fill_times, 95) if len(fill_times) > 20 else 0.0,
            'fill_to_repost_p99': self._percentile(fill_times, 99) if len(fill_times) > 100 else 0.0,
            'avg_queue_size': statistics.mean(queue_sizes) if queue_sizes else 0.0,
            'max_queue_size': max(queue_sizes) if queue_sizes else 0,
            'ttl_violations': self.metrics.ttl_violations,
            'priority_inversions': self.metrics.priority_inversions,
            'fill_events': self.fill_events_count,
            'repost_success': self.repost_success_count,
            'success_rate': (self.repost_success_count / max(self.fill_events_count, 1)) * 100,
            'avg_batch_interval': statistics.mean(batch_intervals) if batch_intervals else 0.0,
            'active_orders': len(self.active_orders)
        }
    
    def _percentile(self, data: List[float], percentile: int) -> float:
        """计算百分位数"""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        index = int((percentile / 100.0) * len(sorted_data))
        return sorted_data[min(index, len(sorted_data) - 1)]
    
    async def start(self):
        """启动毫秒响应系统"""
        self.running = True
        self.last_batch_time = time.time()
        logger.info("[MillisecondResponse] 🚀 毫秒响应系统启动")
        
        # 启动优先级队列处理循环
        asyncio.create_task(self.process_priority_queue())
    
    async def stop(self):
        """停止毫秒响应系统"""
        self.running = False
        logger.info("[MillisecondResponse] ⛔ 毫秒响应系统停止")


# 全局实例
_millisecond_response_system = None


def get_millisecond_response_system() -> MillisecondResponseSystem:
    """获取毫秒响应系统单例"""
    global _millisecond_response_system
    if _millisecond_response_system is None:
        _millisecond_response_system = MillisecondResponseSystem()
    return _millisecond_response_system