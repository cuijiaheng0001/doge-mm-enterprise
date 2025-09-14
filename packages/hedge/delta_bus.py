"""
Delta Bus - 事件聚合与发布系统
用于现货成交事件和对冲需求的统一管理
"""

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class EventType(Enum):
    """事件类型枚举"""
    SPOT_FILL = "spot_fill"
    PERP_FILL = "perp_fill"
    HEDGE_REQUEST = "hedge_request"
    POSITION_UPDATE = "position_update"
    MODE_CHANGE = "mode_change"


@dataclass
class DeltaEvent:
    """Delta事件数据结构"""
    event_type: EventType
    symbol: str
    side: str  # BUY/SELL
    qty: float  # 数量（DOGE）
    px: float  # 价格
    ts: float  # 时间戳
    delta_change: float  # Delta变化量
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def notional(self) -> float:
        """计算名义价值"""
        return self.qty * self.px


class DeltaBus:
    """
    Delta事件总线 - FAHE核心组件
    负责收集现货成交事件并发布给对冲系统
    """
    
    def __init__(self, max_queue_size: int = 1000, batch_size: int = 10):
        """
        初始化Delta Bus
        
        Args:
            max_queue_size: 最大队列大小
            batch_size: 批处理大小
        """
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        
        # 事件队列
        self.event_queue: deque = deque(maxlen=max_queue_size)
        
        # 订阅者列表
        self.subscribers: List[Callable[[DeltaEvent], None]] = []
        
        # 统计信息
        self.stats = {
            'events_published': 0,
            'events_processed': 0,
            'events_dropped': 0,
            'last_event_ts': 0,
            'avg_latency_ms': 0
        }
        
        # 延迟采样
        self.latency_samples = deque(maxlen=100)
        
        # 异步任务
        self.processor_task: Optional[asyncio.Task] = None
        self.running = False
        
        logger.info(f"[DeltaBus] 初始化完成: max_queue={max_queue_size}, batch={batch_size}")
    
    def publish_spot_fill(self, symbol: str, side: str, qty: float, px: float, ts: float = None) -> bool:
        """
        发布现货成交事件
        
        Args:
            symbol: 交易对
            side: 买卖方向
            qty: 成交数量
            px: 成交价格
            ts: 时间戳
        
        Returns:
            是否成功发布
        """
        if ts is None:
            ts = time.time()
        
        # 计算Delta变化
        delta_change = qty if side == 'BUY' else -qty
        
        # 创建事件
        event = DeltaEvent(
            event_type=EventType.SPOT_FILL,
            symbol=symbol,
            side=side,
            qty=qty,
            px=px,
            ts=ts,
            delta_change=delta_change,
            metadata={
                'source': 'spot_maker',
                'notional_usdt': qty * px
            }
        )
        
        return self._publish_event(event)
    
    def publish_perp_fill(self, symbol: str, side: str, qty: float, px: float, ts: float = None) -> bool:
        """
        发布永续合约成交事件
        
        Args:
            symbol: 交易对
            side: 买卖方向
            qty: 成交数量
            px: 成交价格
            ts: 时间戳
        
        Returns:
            是否成功发布
        """
        if ts is None:
            ts = time.time()
        
        # 永续合约Delta变化（与现货相反）
        delta_change = -qty if side == 'BUY' else qty
        
        # 创建事件
        event = DeltaEvent(
            event_type=EventType.PERP_FILL,
            symbol=symbol,
            side=side,
            qty=qty,
            px=px,
            ts=ts,
            delta_change=delta_change,
            metadata={
                'source': 'hedge_engine',
                'notional_usdt': qty * px
            }
        )
        
        return self._publish_event(event)
    
    def _publish_event(self, event: DeltaEvent) -> bool:
        """
        内部发布事件方法
        
        Args:
            event: Delta事件
        
        Returns:
            是否成功发布
        """
        try:
            # 检查队列是否已满
            if len(self.event_queue) >= self.max_queue_size:
                self.stats['events_dropped'] += 1
                logger.warning(f"[DeltaBus] 事件队列已满，丢弃事件: {event.event_type}")
                return False
            
            # 添加到队列
            self.event_queue.append(event)
            self.stats['events_published'] += 1
            self.stats['last_event_ts'] = event.ts
            
            # 计算延迟
            latency_ms = (time.time() - event.ts) * 1000
            self.latency_samples.append(latency_ms)
            
            # 更新平均延迟
            if self.latency_samples:
                self.stats['avg_latency_ms'] = sum(self.latency_samples) / len(self.latency_samples)
            
            logger.debug(f"[DeltaBus] 发布事件: {event.event_type.value} delta={event.delta_change:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"[DeltaBus] 发布事件失败: {e}")
            return False
    
    def subscribe(self, callback: Callable[[DeltaEvent], None]) -> None:
        """
        订阅Delta事件
        
        Args:
            callback: 回调函数
        """
        if callback not in self.subscribers:
            self.subscribers.append(callback)
            logger.info(f"[DeltaBus] 新增订阅者，当前订阅数: {len(self.subscribers)}")
    
    def unsubscribe(self, callback: Callable[[DeltaEvent], None]) -> None:
        """
        取消订阅
        
        Args:
            callback: 回调函数
        """
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            logger.info(f"[DeltaBus] 移除订阅者，当前订阅数: {len(self.subscribers)}")
    
    async def _process_events(self) -> None:
        """
        异步处理事件队列
        """
        logger.info("[DeltaBus] 事件处理器启动")
        
        while self.running:
            try:
                # 批量处理事件
                batch = []
                for _ in range(min(self.batch_size, len(self.event_queue))):
                    if self.event_queue:
                        batch.append(self.event_queue.popleft())
                
                # 处理批次
                if batch:
                    for event in batch:
                        # 通知所有订阅者
                        for subscriber in self.subscribers:
                            try:
                                # 如果是协程，await它
                                if asyncio.iscoroutinefunction(subscriber):
                                    await subscriber(event)
                                else:
                                    subscriber(event)
                            except Exception as e:
                                logger.error(f"[DeltaBus] 订阅者处理失败: {e}")
                        
                        self.stats['events_processed'] += 1
                    
                    logger.debug(f"[DeltaBus] 处理批次: {len(batch)}个事件")
                
                # 短暂休眠
                await asyncio.sleep(0.001)  # 1ms
                
            except Exception as e:
                logger.error(f"[DeltaBus] 事件处理异常: {e}")
                await asyncio.sleep(0.1)
        
        logger.info("[DeltaBus] 事件处理器停止")
    
    async def start(self) -> None:
        """
        启动Delta Bus
        """
        if self.running:
            logger.warning("[DeltaBus] 已在运行中")
            return
        
        self.running = True
        self.processor_task = asyncio.create_task(self._process_events())
        logger.info("[DeltaBus] ✅ 启动成功")
    
    async def stop(self) -> None:
        """
        停止Delta Bus
        """
        if not self.running:
            return
        
        self.running = False
        
        if self.processor_task:
            await self.processor_task
            self.processor_task = None
        
        logger.info("[DeltaBus] 已停止")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'queue_size': len(self.event_queue),
            'subscribers': len(self.subscribers),
            'queue_utilization': len(self.event_queue) / self.max_queue_size
        }
    
    def get_latency_percentiles(self) -> Dict[str, float]:
        """
        获取延迟分位数
        
        Returns:
            延迟统计
        """
        if not self.latency_samples:
            return {'p50': 0, 'p90': 0, 'p95': 0, 'p99': 0}
        
        sorted_samples = sorted(self.latency_samples)
        n = len(sorted_samples)
        
        return {
            'p50': sorted_samples[int(n * 0.5)],
            'p90': sorted_samples[int(n * 0.9)],
            'p95': sorted_samples[int(n * 0.95)],
            'p99': sorted_samples[int(n * 0.99)] if n > 0 else sorted_samples[-1]
        }