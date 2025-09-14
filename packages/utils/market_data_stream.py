"""
F13: 市场数据事件流 - WebSocket数据同步和延迟优化
高效处理实时市场数据，最小化延迟和CPU开销
"""
import time
import asyncio
import logging
from typing import Dict, Any, Optional, List, Callable
from collections import deque
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)


@dataclass
class MarketDataSnapshot:
    """市场数据快照"""
    symbol: str
    bid: float
    ask: float
    mid: float
    spread: float
    timestamp: float
    exchange_timestamp: Optional[float] = None
    sequence: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'bid': self.bid,
            'ask': self.ask, 
            'mid': self.mid,
            'spread': self.spread,
            'timestamp': self.timestamp,
            'exchange_timestamp': self.exchange_timestamp,
            'sequence': self.sequence,
            'latency_ms': (self.timestamp - self.exchange_timestamp) * 1000 if self.exchange_timestamp else None
        }


class MarketDataStream:
    """F13: 市场数据流处理器 - 高效WebSocket数据处理"""
    
    def __init__(self, symbol: str = "DOGEUSDT"):
        self.symbol = symbol
        
        # 数据缓存
        self.latest_snapshot: Optional[MarketDataSnapshot] = None
        self.price_history = deque(maxlen=100)  # 最近100个价格点
        self.update_history = deque(maxlen=1000)  # 更新历史
        
        # 性能统计
        self.stats = {
            'total_updates': 0,
            'avg_latency_ms': 0.0,
            'update_rate': 0.0,  # 每秒更新次数
            'stale_data_count': 0,
            'sequence_gaps': 0
        }
        
        # 延迟监控
        self.latency_samples = deque(maxlen=500)
        self.last_update_time = 0
        self.update_intervals = deque(maxlen=100)
        
        # 数据质量监控
        self.last_sequence = None
        self.spread_history = deque(maxlen=50)
        self.price_volatility = 0.0
        
        # 订阅者
        self.subscribers: List[Callable] = []
        self.async_subscribers: List[Callable] = []
        
        # 数据验证配置
        self.max_spread_pct = 0.05  # 最大价差5%
        self.max_latency_ms = 1000  # 最大延迟1秒
        self.stale_threshold_ms = 5000  # 数据过期阈值5秒
        
        logger.info(f"[F13] MarketDataStream initialized for {symbol}")
    
    def subscribe(self, callback: Callable, is_async: bool = False):
        """订阅市场数据更新"""
        if is_async:
            self.async_subscribers.append(callback)
        else:
            self.subscribers.append(callback)
        
        logger.debug(f"[F13] 新订阅者注册 (async={is_async})")
    
    def validate_data(self, bid: float, ask: float, timestamp: float) -> bool:
        """验证数据质量"""
        # 基本验证
        if bid <= 0 or ask <= 0 or bid >= ask:
            logger.warning(f"[F13] 无效价格数据: bid={bid}, ask={ask}")
            return False
        
        # 价差验证
        spread_pct = (ask - bid) / ((ask + bid) / 2)
        if spread_pct > self.max_spread_pct:
            logger.warning(f"[F13] 价差过大: {spread_pct:.3%}")
            return False
        
        # 时间验证
        now = time.time()
        latency_ms = (now - timestamp) * 1000
        if latency_ms > self.max_latency_ms:
            logger.warning(f"[F13] 数据延迟过高: {latency_ms:.1f}ms")
            self.stats['stale_data_count'] += 1
            return False
        
        return True
    
    async def update_market_data(self, bid: float, ask: float, 
                                exchange_timestamp: Optional[float] = None,
                                sequence: Optional[int] = None):
        """更新市场数据"""
        now = time.time()
        
        # 使用交易所时间戳或当前时间
        data_timestamp = exchange_timestamp if exchange_timestamp else now
        
        # 数据验证
        if not self.validate_data(bid, ask, data_timestamp):
            return False
        
        # 计算衍生数据
        mid = (bid + ask) / 2.0
        spread = ask - bid
        
        # 创建快照
        snapshot = MarketDataSnapshot(
            symbol=self.symbol,
            bid=bid,
            ask=ask,
            mid=mid,
            spread=spread,
            timestamp=now,
            exchange_timestamp=exchange_timestamp,
            sequence=sequence
        )
        
        # 更新统计
        self._update_statistics(snapshot)
        
        # 检查序列号
        self._check_sequence(sequence)
        
        # 存储快照
        self.latest_snapshot = snapshot
        self.price_history.append((now, mid))
        self.spread_history.append(spread)
        self.update_history.append(snapshot)
        
        # 异步通知订阅者
        await self._notify_subscribers(snapshot)
        
        return True
    
    def _update_statistics(self, snapshot: MarketDataSnapshot):
        """更新性能统计"""
        now = time.time()
        
        # 更新计数
        self.stats['total_updates'] += 1
        
        # 计算延迟
        if snapshot.exchange_timestamp:
            latency_ms = (snapshot.timestamp - snapshot.exchange_timestamp) * 1000
            self.latency_samples.append(latency_ms)
            
            if self.latency_samples:
                self.stats['avg_latency_ms'] = sum(self.latency_samples) / len(self.latency_samples)
        
        # 计算更新频率
        if self.last_update_time > 0:
            interval = now - self.last_update_time
            self.update_intervals.append(interval)
            
            if self.update_intervals:
                avg_interval = sum(self.update_intervals) / len(self.update_intervals)
                self.stats['update_rate'] = 1.0 / avg_interval if avg_interval > 0 else 0
        
        self.last_update_time = now
        
        # 计算价格波动性
        if len(self.price_history) >= 10:
            recent_prices = [price for _, price in list(self.price_history)[-10:]]
            if recent_prices:
                price_mean = sum(recent_prices) / len(recent_prices)
                price_variance = sum((p - price_mean) ** 2 for p in recent_prices) / len(recent_prices)
                self.price_volatility = (price_variance ** 0.5) / price_mean if price_mean > 0 else 0
    
    def _check_sequence(self, sequence: Optional[int]):
        """检查序列号连续性"""
        if sequence is not None and self.last_sequence is not None:
            if sequence != self.last_sequence + 1:
                gap = sequence - self.last_sequence - 1
                if gap > 0:
                    self.stats['sequence_gaps'] += gap
                    logger.warning(f"[F13] 序列号跳跃: {self.last_sequence} -> {sequence} (gap={gap})")
        
        self.last_sequence = sequence
    
    async def _notify_subscribers(self, snapshot: MarketDataSnapshot):
        """异步通知所有订阅者"""
        # 同步订阅者
        for callback in self.subscribers:
            try:
                callback(snapshot)
            except Exception as e:
                logger.error(f"[F13] 同步订阅者错误: {e}")
        
        # 异步订阅者
        if self.async_subscribers:
            tasks = []
            for callback in self.async_subscribers:
                try:
                    task = asyncio.create_task(callback(snapshot))
                    tasks.append(task)
                except Exception as e:
                    logger.error(f"[F13] 异步订阅者创建错误: {e}")
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
    
    def get_latest_data(self) -> Optional[Dict[str, Any]]:
        """获取最新市场数据"""
        if self.latest_snapshot is None:
            return None
        
        return self.latest_snapshot.to_dict()
    
    def is_data_fresh(self, max_age_ms: float = 1000) -> bool:
        """检查数据是否新鲜"""
        if self.latest_snapshot is None:
            return False
        
        age_ms = (time.time() - self.latest_snapshot.timestamp) * 1000
        return age_ms <= max_age_ms
    
    def get_price_trend(self, window_seconds: float = 10.0) -> Dict[str, float]:
        """获取价格趋势"""
        now = time.time()
        cutoff = now - window_seconds
        
        recent_prices = [(ts, price) for ts, price in self.price_history if ts >= cutoff]
        
        if len(recent_prices) < 2:
            return {'trend': 0.0, 'volatility': 0.0, 'price_change': 0.0}
        
        # 计算趋势
        first_price = recent_prices[0][1]
        last_price = recent_prices[-1][1]
        price_change = (last_price - first_price) / first_price if first_price > 0 else 0
        
        # 简单趋势计算
        prices = [price for _, price in recent_prices]
        mid_index = len(prices) // 2
        first_half_avg = sum(prices[:mid_index]) / mid_index if mid_index > 0 else 0
        second_half_avg = sum(prices[mid_index:]) / (len(prices) - mid_index) if len(prices) > mid_index else 0
        
        trend = (second_half_avg - first_half_avg) / first_half_avg if first_half_avg > 0 else 0
        
        return {
            'trend': trend,
            'volatility': self.price_volatility,
            'price_change': price_change,
            'sample_count': len(recent_prices)
        }
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """获取性能指标"""
        latency_percentiles = {}
        if self.latency_samples:
            sorted_latencies = sorted(self.latency_samples)
            n = len(sorted_latencies)
            latency_percentiles = {
                'p50': sorted_latencies[int(n * 0.5)] if n > 0 else 0,
                'p90': sorted_latencies[int(n * 0.9)] if n > 0 else 0,
                'p95': sorted_latencies[int(n * 0.95)] if n > 0 else 0,
                'p99': sorted_latencies[int(n * 0.99)] if n > 0 else 0
            }
        
        current_spread_pct = 0
        if self.latest_snapshot:
            current_spread_pct = (self.latest_snapshot.spread / self.latest_snapshot.mid) * 100 if self.latest_snapshot.mid > 0 else 0
        
        return {
            'updates_total': self.stats['total_updates'],
            'update_rate_per_sec': self.stats['update_rate'],
            'avg_latency_ms': self.stats['avg_latency_ms'],
            'latency_percentiles': latency_percentiles,
            'sequence_gaps': self.stats['sequence_gaps'],
            'stale_data_count': self.stats['stale_data_count'],
            'current_spread_bps': current_spread_pct * 100,  # 基点
            'price_volatility': self.price_volatility,
            'subscribers_count': len(self.subscribers) + len(self.async_subscribers),
            'data_freshness': self.is_data_fresh()
        }
    
    def log_performance(self):
        """记录性能状态"""
        metrics = self.get_performance_metrics()
        
        logger.info(
            f"[F13-MD] updates={metrics['updates_total']} "
            f"rate={metrics['update_rate_per_sec']:.1f}/s "
            f"latency={metrics['avg_latency_ms']:.1f}ms "
            f"p95={metrics['latency_percentiles'].get('p95', 0):.1f}ms "
            f"spread={metrics['current_spread_bps']:.1f}bps "
            f"fresh={metrics['data_freshness']}"
        )


# 便利函数：从WebSocket数据更新市场流
async def update_from_websocket_data(stream: MarketDataStream, ws_data: Dict[str, Any]):
    """从WebSocket数据更新市场流"""
    try:
        # 解析Binance WebSocket数据格式
        if 'b' in ws_data and 'a' in ws_data:  # bookTicker格式
            bid = float(ws_data['b'])
            ask = float(ws_data['a'])
            exchange_timestamp = ws_data.get('E', 0) / 1000.0  # 毫秒转秒
            
            await stream.update_market_data(
                bid=bid,
                ask=ask, 
                exchange_timestamp=exchange_timestamp
            )
            return True
            
        elif 'bids' in ws_data and 'asks' in ws_data:  # depth格式
            if ws_data['bids'] and ws_data['asks']:
                bid = float(ws_data['bids'][0][0])
                ask = float(ws_data['asks'][0][0])
                sequence = ws_data.get('lastUpdateId')
                
                await stream.update_market_data(
                    bid=bid,
                    ask=ask,
                    sequence=sequence
                )
                return True
    
    except Exception as e:
        logger.error(f"[F13] WebSocket数据解析错误: {e}")
        return False
    
    return False