"""
F13: 事件驱动协调器 - 异步事件队列和优先级处理机制
用于提升系统响应速度和事件处理效率
"""
import time
import asyncio
import logging
from typing import Dict, Any, Optional, Callable, List, Tuple
from collections import deque
from enum import Enum
import heapq
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class EventPriority(Enum):
    """事件优先级定义"""
    CRITICAL = 1     # 紧急：限价单填充、风控触发
    HIGH = 2         # 高优先级：市场数据更新、订单状态变化
    NORMAL = 3       # 正常：常规监控、日志记录
    LOW = 4          # 低优先级：统计计算、清理任务


class EventType(Enum):
    """事件类型定义"""
    ORDER_FILL = "order_fill"
    ORDER_UPDATE = "order_update" 
    MARKET_DATA = "market_data"
    RISK_ALERT = "risk_alert"
    SYSTEM_STATUS = "system_status"
    CLEANUP = "cleanup"
    STATS_UPDATE = "stats_update"


@dataclass
class Event:
    """事件对象"""
    event_type: EventType
    priority: EventPriority
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    callback: Optional[Callable] = None
    
    def __lt__(self, other):
        """用于优先级队列排序"""
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        return self.timestamp < other.timestamp


class EventDrivenCoordinator:
    """F13: 事件驱动协调器 - 异步事件处理和优先级调度"""
    
    def __init__(self):
        # 事件队列配置
        self.max_queue_size = 1000
        self.batch_size = 10
        self.process_interval = 0.001  # 1ms处理间隔
        
        # 优先级队列
        self.event_queue = []  # heapq优先级队列
        self.queue_lock = asyncio.Lock()
        
        # 事件处理器映射
        self.event_handlers: Dict[EventType, List[Callable]] = {}
        self.async_handlers: Dict[EventType, List[Callable]] = {}
        
        # 性能统计
        self.event_stats = {
            'total_processed': 0,
            'by_type': {},
            'by_priority': {},
            'avg_latency_ms': 0.0,
            'queue_size_history': deque(maxlen=100)
        }
        
        # 延迟统计
        self.latency_history = deque(maxlen=1000)
        self.processing_times = {}
        
        # 运行状态
        self.running = False
        self.processor_task: Optional[asyncio.Task] = None
        
        logger.info(f"[F13] EventDrivenCoordinator initialized: max_queue={self.max_queue_size}")
    
    def register_handler(self, event_type: EventType, handler: Callable, is_async: bool = False):
        """注册事件处理器"""
        if is_async:
            if event_type not in self.async_handlers:
                self.async_handlers[event_type] = []
            self.async_handlers[event_type].append(handler)
        else:
            if event_type not in self.event_handlers:
                self.event_handlers[event_type] = []
            self.event_handlers[event_type].append(handler)
        
        logger.debug(f"[F13] 注册处理器: {event_type.value} (async={is_async})")
    
    async def emit_event(self, event_type: EventType, data: Dict[str, Any], 
                        priority: EventPriority = EventPriority.NORMAL,
                        source: str = "", callback: Optional[Callable] = None) -> bool:
        """发出事件"""
        async with self.queue_lock:
            if len(self.event_queue) >= self.max_queue_size:
                logger.warning(f"[F13] 事件队列已满，丢弃事件: {event_type.value}")
                return False
            
            event = Event(
                event_type=event_type,
                priority=priority,
                data=data,
                source=source,
                callback=callback
            )
            
            heapq.heappush(self.event_queue, event)
            self.event_stats['queue_size_history'].append(len(self.event_queue))
            
            logger.debug(f"[F13] 事件入队: {event_type.value} priority={priority.name} queue_size={len(self.event_queue)}")
            return True
    
    async def _process_event(self, event: Event):
        """处理单个事件"""
        start_time = time.time()
        
        try:
            # 同步处理器
            if event.event_type in self.event_handlers:
                for handler in self.event_handlers[event.event_type]:
                    try:
                        handler(event)
                    except Exception as e:
                        logger.error(f"[F13] 同步处理器错误: {e}")
            
            # 异步处理器
            if event.event_type in self.async_handlers:
                tasks = []
                for handler in self.async_handlers[event.event_type]:
                    try:
                        task = asyncio.create_task(handler(event))
                        tasks.append(task)
                    except Exception as e:
                        logger.error(f"[F13] 异步处理器创建错误: {e}")
                
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            
            # 执行回调
            if event.callback:
                try:
                    if asyncio.iscoroutinefunction(event.callback):
                        await event.callback(event)
                    else:
                        event.callback(event)
                except Exception as e:
                    logger.error(f"[F13] 回调执行错误: {e}")
            
            # 统计处理时间
            process_time = (time.time() - start_time) * 1000  # ms
            self.latency_history.append(process_time)
            
            # 更新统计
            self.event_stats['total_processed'] += 1
            
            event_type_str = event.event_type.value
            if event_type_str not in self.event_stats['by_type']:
                self.event_stats['by_type'][event_type_str] = 0
            self.event_stats['by_type'][event_type_str] += 1
            
            priority_str = event.priority.name
            if priority_str not in self.event_stats['by_priority']:
                self.event_stats['by_priority'][priority_str] = 0
            self.event_stats['by_priority'][priority_str] += 1
            
            # 计算平均延迟
            if self.latency_history:
                self.event_stats['avg_latency_ms'] = sum(self.latency_history) / len(self.latency_history)
            
        except Exception as e:
            logger.error(f"[F13] 事件处理错误: {event.event_type.value} - {e}")
    
    async def _event_processor(self):
        """事件处理主循环"""
        logger.info("[F13] 事件处理器启动")
        
        while self.running:
            try:
                events_to_process = []
                
                # 批量获取事件
                async with self.queue_lock:
                    batch_count = min(self.batch_size, len(self.event_queue))
                    for _ in range(batch_count):
                        if self.event_queue:
                            event = heapq.heappop(self.event_queue)
                            events_to_process.append(event)
                
                # 批量处理事件
                if events_to_process:
                    # 按优先级分组处理
                    critical_events = [e for e in events_to_process if e.priority == EventPriority.CRITICAL]
                    other_events = [e for e in events_to_process if e.priority != EventPriority.CRITICAL]
                    
                    # 优先处理紧急事件
                    for event in critical_events:
                        await self._process_event(event)
                    
                    # 并发处理其他事件
                    if other_events:
                        tasks = [self._process_event(event) for event in other_events]
                        await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    # 空闲时短暂休眠
                    await asyncio.sleep(self.process_interval)
                    
            except Exception as e:
                logger.error(f"[F13] 事件处理器错误: {e}")
                await asyncio.sleep(0.01)  # 错误恢复延迟
    
    async def start(self):
        """启动事件处理器"""
        if self.running:
            return
            
        self.running = True
        self.processor_task = asyncio.create_task(self._event_processor())
        logger.info("[F13] 事件驱动协调器已启动")
    
    async def stop(self):
        """停止事件处理器"""
        if not self.running:
            return
            
        self.running = False
        
        if self.processor_task:
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                pass
        
        # 处理剩余事件
        while self.event_queue:
            event = heapq.heappop(self.event_queue)
            await self._process_event(event)
        
        logger.info("[F13] 事件驱动协调器已停止")
    
    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        return {
            'queue_size': len(self.event_queue),
            'max_size': self.max_queue_size,
            'utilization': len(self.event_queue) / self.max_queue_size,
            'running': self.running
        }
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计"""
        queue_sizes = list(self.event_stats['queue_size_history'])
        
        return {
            'events_processed': self.event_stats['total_processed'],
            'avg_latency_ms': self.event_stats['avg_latency_ms'],
            'by_type': self.event_stats['by_type'].copy(),
            'by_priority': self.event_stats['by_priority'].copy(),
            'queue_metrics': {
                'current_size': len(self.event_queue),
                'avg_size': sum(queue_sizes) / len(queue_sizes) if queue_sizes else 0,
                'max_size_seen': max(queue_sizes) if queue_sizes else 0
            },
            'latency_percentiles': self._calculate_latency_percentiles()
        }
    
    def _calculate_latency_percentiles(self) -> Dict[str, float]:
        """计算延迟百分位数"""
        if not self.latency_history:
            return {'p50': 0, 'p95': 0, 'p99': 0}
        
        sorted_latencies = sorted(self.latency_history)
        n = len(sorted_latencies)
        
        return {
            'p50': sorted_latencies[int(n * 0.5)] if n > 0 else 0,
            'p95': sorted_latencies[int(n * 0.95)] if n > 0 else 0,
            'p99': sorted_latencies[int(n * 0.99)] if n > 0 else 0
        }
    
    def log_status(self):
        """记录状态信息"""
        stats = self.get_performance_stats()
        queue_status = self.get_queue_status()
        
        logger.info(
            f"[F13] events={stats['events_processed']} "
            f"latency_ms={stats['avg_latency_ms']:.2f} "
            f"queue={queue_status['queue_size']}/{queue_status['max_size']} "
            f"util={queue_status['utilization']:.1%} "
            f"p95={stats['latency_percentiles']['p95']:.2f}ms"
        )


# 全局事件协调器实例
event_coordinator = EventDrivenCoordinator()


# 便利函数
async def emit_order_fill_event(order_data: Dict[str, Any], source: str = ""):
    """发出订单填充事件"""
    await event_coordinator.emit_event(
        EventType.ORDER_FILL, 
        order_data, 
        EventPriority.CRITICAL,
        source
    )


async def emit_market_data_event(market_data: Dict[str, Any], source: str = ""):
    """发出市场数据事件"""
    await event_coordinator.emit_event(
        EventType.MARKET_DATA, 
        market_data, 
        EventPriority.HIGH,
        source
    )


async def emit_risk_alert_event(risk_data: Dict[str, Any], source: str = ""):
    """发出风控警告事件"""
    await event_coordinator.emit_event(
        EventType.RISK_ALERT, 
        risk_data, 
        EventPriority.CRITICAL,
        source
    )