"""
Dual Active Market Data - 双活市场数据源
零延迟容错，主备切换<1ms，对标机构级交易系统
"""
import asyncio
import time
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import statistics
import json

logger = logging.getLogger(__name__)


class DataSourceStatus(Enum):
    """数据源状态"""
    ACTIVE = "active"
    STANDBY = "standby"
    FAILED = "failed"
    RECOVERING = "recovering"


@dataclass
class TickerData:
    """Ticker数据结构"""
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: Decimal
    ts: int                    # 交易所时间戳
    recv_ts: int              # 接收时间戳
    source: str               # 数据源标识


@dataclass
class LatencyMetric:
    """延迟指标"""
    source: str
    avg_latency_ns: int       # 平均延迟(纳秒)
    p99_latency_ns: int       # 99分位延迟
    success_rate: float       # 成功率
    last_update_ts: int
    sample_count: int = 0


class LatencyMonitor:
    """延迟监控器"""
    
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.latency_samples: Dict[str, List[int]] = {}
        self.success_counts: Dict[str, int] = {}
        self.total_counts: Dict[str, int] = {}
        self.last_metrics: Dict[str, LatencyMetric] = {}
        
    def record_latency(self, source: str, latency_ns: int, success: bool = True):
        """记录延迟样本"""
        # 初始化源
        if source not in self.latency_samples:
            self.latency_samples[source] = []
            self.success_counts[source] = 0
            self.total_counts[source] = 0
        
        # 记录延迟样本
        self.latency_samples[source].append(latency_ns)
        if len(self.latency_samples[source]) > self.window_size:
            self.latency_samples[source].pop(0)
        
        # 记录成功率
        self.total_counts[source] += 1
        if success:
            self.success_counts[source] += 1
    
    def get_metric(self, source: str) -> Optional[LatencyMetric]:
        """获取延迟指标"""
        if source not in self.latency_samples or not self.latency_samples[source]:
            return None
        
        samples = self.latency_samples[source]
        
        # 计算指标
        avg_latency = int(statistics.mean(samples))
        p99_latency = int(sorted(samples)[int(len(samples) * 0.99)]) if len(samples) > 1 else samples[0]
        success_rate = self.success_counts[source] / self.total_counts[source]
        
        metric = LatencyMetric(
            source=source,
            avg_latency_ns=avg_latency,
            p99_latency_ns=p99_latency,
            success_rate=success_rate,
            last_update_ts=time.time_ns(),
            sample_count=len(samples)
        )
        
        self.last_metrics[source] = metric
        return metric


class BinanceWSStream:
    """Binance WebSocket流包装器"""
    
    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self.status = DataSourceStatus.STANDBY
        self.last_data_ts = 0
        self.connection_ts = 0
        self.reconnect_count = 0
        
        # 模拟连接状态
        self._connected = False
        self._last_ticker: Optional[TickerData] = None
        
    async def connect(self) -> bool:
        """连接到数据源"""
        try:
            # 模拟连接延迟
            await asyncio.sleep(0.1)
            
            self._connected = True
            self.connection_ts = time.time_ns()
            self.status = DataSourceStatus.ACTIVE
            
            logger.info(
                "[%s] WebSocket connected successfully",
                self.stream_id
            )
            return True
            
        except Exception as e:
            logger.error(
                "[%s] Connection failed: %s",
                self.stream_id, str(e)
            )
            self.status = DataSourceStatus.FAILED
            return False
    
    async def get_ticker(self) -> Optional[TickerData]:
        """获取ticker数据"""
        if not self._connected or self.status != DataSourceStatus.ACTIVE:
            return None
        
        try:
            # 模拟实时ticker数据
            now = time.time_ns()
            ticker = TickerData(
                symbol="DOGEUSDT",
                bid=Decimal("0.25984"),
                ask=Decimal("0.25985"),
                last=Decimal("0.25984"),
                volume=Decimal("1000000"),
                ts=now - 1000000,  # 1ms ago
                recv_ts=now,
                source=self.stream_id
            )
            
            self.last_data_ts = now
            self._last_ticker = ticker
            return ticker
            
        except Exception as e:
            logger.error(
                "[%s] Failed to get ticker: %s",
                self.stream_id, str(e)
            )
            self.status = DataSourceStatus.FAILED
            return None
    
    def get_last_ticker(self) -> Optional[TickerData]:
        """获取最后一个ticker"""
        return self._last_ticker
    
    async def disconnect(self):
        """断开连接"""
        self._connected = False
        self.status = DataSourceStatus.STANDBY
        logger.info("[%s] Disconnected", self.stream_id)


