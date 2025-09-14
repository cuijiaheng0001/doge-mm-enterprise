"""
Hedge Service - FAHE主服务
集成所有对冲组件的核心服务
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum

from .delta_bus import DeltaBus, DeltaEvent
from .position_book import PositionBook
from .mode_controller import ModeController, MarketSignals
from .planner_passive import PassivePlanner
from .planner_active import ActivePlanner
from .router import HedgeRouter
from .governor import HedgeGovernor, BudgetType
from ..connectors.perp_binance import PerpBinanceConnector, PerpOrder, OrderSide, OrderType, TimeInForce

logger = logging.getLogger(__name__)


class ServiceStatus(Enum):
    """服务状态"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class HedgeConfig:
    """对冲配置"""
    # API配置
    api_key: str
    api_secret: str
    testnet: bool = False
    
    # 对冲参数
    bandwidth: float = 150  # 目标带宽（DOGE）
    deadband: float = 40  # 死区（DOGE）
    max_delta_error: float = 30  # 最大Delta误差
    
    # 预算参数
    fill_budget: int = 12
    reprice_budget: int = 12
    cancel_budget: int = 40
    
    # 模式控制参数
    target_usage_pct: float = 0.07
    safe_usage_pct: float = 0.15
    
    # 执行参数
    single_order_limit: float = 5000  # 单笔订单上限（USDT）
    max_slippage_bps: float = 5  # 最大滑点
    
    # 监控参数
    heartbeat_interval: int = 5  # 心跳间隔（秒）
    stats_interval: int = 30  # 统计输出间隔（秒）


