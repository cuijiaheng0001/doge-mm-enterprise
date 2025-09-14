#!/usr/bin/env python3
"""
Batch Replacer - 改价优先批量化处理器
实现Cancel/Replace操作的批量化和优先级管理
"""

import time
import asyncio
import threading
import logging
from typing import Dict, List, Optional, Tuple, Any, NamedTuple
from collections import defaultdict, deque
from enum import Enum
import heapq

logger = logging.getLogger(__name__)


class ReplaceAction(Enum):
    """替换操作类型"""
    CANCEL_REPLACE = "CANCEL_REPLACE"  # 撤销后重新下单
    REPLACE_IN_PLACE = "REPLACE_IN_PLACE"  # 原地改价
    BATCH_CANCEL = "BATCH_CANCEL"  # 批量撤销
    BATCH_CREATE = "BATCH_CREATE"  # 批量创建


class ReplacePriority(Enum):
    """替换优先级"""
    L0_CRITICAL = 0  # L0层关键订单，立即处理
    L1_HIGH = 1      # L1层高优先级
    L2_NORMAL = 2    # L2层正常优先级
    CLEANUP = 3      # 清理操作，低优先级


class ReplaceRequest(NamedTuple):
    """替换请求"""
    request_id: str
    priority: ReplacePriority
    action: ReplaceAction
    order_id: str
    symbol: str
    side: str
    new_price: Optional[float] = None
    new_quantity: Optional[float] = None
    layer: Optional[str] = None
    timestamp: Optional[float] = None
    timeout_ms: int = 5000  # 5秒超时
    callback: Optional[callable] = None
    raw_params: Optional[Dict] = None


class BatchResult(NamedTuple):
    """批次执行结果"""
    batch_id: str
    success_count: int
    fail_count: int
    total_latency_ms: float
    requests: List[ReplaceRequest]
    errors: List[str]