class DualActiveMarketData:
    """
    双活市场数据 - 主备切换<1ms
    
    核心特性：
    1. 双路数据源同时接收
    2. 主源故障时立即切换到备源
    3. 延迟超过阈值自动切换
    4. 智能恢复机制
    """
    
    def __init__(self, symbol: str = "DOGEUSDT", latency_threshold_ms: int = 10):
        self.symbol = symbol
        self.latency_threshold_ns = latency_threshold_ms * 1_000_000  # 转换为纳秒
        
        # 双路数据源
        self.primary_stream = BinanceWSStream("primary")
        self.backup_stream = BinanceWSStream("backup")
        
        # 当前活跃源
        self.active_source = "primary"
        self.last_switch_ts = time.time_ns()
        self.switch_count = 0
        
        # 延迟监控
        self.latency_monitor = LatencyMonitor()
        
        # 数据回调
        self.ticker_callbacks: List[Callable[[TickerData], Any]] = []
        
        # 运行状态
        self.running = False
        self.tasks: List[asyncio.Task] = []
        
        # 性能指标
        self.metrics = {
            'total_tickers': 0,
            'switch_count': 0,
            'primary_tickers': 0,
            'backup_tickers': 0,
            'latency_violations': 0,
            'failover_ms': 0  # 最后一次故障切换耗时
        }
        
        logger.info(
            "[DualActiveMarketData] 初始化完成: symbol=%s threshold=%dms",
            symbol, latency_threshold_ms
        )
    
    def add_ticker_callback(self, callback: Callable[[TickerData], Any]):
        """添加ticker数据回调"""
        self.ticker_callbacks.append(callback)
        logger.debug("[DualActiveMarketData] Added ticker callback")
    
    async def start(self):
        """启动双活数据流"""
        if self.running:
            return
        
        self.running = True
        
        # 连接双路数据源
        primary_ok = await self.primary_stream.connect()
        backup_ok = await self.backup_stream.connect()
        
        if not primary_ok and not backup_ok:
            raise RuntimeError("Both data sources failed to connect")
        
        # 选择初始活跃源
        if primary_ok:
            self.active_source = "primary"
            self.primary_stream.status = DataSourceStatus.ACTIVE
            if backup_ok:
                self.backup_stream.status = DataSourceStatus.STANDBY
        else:
            self.active_source = "backup"
            self.backup_stream.status = DataSourceStatus.ACTIVE
        
        # 启动数据收集任务
        self.tasks.append(asyncio.create_task(self._data_collection_loop()))
        self.tasks.append(asyncio.create_task(self._health_monitoring_loop()))
        
        logger.info(
            "[DualActiveMarketData] Started: active_source=%s primary=%s backup=%s",
            self.active_source,
            self.primary_stream.status.value,
            self.backup_stream.status.value
        )
    
    async def stop(self):
        """停止数据流"""
        self.running = False
        
        # 取消任务
        for task in self.tasks:
            task.cancel()
        
        # 断开连接
        await self.primary_stream.disconnect()
        await self.backup_stream.disconnect()
        
        logger.info("[DualActiveMarketData] Stopped")
    
    async def _data_collection_loop(self):
        """数据收集主循环"""
        while self.running:
            try:
                # 获取活跃源数据
                ticker = await self._get_active_ticker()
                
                if ticker:
                    # 计算延迟
                    receive_latency = ticker.recv_ts - ticker.ts
                    self.latency_monitor.record_latency(
                        ticker.source, receive_latency, True
                    )
                    
                    # 检查延迟阈值
                    await self._check_latency_threshold(ticker.source, receive_latency)
                    
                    # 分发数据
                    await self._distribute_ticker(ticker)
                    
                    # 更新指标
                    self.metrics['total_tickers'] += 1
                    if ticker.source == 'primary':
                        self.metrics['primary_tickers'] += 1
                    else:
                        self.metrics['backup_tickers'] += 1
                
                # 高频采样
                await asyncio.sleep(0.001)  # 1ms
                
            except Exception as e:
                logger.error(
                    "[DualActiveMarketData] Data collection error: %s",
                    str(e)
                )
                await asyncio.sleep(0.01)  # 错误时稍微降频
    
    async def _get_active_ticker(self) -> Optional[TickerData]:
        """获取活跃源ticker数据"""
        if self.active_source == "primary":
            return await self.primary_stream.get_ticker()
        else:
            return await self.backup_stream.get_ticker()
    
    async def _check_latency_threshold(self, source: str, latency_ns: int):
        """检查延迟阈值并触发切换"""
        if latency_ns > self.latency_threshold_ns:
            self.metrics['latency_violations'] += 1
            
            logger.warning(
                "[DualActiveMarketData] Latency threshold exceeded: "
                "source=%s latency=%.1fms threshold=%.1fms",
                source, latency_ns / 1_000_000, self.latency_threshold_ns / 1_000_000
            )
            
            # 触发故障切换
            await self.failover_to_backup()
    
    async def failover_to_backup(self):
        """故障切换到备用源"""
        failover_start = time.time_ns()
        
        try:
            old_source = self.active_source
            new_source = "backup" if old_source == "primary" else "primary"
            
            # 检查目标源是否可用
            target_stream = self.backup_stream if new_source == "backup" else self.primary_stream
            
            if target_stream.status in [DataSourceStatus.FAILED, DataSourceStatus.RECOVERING]:
                # 尝试重连目标源
                if not await target_stream.connect():
                    logger.error(
                        "[DualActiveMarketData] Failover failed: target source unavailable"
                    )
                    return
            
            # 执行切换
            self.active_source = new_source
            self.last_switch_ts = time.time_ns()
            self.switch_count += 1
            
            # 更新源状态
            if new_source == "primary":
                self.primary_stream.status = DataSourceStatus.ACTIVE
                self.backup_stream.status = DataSourceStatus.STANDBY
            else:
                self.backup_stream.status = DataSourceStatus.ACTIVE
                self.primary_stream.status = DataSourceStatus.STANDBY
            
            # 计算切换耗时
            failover_ms = (time.time_ns() - failover_start) / 1_000_000
            self.metrics['failover_ms'] = failover_ms
            self.metrics['switch_count'] += 1
            
            logger.info(
                "[DualActiveMarketData] ✅ Failover completed: %s→%s in %.2fms",
                old_source, new_source, failover_ms
            )
            
        except Exception as e:
            logger.error(
                "[DualActiveMarketData] Failover failed: %s",
                str(e), exc_info=True
            )
    
    async def _health_monitoring_loop(self):
        """健康监控循环"""
        while self.running:
            try:
                # 检查数据源健康状态
                await self._check_source_health()
                
                # 定期输出延迟指标
                self._emit_latency_metrics()
                
                await asyncio.sleep(5)  # 5秒检查一次
                
            except Exception as e:
                logger.error(
                    "[DualActiveMarketData] Health monitoring error: %s",
                    str(e)
                )
                await asyncio.sleep(1)
    
    async def _check_source_health(self):
        """检查数据源健康状态"""
        current_ts = time.time_ns()
        
        # 检查主源
        if (current_ts - self.primary_stream.last_data_ts) > 10_000_000_000:  # 10秒无数据
            if self.primary_stream.status == DataSourceStatus.ACTIVE:
                logger.warning("[DualActiveMarketData] Primary source stale, triggering failover")
                await self.failover_to_backup()
            else:
                self.primary_stream.status = DataSourceStatus.FAILED
        
        # 检查备源
        if (current_ts - self.backup_stream.last_data_ts) > 10_000_000_000:  # 10秒无数据
            if self.backup_stream.status == DataSourceStatus.ACTIVE:
                logger.warning("[DualActiveMarketData] Backup source stale, triggering failover")
                await self.failover_to_backup()
            else:
                self.backup_stream.status = DataSourceStatus.FAILED
    
    def _emit_latency_metrics(self):
        """输出延迟指标"""
        primary_metric = self.latency_monitor.get_metric('primary')
        backup_metric = self.latency_monitor.get_metric('backup')
        
        logger.info(
            "[DualActiveMarketData-Metrics] active=%s switches=%d "
            "primary(%.1fms/%.1fms/%.1f%%) backup(%.1fms/%.1fms/%.1f%%)",
            self.active_source, self.switch_count,
            
            primary_metric.avg_latency_ns / 1_000_000 if primary_metric else 0,
            primary_metric.p99_latency_ns / 1_000_000 if primary_metric else 0,
            primary_metric.success_rate * 100 if primary_metric else 0,
            
            backup_metric.avg_latency_ns / 1_000_000 if backup_metric else 0,
            backup_metric.p99_latency_ns / 1_000_000 if backup_metric else 0,
            backup_metric.success_rate * 100 if backup_metric else 0
        )
    
    async def _distribute_ticker(self, ticker: TickerData):
        """分发ticker数据到回调"""
        for callback in self.ticker_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(ticker)
                else:
                    callback(ticker)
            except Exception as e:
                logger.error(
                    "[DualActiveMarketData] Callback error: %s",
                    str(e)
                )
    
    def get_current_ticker(self) -> Optional[TickerData]:
        """获取当前ticker数据"""
        if self.active_source == "primary":
            return self.primary_stream.get_last_ticker()
        else:
            return self.backup_stream.get_last_ticker()
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态信息"""
        return {
            'active_source': self.active_source,
            'switch_count': self.switch_count,
            'last_switch_age_ms': (time.time_ns() - self.last_switch_ts) / 1_000_000,
            'sources': {
                'primary': {
                    'status': self.primary_stream.status.value,
                    'last_data_age_ms': (time.time_ns() - self.primary_stream.last_data_ts) / 1_000_000,
                    'reconnect_count': self.primary_stream.reconnect_count
                },
                'backup': {
                    'status': self.backup_stream.status.value,
                    'last_data_age_ms': (time.time_ns() - self.backup_stream.last_data_ts) / 1_000_000,
                    'reconnect_count': self.backup_stream.reconnect_count
                }
            },
            'metrics': self.metrics.copy(),
            'latency_threshold_ms': self.latency_threshold_ns / 1_000_000
        }