class HedgeService:
    """
    FAHE对冲服务 - 核心集成服务
    H-MVP最小闭环实现
    """
    
    def __init__(self, config: HedgeConfig):
        """
        初始化对冲服务
        
        Args:
            config: 对冲配置
        """
        self.config = config
        self.status = ServiceStatus.STOPPED
        
        # 核心组件
        self.delta_bus = DeltaBus()
        self.position_book = PositionBook(
            bandwidth=config.bandwidth,
            deadband=config.deadband,
            max_delta_error=config.max_delta_error
        )
        self.mode_controller = ModeController()
        self.passive_planner = PassivePlanner()
        self.active_planner = ActivePlanner(
            single_order_limit=config.single_order_limit,
            max_slippage_bps=config.max_slippage_bps
        )
        self.hedge_router = HedgeRouter()
        self.hedge_governor = HedgeGovernor(
            fill_budget=config.fill_budget,
            reprice_budget=config.reprice_budget,
            cancel_budget=config.cancel_budget,
            target_usage_pct=config.target_usage_pct,
            safe_usage_pct=config.safe_usage_pct
        )
        
        # 永续合约连接器
        self.perp_connector = PerpBinanceConnector(
            api_key=config.api_key,
            api_secret=config.api_secret,
            testnet=config.testnet
        )
        
        # 注册连接器到路由器
        self.hedge_router.register_connector("BINANCE_USDT", self.perp_connector)
        
        # 订阅Delta事件
        self.delta_bus.subscribe(self.on_delta_event)
        
        # 设置永续合约成交回调
        self.perp_connector.set_fill_callback(self.on_perp_fill)
        
        # 异步任务
        self.tasks = []
        
        # 统计信息
        self.stats = {
            'service_start_ts': 0,
            'total_hedge_events': 0,
            'successful_hedges': 0,
            'failed_hedges': 0,
            'avg_hedge_latency_ms': 0,
            'total_pnl': 0.0
        }
        
        # 市场数据缓存
        self.market_data = {
            'mid_price': 0.0,
            'bid': 0.0,
            'ask': 0.0,
            'spread_bps': 0.0,
            'volatility_30s': 0.001,
            'queue_depth': 1000,
            'queue_toxicity': 0.3,
            'last_update_ts': 0
        }
        
        logger.info(f"[HedgeService] 初始化完成: bw={config.bandwidth}, db={config.deadband}")
    
    async def start(self) -> None:
        """
        启动对冲服务
        """
        if self.status != ServiceStatus.STOPPED:
            logger.warning(f"[HedgeService] 服务状态不正确: {self.status}")
            return
        
        self.status = ServiceStatus.STARTING
        logger.info("[HedgeService] 启动中...")
        
        try:
            # 启动Delta Bus
            await self.delta_bus.start()
            
            # 启动永续合约连接器
            await self.perp_connector.start()
            
            # 启动监控任务
            self.tasks.append(asyncio.create_task(self._heartbeat_loop()))
            self.tasks.append(asyncio.create_task(self._stats_loop()))
            self.tasks.append(asyncio.create_task(self._market_data_loop()))
            
            # 更新统计
            self.stats['service_start_ts'] = time.time()
            
            self.status = ServiceStatus.RUNNING
            logger.info("[HedgeService] ✅ 服务启动成功")
            
        except Exception as e:
            self.status = ServiceStatus.ERROR
            logger.error(f"[HedgeService] 启动失败: {e}")
            raise
    
    async def stop(self) -> None:
        """
        停止对冲服务
        """
        if self.status != ServiceStatus.RUNNING:
            return
        
        self.status = ServiceStatus.STOPPING
        logger.info("[HedgeService] 停止中...")
        
        # 取消所有任务
        for task in self.tasks:
            task.cancel()
        
        # 等待任务结束
        await asyncio.gather(*self.tasks, return_exceptions=True)
        
        # 停止组件
        await self.delta_bus.stop()
        await self.perp_connector.stop()
        
        self.status = ServiceStatus.STOPPED
        logger.info("[HedgeService] 服务已停止")
    
    async def on_delta_event(self, event: DeltaEvent) -> None:
        """
        处理Delta事件（核心对冲逻辑）
        
        Args:
            event: Delta事件
        """
        start_ts = time.time()
        
        try:
            logger.info(f"[HedgeService] 收到Delta事件: {event.event_type.value} "
                       f"delta={event.delta_change:.2f}")
            
            # 更新持仓簿
            if event.event_type.value == "spot_fill":
                self.position_book.on_spot_fill(
                    side=event.side,
                    qty=event.qty,
                    px=event.px,
                    ts=event.ts
                )
            elif event.event_type.value == "perp_fill":
                self.position_book.on_perp_fill(
                    side=event.side,
                    qty=event.qty,
                    px=event.px,
                    ts=event.ts
                )
            
            # 检查是否需要对冲
            if not self.position_book.is_hedge_needed():
                logger.debug("[HedgeService] 无需对冲")
                return
            
            # 获取对冲需求
            side, qty = self.position_book.get_hedge_requirement()
            
            if qty < 10:  # 最小对冲量
                logger.debug(f"[HedgeService] 对冲量过小: {qty:.2f}")
                return
            
            logger.info(f"[HedgeService] 对冲需求: {side} {qty:.2f} DOGE")
            
            # 执行对冲
            await self._execute_hedge(side, qty)
            
            # 更新统计
            self.stats['total_hedge_events'] += 1
            latency_ms = (time.time() - start_ts) * 1000
            alpha = 0.1
            self.stats['avg_hedge_latency_ms'] = \
                (1 - alpha) * self.stats['avg_hedge_latency_ms'] + alpha * latency_ms
            
        except Exception as e:
            logger.error(f"[HedgeService] Delta事件处理失败: {e}")
            self.stats['failed_hedges'] += 1
    
    async def _execute_hedge(self, side: str, qty: float) -> None:
        """
        执行对冲（H-MVP最小闭环）
        
        Args:
            side: 方向（BUY/SELL）
            qty: 数量
        """
        try:
            # 1. 获取市场信号
            signals = await self._get_market_signals()
            
            # 2. 计算模式权重
            w_passive = self.mode_controller.mode_weights(signals, qty)
            logger.info(f"[HedgeService] 模式权重: w_passive={w_passive:.3f}")
            
            # 3. 拆分数量
            passive_qty, active_qty = self.mode_controller.split_hedge_quantity(qty, w_passive)
            
            # 4. 生成执行计划
            legs = []
            
            # H-MVP: 先只做Active-IOC路径
            if active_qty > 0:
                # 检查预算
                lease_id = self.hedge_governor.try_acquire(BudgetType.HEDGE_FILL, 1)
                
                if lease_id:
                    try:
                        # 生成主动腿计划
                        active_legs = self.active_planner.plan(
                            side=side,
                            qty=active_qty,
                            market_data=self.market_data
                        )
                        legs.extend(active_legs)
                        
                        # 提交租约
                        self.hedge_governor.commit_lease(lease_id)
                    except Exception as e:
                        # 回滚租约
                        self.hedge_governor.rollback_lease(lease_id)
                        raise
                else:
                    logger.warning("[HedgeService] 预算不足，跳过对冲")
                    return
            
            # TODO: 后续加入Passive-Maker路径
            # if passive_qty > 0:
            #     passive_legs = self.passive_planner.plan(...)
            #     legs.extend(passive_legs)
            
            if not legs:
                logger.warning("[HedgeService] 无有效执行计划")
                return
            
            # 5. 执行对冲（简化版：直接下IOC订单）
            for leg in legs[:1]:  # H-MVP: 先只执行第一个腿
                order = PerpOrder(
                    symbol="DOGEUSDT",
                    side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=active_qty,
                    price=self.market_data['ask'] * 1.01 if side == "BUY" else self.market_data['bid'] * 0.99,
                    time_in_force=TimeInForce.IOC
                )
                
                result = await self.perp_connector.place_order(order)
                
                logger.info(f"[HedgeService] 对冲订单已下: {result.get('orderId')}")
                self.stats['successful_hedges'] += 1
            
        except Exception as e:
            logger.error(f"[HedgeService] 对冲执行失败: {e}")
            self.stats['failed_hedges'] += 1
    
    async def _get_market_signals(self) -> MarketSignals:
        """
        获取市场信号
        
        Returns:
            市场信号
        """
        # 获取最新订单簿
        orderbook = await self.perp_connector.get_orderbook()
        
        # 更新市场数据
        if orderbook['bids'] and orderbook['asks']:
            self.market_data['bid'] = orderbook['bids'][0][0]
            self.market_data['ask'] = orderbook['asks'][0][0]
            self.market_data['mid_price'] = (self.market_data['bid'] + self.market_data['ask']) / 2
            self.market_data['spread_bps'] = (self.market_data['ask'] - self.market_data['bid']) / self.market_data['mid_price'] * 10000
            self.market_data['last_update_ts'] = time.time()
        
        # 构建信号
        return MarketSignals(
            lambda_delta=1.0,  # TODO: 计算真实到达频率
            sigma_30s=self.market_data['volatility_30s'],
            queue_toxicity=self.market_data['queue_toxicity'],
            funding_pred=0.0001,  # TODO: 获取真实资金费率
            maker_rebate=-0.0003,  # -0.03%
            spread_bps=self.market_data['spread_bps'],
            queue_depth=self.market_data['queue_depth'],
            market_impact=0.001,
            ts=time.time()
        )
    
    async def on_perp_fill(self, fill_data: Dict[str, Any]) -> None:
        """
        处理永续合约成交
        
        Args:
            fill_data: 成交数据
        """
        logger.info(f"[HedgeService] 永续合约成交: {fill_data}")
        
        # 发布到Delta Bus
        self.delta_bus.publish_perp_fill(
            symbol="DOGEUSDT",
            side=fill_data['side'],
            qty=fill_data['qty'],
            px=fill_data['price']
        )
    
    def publish_spot_fill(self, side: str, qty: float, px: float) -> None:
        """
        发布现货成交（供外部调用）
        
        Args:
            side: 方向
            qty: 数量
            px: 价格
        """
        self.delta_bus.publish_spot_fill(
            symbol="DOGEUSDT",
            side=side,
            qty=qty,
            px=px
        )
    
    async def _heartbeat_loop(self) -> None:
        """
        心跳循环
        """
        while self.status == ServiceStatus.RUNNING:
            try:
                # 更新持仓
                await self.perp_connector.update_position()
                
                # 验证持仓
                is_valid, msg = self.position_book.validate_position()
                if not is_valid:
                    logger.warning(f"[HedgeService] 持仓验证失败: {msg}")
                
                # 输出心跳
                logger.debug(f"[HedgeService] 心跳: delta_total={self.position_book.delta_total:.2f}")
                
                await asyncio.sleep(self.config.heartbeat_interval)
                
            except Exception as e:
                logger.error(f"[HedgeService] 心跳异常: {e}")
                await asyncio.sleep(1)
    
    async def _stats_loop(self) -> None:
        """
        统计输出循环
        """
        while self.status == ServiceStatus.RUNNING:
            try:
                await asyncio.sleep(self.config.stats_interval)
                
                # 收集统计
                stats = {
                    'service': self.stats,
                    'position': self.position_book.get_stats(),
                    'governor': self.hedge_governor.get_stats(),
                    'router': self.hedge_router.get_stats(),
                    'connector': self.perp_connector.get_stats()
                }
                
                # 输出关键指标
                logger.info(f"[HedgeService] 统计摘要:")
                logger.info(f"  - Delta: spot={stats['position']['delta_spot']:.2f}, "
                           f"perp={stats['position']['delta_perp']:.2f}, "
                           f"total={stats['position']['delta_total']:.2f}")
                logger.info(f"  - 对冲: events={stats['service']['total_hedge_events']}, "
                           f"success={stats['service']['successful_hedges']}, "
                           f"latency={stats['service']['avg_hedge_latency_ms']:.1f}ms")
                logger.info(f"  - 预算: usage={stats['governor']['current_usage_pct']:.1%}, "
                           f"approval={stats['governor']['approval_rate']:.1%}")
                
            except Exception as e:
                logger.error(f"[HedgeService] 统计输出异常: {e}")
    
    async def _market_data_loop(self) -> None:
        """
        市场数据更新循环
        """
        while self.status == ServiceStatus.RUNNING:
            try:
                # 更新订单簿
                await self.perp_connector.get_orderbook()
                
                # TODO: 计算更多市场指标
                # - 波动率
                # - 队列毒性
                # - 资金费率
                
                await asyncio.sleep(1)  # 1秒更新一次
                
            except Exception as e:
                logger.error(f"[HedgeService] 市场数据更新异常: {e}")
                await asyncio.sleep(5)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取服务统计
        
        Returns:
            统计数据
        """
        return {
            'status': self.status.value,
            'uptime': time.time() - self.stats['service_start_ts'] if self.stats['service_start_ts'] > 0 else 0,
            **self.stats,
            'position': self.position_book.get_stats(),
            'governor': self.hedge_governor.get_stats()
        }


async def run_hedge_service(config: HedgeConfig) -> None:
    """
    运行对冲服务
    
    Args:
        config: 对冲配置
    """
    service = HedgeService(config)
    
    try:
        await service.start()
        
        # 保持运行
        while service.status == ServiceStatus.RUNNING:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("收到停止信号")
    finally:
        await service.stop()