class BatchReplacer:
    """改价优先批量化处理器"""
    
    def __init__(self, batch_window_ms: int = 80, max_batch_size: int = 10, 
                 max_concurrent_batches: int = 3):
        """
        初始化批量处理器
        
        Args:
            batch_window_ms: 批处理窗口时间（毫秒）
            max_batch_size: 最大批处理大小
            max_concurrent_batches: 最大并发批次数
        """
        self.batch_window_ms = batch_window_ms
        self.max_batch_size = max_batch_size
        self.max_concurrent_batches = max_concurrent_batches
        
        # 请求队列 - 使用优先队列
        self.request_queue = []  # heapq
        self.queue_lock = threading.Lock()
        
        # 批次管理
        self.pending_batches = {}  # batch_id -> requests
        self.active_batches = {}   # batch_id -> asyncio.Task
        self.batch_counter = 0
        
        # 统计
        self.stats = {
            'total_requests': 0,
            'batched_requests': 0,
            'immediate_requests': 0,
            'successful_batches': 0,
            'failed_batches': 0,
            'avg_batch_size': 0.0,
            'avg_latency_ms': 0.0,
            'replace_ratio': 0.0  # replace vs cancel+new ratio
        }
        
        # 交易所连接器
        self.exchange = None
        self.running = False
        self.batch_processor_task = None
        
    def set_exchange(self, exchange):
        """设置交易所连接器"""
        self.exchange = exchange
        
    async def start(self):
        """启动批处理器"""
        if self.running:
            return
            
        self.running = True
        self.batch_processor_task = asyncio.create_task(self._batch_processor())
        logger.info("[BatchReplacer] 批处理器已启动")
        
    async def stop(self):
        """停止批处理器"""
        self.running = False
        
        if self.batch_processor_task:
            self.batch_processor_task.cancel()
            try:
                await self.batch_processor_task
            except asyncio.CancelledError:
                pass
                
        # 等待活跃批次完成
        if self.active_batches:
            await asyncio.gather(*self.active_batches.values(), return_exceptions=True)
            
        logger.info("[BatchReplacer] 批处理器已停止")
        
    async def submit_replace_request(self, request: ReplaceRequest) -> bool:
        """提交替换请求"""
        if not self.exchange:
            logger.error("[BatchReplacer] 交易所连接器未设置")
            return False
            
        with self.queue_lock:
            # 使用时间戳+优先级作为排序键
            timestamp = request.timestamp or time.time()
            priority_key = (request.priority.value, timestamp, self.stats['total_requests'])
            
            heapq.heappush(self.request_queue, (priority_key, request))
            self.stats['total_requests'] += 1
            
        logger.debug(f"[BatchReplacer] 请求已排队: {request.action.value} {request.order_id} "
                    f"优先级={request.priority.value}")
        return True
        
    async def _batch_processor(self):
        """批处理器主循环"""
        while self.running:
            try:
                # 收集批次
                batch = await self._collect_batch()
                
                if batch:
                    # 启动批次处理
                    batch_id = f"batch_{self.batch_counter}"
                    self.batch_counter += 1
                    
                    task = asyncio.create_task(self._execute_batch(batch_id, batch))
                    self.active_batches[batch_id] = task
                    
                    # 清理完成的任务
                    await self._cleanup_completed_batches()
                    
                await asyncio.sleep(0.01)  # 10ms检查间隔
                
            except Exception as e:
                logger.error(f"[BatchReplacer] 批处理器错误: {e}")
                await asyncio.sleep(0.1)
                
    async def _collect_batch(self) -> List[ReplaceRequest]:
        """收集一个批次的请求"""
        batch = []
        batch_start = time.time()
        
        while (len(batch) < self.max_batch_size and 
               (time.time() - batch_start) * 1000 < self.batch_window_ms):
            
            with self.queue_lock:
                if not self.request_queue:
                    break
                    
                # 检查是否有高优先级请求需要立即处理
                if self.request_queue and self.request_queue[0][1].priority == ReplacePriority.L0_CRITICAL:
                    _, request = heapq.heappop(self.request_queue)
                    batch.append(request)
                    break  # L0请求立即处理
                elif batch:  # 已有请求在批次中
                    _, request = heapq.heappop(self.request_queue)
                    batch.append(request)
                else:
                    # 等待更多请求聚集
                    pass
                    
            if not batch:
                await asyncio.sleep(0.005)  # 5ms等待
                
        return batch
        
    async def _execute_batch(self, batch_id: str, requests: List[ReplaceRequest]) -> BatchResult:
        """执行批次操作"""
        start_time = time.time()
        success_count = 0
        fail_count = 0
        errors = []
        
        try:
            logger.debug(f"[BatchReplacer] 执行批次 {batch_id}, {len(requests)} 个请求")
            
            # 按操作类型分组
            grouped_requests = defaultdict(list)
            for req in requests:
                grouped_requests[req.action].append(req)
                
            # 优先执行replace in place
            if ReplaceAction.REPLACE_IN_PLACE in grouped_requests:
                success, fail = await self._execute_replace_in_place_batch(
                    grouped_requests[ReplaceAction.REPLACE_IN_PLACE]
                )
                success_count += success
                fail_count += fail
                
            # 执行cancel+replace
            if ReplaceAction.CANCEL_REPLACE in grouped_requests:
                success, fail = await self._execute_cancel_replace_batch(
                    grouped_requests[ReplaceAction.CANCEL_REPLACE]
                )
                success_count += success
                fail_count += fail
                
            # 执行批量操作
            if ReplaceAction.BATCH_CANCEL in grouped_requests:
                success, fail = await self._execute_batch_cancel(
                    grouped_requests[ReplaceAction.BATCH_CANCEL]
                )
                success_count += success
                fail_count += fail
                
            if ReplaceAction.BATCH_CREATE in grouped_requests:
                success, fail = await self._execute_batch_create(
                    grouped_requests[ReplaceAction.BATCH_CREATE]
                )
                success_count += success
                fail_count += fail
                
        except Exception as e:
            logger.error(f"[BatchReplacer] 批次 {batch_id} 执行失败: {e}")
            errors.append(str(e))
            fail_count = len(requests)
            
        finally:
            # 清理活跃批次
            if batch_id in self.active_batches:
                del self.active_batches[batch_id]
                
        # 更新统计
        total_latency_ms = (time.time() - start_time) * 1000
        self.stats['batched_requests'] += len(requests)
        
        if success_count > 0:
            self.stats['successful_batches'] += 1
        if fail_count > 0:
            self.stats['failed_batches'] += 1
            
        # 更新平均值
        total_batches = self.stats['successful_batches'] + self.stats['failed_batches']
        if total_batches > 0:
            self.stats['avg_batch_size'] = self.stats['batched_requests'] / total_batches
            
        # 执行回调
        for req in requests:
            if req.callback:
                try:
                    await req.callback(success_count > 0)
                except Exception as e:
                    logger.error(f"[BatchReplacer] 回调执行失败: {e}")
                    
        result = BatchResult(
            batch_id=batch_id,
            success_count=success_count,
            fail_count=fail_count,
            total_latency_ms=total_latency_ms,
            requests=requests,
            errors=errors
        )
        
        logger.debug(f"[BatchReplacer] 批次 {batch_id} 完成: {success_count}成功 {fail_count}失败 "
                    f"延迟{total_latency_ms:.1f}ms")
        
        return result
        
    async def _execute_replace_in_place_batch(self, requests: List[ReplaceRequest]) -> Tuple[int, int]:
        """执行原地改价批次"""
        success = 0
        fail = 0
        
        # 优先尝试交易所原生的批量改价接口
        if hasattr(self.exchange, 'batch_replace_orders'):
            try:
                replace_params = []
                for req in requests:
                    params = {
                        'orderId': req.order_id,
                        'symbol': req.symbol,
                        'side': req.side,
                    }
                    if req.new_price is not None:
                        params['price'] = req.new_price
                    if req.new_quantity is not None:
                        params['quantity'] = req.new_quantity
                    replace_params.append(params)
                    
                results = await self.exchange.batch_replace_orders(replace_params)
                
                for result in results:
                    if result.get('status') == 'success':
                        success += 1
                    else:
                        fail += 1
                        
                self.stats['replace_ratio'] = (self.stats['replace_ratio'] * 0.9 + 
                                             success / len(requests) * 0.1)
                return success, fail
                
            except Exception as e:
                logger.debug(f"[BatchReplacer] 批量改价失败，回退到单个改价: {e}")
                
        # 回退到单个改价
        tasks = []
        for req in requests:
            task = asyncio.create_task(self._single_replace_in_place(req))
            tasks.append(task)
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                fail += 1
            elif result:
                success += 1
            else:
                fail += 1
                
        return success, fail
        
    async def _single_replace_in_place(self, req: ReplaceRequest) -> bool:
        """单个原地改价"""
        try:
            if hasattr(self.exchange, 'replace_order'):
                params = {
                    'orderId': req.order_id,
                    'symbol': req.symbol,
                    'side': req.side
                }
                if req.new_price is not None:
                    params['price'] = req.new_price
                if req.new_quantity is not None:
                    params['quantity'] = req.new_quantity
                    
                result = await self.exchange.replace_order(params)
                return result.get('status') == 'success'
            else:
                # 不支持replace，返回False让上层回退到cancel+new
                return False
                
        except Exception as e:
            logger.debug(f"[BatchReplacer] 改价失败 {req.order_id}: {e}")
            return False
            
    async def _execute_cancel_replace_batch(self, requests: List[ReplaceRequest]) -> Tuple[int, int]:
        """执行撤销+重新下单批次"""
        success = 0
        fail = 0
        
        # 第一阶段：批量撤销
        cancel_tasks = []
        for req in requests:
            task = asyncio.create_task(self._cancel_order(req))
            cancel_tasks.append(task)
            
        cancel_results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
        
        # 第二阶段：重新下单（只对撤销成功的）
        create_tasks = []
        successful_cancels = []
        
        for i, result in enumerate(cancel_results):
            if not isinstance(result, Exception) and result:
                successful_cancels.append(requests[i])
                
        # 短暂延迟确保撤销生效
        if successful_cancels:
            await asyncio.sleep(0.01)  # 10ms延迟
            
        for req in successful_cancels:
            task = asyncio.create_task(self._create_order(req))
            create_tasks.append(task)
            
        if create_tasks:
            create_results = await asyncio.gather(*create_tasks, return_exceptions=True)
            
            for result in create_results:
                if not isinstance(result, Exception) and result:
                    success += 1
                else:
                    fail += 1
        
        # 撤销失败的也算失败
        fail += len(requests) - len(successful_cancels)
        
        return success, fail
        
    async def _cancel_order(self, req: ReplaceRequest) -> bool:
        """撤销订单"""
        try:
            result = await self.exchange.cancel_order(req.order_id, req.symbol)
            return result.get('status') in ['CANCELED', 'success']
        except Exception as e:
            logger.debug(f"[BatchReplacer] 撤销失败 {req.order_id}: {e}")
            return False
            
    async def _create_order(self, req: ReplaceRequest) -> bool:
        """创建订单"""
        try:
            params = {
                'symbol': req.symbol,
                'side': req.side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': req.new_quantity,
                'price': req.new_price
            }
            
            if req.raw_params:
                params.update(req.raw_params)
                
            result = await self.exchange.create_order(**params)
            return result.get('status') in ['NEW', 'PARTIALLY_FILLED']
            
        except Exception as e:
            logger.debug(f"[BatchReplacer] 创建订单失败: {e}")
            return False
            
    async def _execute_batch_cancel(self, requests: List[ReplaceRequest]) -> Tuple[int, int]:
        """执行批量撤销"""
        if hasattr(self.exchange, 'cancel_all_orders'):
            # 按symbol分组
            symbol_groups = defaultdict(list)
            for req in requests:
                symbol_groups[req.symbol].append(req)
                
            success = 0
            fail = 0
            
            for symbol, reqs in symbol_groups.items():
                try:
                    result = await self.exchange.cancel_all_orders(symbol)
                    if result.get('status') == 'success':
                        success += len(reqs)
                    else:
                        fail += len(reqs)
                except Exception as e:
                    logger.debug(f"[BatchReplacer] 批量撤销失败 {symbol}: {e}")
                    fail += len(reqs)
                    
            return success, fail
        else:
            # 单个撤销
            tasks = [asyncio.create_task(self._cancel_order(req)) for req in requests]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success = sum(1 for r in results if not isinstance(r, Exception) and r)
            fail = len(requests) - success
            
            return success, fail
            
    async def _execute_batch_create(self, requests: List[ReplaceRequest]) -> Tuple[int, int]:
        """执行批量创建"""
        tasks = [asyncio.create_task(self._create_order(req)) for req in requests]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success = sum(1 for r in results if not isinstance(r, Exception) and r)
        fail = len(requests) - success
        
        return success, fail
        
    async def _cleanup_completed_batches(self):
        """清理已完成的批次"""
        completed = []
        for batch_id, task in self.active_batches.items():
            if task.done():
                completed.append(batch_id)
                
        for batch_id in completed:
            del self.active_batches[batch_id]
            
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'queue_size': len(self.request_queue),
            'active_batches': len(self.active_batches),
            'stats': self.stats.copy()
        }
        
    def get_summary(self) -> str:
        """获取状态摘要"""
        stats = self.get_stats()
        return (f"batch=[队列:{stats['queue_size']} 活跃:{stats['active_batches']}] "
               f"replace_ratio={stats['stats']['replace_ratio']:.1%} "
               f"avg_latency={stats['stats']['avg_latency_ms']:.1f}ms")


