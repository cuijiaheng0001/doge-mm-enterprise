"""
Hedge Router - 对冲路由器
负责执行对冲订单的路由、执行和回滚
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

from .planner_passive import PassiveLeg
from .planner_active import ActiveLeg

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    """执行状态"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class ExecutionResult:
    """执行结果"""
    order_id: str
    status: ExecutionStatus
    filled_qty: float
    avg_price: float
    remaining_qty: float
    fee: float
    rebate: float
    slippage_bps: float
    latency_ms: float
    venue: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_complete(self) -> bool:
        """是否完成"""
        return self.status in [ExecutionStatus.FILLED, ExecutionStatus.CANCELLED, 
                               ExecutionStatus.REJECTED, ExecutionStatus.FAILED]
    
    @property
    def fill_rate(self) -> float:
        """成交率"""
        total_qty = self.filled_qty + self.remaining_qty
        return self.filled_qty / total_qty if total_qty > 0 else 0


@dataclass
class HedgeReport:
    """对冲报告"""
    ts: float
    total_qty_target: float
    total_qty_filled: float
    passive_qty_filled: float
    active_qty_filled: float
    avg_price: float
    total_fee: float
    total_rebate: float
    avg_slippage_bps: float
    avg_latency_ms: float
    execution_results: List[ExecutionResult]
    success: bool
    error_msg: Optional[str] = None
    
    @property
    def fill_rate(self) -> float:
        """总成交率"""
        return self.total_qty_filled / self.total_qty_target if self.total_qty_target > 0 else 0
    
    @property
    def net_cost_bps(self) -> float:
        """净成本（基点）"""
        if self.total_qty_filled == 0:
            return 0
        net_fee = self.total_fee - self.total_rebate
        notional = self.total_qty_filled * self.avg_price
        return (net_fee / notional) * 10000 if notional > 0 else 0