# 全局实例
_batch_replacer_instance = None
_batch_replacer_lock = threading.Lock()


def get_batch_replacer(batch_window_ms: int = 80) -> BatchReplacer:
    """获取全局批量替换器实例"""
    global _batch_replacer_instance
    
    with _batch_replacer_lock:
        if _batch_replacer_instance is None:
            _batch_replacer_instance = BatchReplacer(batch_window_ms)
            
        return _batch_replacer_instance


def reset_batch_replacer():
    """重置全局实例（用于测试）"""
    global _batch_replacer_instance
    with _batch_replacer_lock:
        _batch_replacer_instance = None


if __name__ == "__main__":
    # 简单测试
    async def test_batch_replacer():
        replacer = BatchReplacer(batch_window_ms=50, max_batch_size=5)
        
        # 模拟交易所
        class MockExchange:
            async def cancel_order(self, order_id, symbol):
                await asyncio.sleep(0.01)
                return {'status': 'CANCELED'}
                
            async def create_order(self, **params):
                await asyncio.sleep(0.01) 
                return {'status': 'NEW'}
                
        replacer.set_exchange(MockExchange())
        await replacer.start()
        
        # 提交测试请求
        requests = []
        for i in range(10):
            req = ReplaceRequest(
                request_id=f"req_{i}",
                priority=ReplacePriority.L1_HIGH,
                action=ReplaceAction.CANCEL_REPLACE,
                order_id=f"order_{i}",
                symbol="DOGEUSDT",
                side="BUY",
                new_price=0.1 + i * 0.001,
                new_quantity=100.0
            )
            requests.append(req)
            await replacer.submit_replace_request(req)
            
        # 等待处理完成
        await asyncio.sleep(1)
        
        print(f"统计: {replacer.get_stats()}")
        print(f"摘要: {replacer.get_summary()}")
        
        await replacer.stop()
        
    asyncio.run(test_batch_replacer())