class HedgeRouter:
    """
    对冲路由器 - FAHE执行组件
    负责路由和执行对冲订单
    """
    
    def __init__(self,
                 max_retry: int = 3,
                 retry_delay_ms: int = 100,
                 timeout_ms: int = 5000,
                 enable_rollback: bool = True):
        """
        初始化对冲路由器
        
        Args:
            max_retry: 最大重试次数
            retry_delay_ms: 重试延迟
            timeout_ms: 超时时间
            enable_rollback: 是否启用回滚
        """
        self.max_retry = max_retry
        self.retry_delay_ms = retry_delay_ms
        self.timeout_ms = timeout_ms
        self.enable_rollback = enable_rollback
        
        # 连接器映射
        self.connectors = {}  # venue -> connector
        
        # 执行队列
        self.execution_queue = asyncio.Queue()
        
        # 订单跟踪
        self.active_orders = {}  # order_id -> order_info
        self.order_history = []
        
        # 统计信息
        self.stats = {
            'total_executions': 0,
            'successful_executions': 0,
            'failed_executions': 0,
            'total_qty_routed': 0.0,
            'total_qty_filled': 0.0,
            'avg_fill_rate': 0.0,
            'rollback_count': 0
        }
        
        # 成本模型缓存
        self.cost_cache = {}
        
        logger.info(f"[HedgeRouter] 初始化完成: retry={max_retry}, timeout={timeout_ms}ms")
    
    async def exec(self, legs: List[Union[PassiveLeg, ActiveLeg]]) -> HedgeReport:
        """
        执行对冲订单
        
        Args:
            legs: 订单腿列表（被动或主动）
        
        Returns:
            对冲执行报告
        """
        start_ts = time.time()
        execution_results = []
        rollback_orders = []
        
        try:
            # 按优先级排序
            sorted_legs = self._sort_by_priority(legs)
            
            # 计算总目标量
            total_qty_target = sum(self._get_leg_qty(leg) for leg in sorted_legs)
            
            # 执行每个腿
            for leg in sorted_legs:
                try:
                    # 选择最优路由
                    best_route = await self._select_best_route(leg)
                    
                    # 执行订单
                    result = await self._execute_leg(leg, best_route)
                    execution_results.append(result)
                    
                    # 如果需要回滚，记录订单
                    if self.enable_rollback and result.status == ExecutionStatus.FILLED:
                        rollback_orders.append((result.order_id, result.venue))
                    
                    # 检查是否需要停止
                    if self._should_stop_execution(execution_results, total_qty_target):
                        logger.info("[HedgeRouter] 达到目标，停止执行")
                        break
                        
                except Exception as e:
                    logger.error(f"[HedgeRouter] 腿执行失败: {e}")
                    
                    # 失败处理
                    if self.enable_rollback and rollback_orders:
                        await self._rollback_orders(rollback_orders)
                    
                    # 创建失败结果
                    failed_result = ExecutionResult(
                        order_id="FAILED",
                        status=ExecutionStatus.FAILED,
                        filled_qty=0,
                        avg_price=0,
                        remaining_qty=self._get_leg_qty(leg),
                        fee=0,
                        rebate=0,
                        slippage_bps=0,
                        latency_ms=(time.time() - start_ts) * 1000,
                        venue="",
                        metadata={'error': str(e)}
                    )
                    execution_results.append(failed_result)
            
            # 生成报告
            report = self._generate_report(execution_results, total_qty_target, start_ts)
            
            # 更新统计
            self._update_stats(report)
            
            return report
            
        except Exception as e:
            logger.error(f"[HedgeRouter] 执行异常: {e}")
            
            # 回滚所有订单
            if self.enable_rollback and rollback_orders:
                await self._rollback_orders(rollback_orders)
            
            # 返回失败报告
            return HedgeReport(
                ts=start_ts,
                total_qty_target=total_qty_target if 'total_qty_target' in locals() else 0,
                total_qty_filled=0,
                passive_qty_filled=0,
                active_qty_filled=0,
                avg_price=0,
                total_fee=0,
                total_rebate=0,
                avg_slippage_bps=0,
                avg_latency_ms=(time.time() - start_ts) * 1000,
                execution_results=execution_results,
                success=False,
                error_msg=str(e)
            )
    
    def _sort_by_priority(self, legs: List[Union[PassiveLeg, ActiveLeg]]) -> List[Union[PassiveLeg, ActiveLeg]]:
        """
        按优先级排序
        
        Args:
            legs: 订单腿列表
        
        Returns:
            排序后的列表
        """
        def get_priority(leg):
            if isinstance(leg, ActiveLeg):
                return leg.priority
            else:
                # PassiveLeg默认优先级较低
                return 5
        
        return sorted(legs, key=get_priority)
    
    def _get_leg_qty(self, leg: Union[PassiveLeg, ActiveLeg]) -> float:
        """
        获取腿的数量
        
        Args:
            leg: 订单腿
        
        Returns:
            数量
        """
        return leg.qty
    
    async def _select_best_route(self, leg: Union[PassiveLeg, ActiveLeg]) -> Dict[str, Any]:
        """
        选择最优路由
        
        Args:
            leg: 订单腿
        
        Returns:
            路由信息
        """
        # 获取场所
        if isinstance(leg, PassiveLeg):
            venue = leg.venue.value if hasattr(leg.venue, 'value') else str(leg.venue)
        else:
            venue = leg.venue
        
        # 检查连接器
        if venue not in self.connectors:
            # 使用默认连接器
            venue = "BINANCE_USDT"
        
        # 估算成本
        estimated_cost = await self._estimate_execution_cost(leg, venue)
        
        return {
            'venue': venue,
            'connector': self.connectors.get(venue),
            'estimated_cost': estimated_cost,
            'routing_ts': time.time()
        }
    
    async def _execute_leg(self, leg: Union[PassiveLeg, ActiveLeg], route: Dict[str, Any]) -> ExecutionResult:
        """
        执行单个腿
        
        Args:
            leg: 订单腿
            route: 路由信息
        
        Returns:
            执行结果
        """
        start_ts = time.time()
        venue = route['venue']
        
        # 模拟执行（实际需要连接真实交易所）
        await asyncio.sleep(0.01)  # 模拟网络延迟
        
        # 根据腿类型执行
        if isinstance(leg, PassiveLeg):
            result = await self._execute_passive_leg(leg, venue, start_ts)
        else:
            result = await self._execute_active_leg(leg, venue, start_ts)
        
        return result
    
    async def _execute_passive_leg(self, leg: PassiveLeg, venue: str, start_ts: float) -> ExecutionResult:
        """
        执行被动腿
        
        Args:
            leg: 被动腿
            venue: 交易场所
            start_ts: 开始时间
        
        Returns:
            执行结果
        """
        # 模拟被动订单执行
        # 实际需要调用交易所API
        
        # 模拟成交概率
        fill_prob = leg.metadata.get('fill_prob_estimate', 0.7) if leg.metadata else 0.7
        is_filled = time.time() % 1 < fill_prob  # 简单随机
        
        if is_filled:
            filled_qty = leg.qty
            remaining_qty = 0
            status = ExecutionStatus.FILLED
            
            # 计算返佣
            rebate = abs(leg.metadata.get('expected_rebate_bps', 3)) * leg.qty * 0.25 / 10000 if leg.metadata else 0
            fee = 0
        else:
            filled_qty = leg.qty * 0.3  # 部分成交
            remaining_qty = leg.qty - filled_qty
            status = ExecutionStatus.PARTIALLY_FILLED
            rebate = abs(leg.metadata.get('expected_rebate_bps', 3)) * filled_qty * 0.25 / 10000 if leg.metadata else 0
            fee = 0
        
        return ExecutionResult(
            order_id=f"P_{int(time.time()*1000)}",
            status=status,
            filled_qty=filled_qty,
            avg_price=0.25,  # 模拟价格
            remaining_qty=remaining_qty,
            fee=fee,
            rebate=rebate,
            slippage_bps=0,  # Maker没有滑点
            latency_ms=(time.time() - start_ts) * 1000,
            venue=venue,
            metadata={'leg_type': 'passive', 'ttl_ms': leg.ttl_ms}
        )
    
    async def _execute_active_leg(self, leg: ActiveLeg, venue: str, start_ts: float) -> ExecutionResult:
        """
        执行主动腿
        
        Args:
            leg: 主动腿
            venue: 交易场所
            start_ts: 开始时间
        
        Returns:
            执行结果
        """
        # 模拟主动订单执行
        # 实际需要调用交易所API
        
        # IOC订单通常立即成交或取消
        fill_rate = 0.95 if leg.is_aggressive else 0.85
        filled_qty = leg.qty * fill_rate
        remaining_qty = leg.qty - filled_qty
        
        # 计算费用和滑点
        fee = 0.0004 * filled_qty * 0.25  # 0.04% taker费
        slippage_bps = leg.metadata.get('estimated_slippage_bps', 2) if leg.metadata else 2
        
        return ExecutionResult(
            order_id=f"A_{int(time.time()*1000)}",
            status=ExecutionStatus.FILLED if fill_rate > 0.95 else ExecutionStatus.PARTIALLY_FILLED,
            filled_qty=filled_qty,
            avg_price=0.25 * (1 + slippage_bps/10000),  # 包含滑点
            remaining_qty=remaining_qty,
            fee=fee,
            rebate=0,
            slippage_bps=slippage_bps,
            latency_ms=(time.time() - start_ts) * 1000,
            venue=venue,
            metadata={'leg_type': 'active', 'execution_type': leg.execution_type.value}
        )
    
    async def _estimate_execution_cost(self, leg: Union[PassiveLeg, ActiveLeg], venue: str) -> float:
        """
        估算执行成本
        
        Args:
            leg: 订单腿
            venue: 交易场所
        
        Returns:
            预估成本（基点）
        """
        # 缓存键
        cache_key = f"{venue}_{type(leg).__name__}_{leg.qty}"
        
        if cache_key in self.cost_cache:
            return self.cost_cache[cache_key]
        
        # 计算成本
        if isinstance(leg, PassiveLeg):
            # Maker返佣
            cost = -3 if venue == "BINANCE_USDT" else -6  # 负值表示返佣
        else:
            # Taker费用 + 滑点
            cost = 4 + leg.max_slippage_bps
        
        # 缓存结果
        self.cost_cache[cache_key] = cost
        
        return cost
    
    def _should_stop_execution(self, results: List[ExecutionResult], target_qty: float) -> bool:
        """
        判断是否应该停止执行
        
        Args:
            results: 执行结果列表
            target_qty: 目标数量
        
        Returns:
            是否停止
        """
        total_filled = sum(r.filled_qty for r in results)
        return total_filled >= target_qty * 0.95  # 达到95%即可
    
    async def _rollback_orders(self, orders: List[Tuple[str, str]]) -> None:
        """
        回滚订单
        
        Args:
            orders: [(order_id, venue)]列表
        """
        logger.warning(f"[HedgeRouter] 回滚{len(orders)}个订单")
        
        for order_id, venue in orders:
            try:
                # 实际需要调用交易所取消订单API
                await asyncio.sleep(0.01)  # 模拟
                logger.info(f"[HedgeRouter] 回滚订单: {order_id} @ {venue}")
            except Exception as e:
                logger.error(f"[HedgeRouter] 回滚失败: {order_id} - {e}")
        
        self.stats['rollback_count'] += len(orders)
    
    def _generate_report(self, results: List[ExecutionResult], target_qty: float, start_ts: float) -> HedgeReport:
        """
        生成执行报告
        
        Args:
            results: 执行结果列表
            target_qty: 目标数量
            start_ts: 开始时间
        
        Returns:
            对冲报告
        """
        # 统计数据
        total_filled = sum(r.filled_qty for r in results)
        passive_filled = sum(r.filled_qty for r in results if r.metadata.get('leg_type') == 'passive')
        active_filled = sum(r.filled_qty for r in results if r.metadata.get('leg_type') == 'active')
        
        # 计算平均价格
        total_notional = sum(r.filled_qty * r.avg_price for r in results)
        avg_price = total_notional / total_filled if total_filled > 0 else 0
        
        # 计算费用
        total_fee = sum(r.fee for r in results)
        total_rebate = sum(r.rebate for r in results)
        
        # 计算平均滑点
        slippages = [r.slippage_bps for r in results if r.slippage_bps > 0]
        avg_slippage = sum(slippages) / len(slippages) if slippages else 0
        
        # 计算平均延迟
        latencies = [r.latency_ms for r in results]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        
        # 判断成功
        success = total_filled >= target_qty * 0.8  # 80%以上算成功
        
        return HedgeReport(
            ts=start_ts,
            total_qty_target=target_qty,
            total_qty_filled=total_filled,
            passive_qty_filled=passive_filled,
            active_qty_filled=active_filled,
            avg_price=avg_price,
            total_fee=total_fee,
            total_rebate=total_rebate,
            avg_slippage_bps=avg_slippage,
            avg_latency_ms=avg_latency,
            execution_results=results,
            success=success,
            error_msg=None if success else f"仅完成{total_filled/target_qty*100:.1f}%"
        )
    
    def _update_stats(self, report: HedgeReport) -> None:
        """
        更新统计信息
        
        Args:
            report: 对冲报告
        """
        self.stats['total_executions'] += 1
        
        if report.success:
            self.stats['successful_executions'] += 1
        else:
            self.stats['failed_executions'] += 1
        
        self.stats['total_qty_routed'] += report.total_qty_target
        self.stats['total_qty_filled'] += report.total_qty_filled
        
        # 更新平均成交率
        alpha = 0.1
        self.stats['avg_fill_rate'] = \
            (1 - alpha) * self.stats['avg_fill_rate'] + alpha * report.fill_rate
    
    def register_connector(self, venue: str, connector: Any) -> None:
        """
        注册连接器
        
        Args:
            venue: 交易场所
            connector: 连接器实例
        """
        self.connectors[venue] = connector
        logger.info(f"[HedgeRouter] 注册连接器: {venue}")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'success_rate': self.stats['successful_executions'] / self.stats['total_executions'] 
                           if self.stats['total_executions'] > 0 else 0,
            'active_venues': list(self.connectors.keys())
        }