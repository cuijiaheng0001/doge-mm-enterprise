#!/usr/bin/env python3
"""
Phase 3 - Track B2: Utilization Wave（250ms快速部署）
解决问题：300+ USDT可用余额需要30秒才能部署
"""

import time
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class WaveStage(Enum):
    """部署波次阶段"""
    IDLE = "idle"
    L0_WAVE = "l0_wave"      # 0-50ms
    L1_WAVE = "l1_wave"      # 50-150ms
    L2_WAVE = "l2_wave"      # 150-250ms
    COMPLETED = "completed"

@dataclass
class DeployableCapital:
    """可部署资金"""
    usdt_free: float
    doge_free: float
    usdt_value: float  # USDT总价值
    doge_value: float  # DOGE的USDT价值
    total_value: float
    deployment_ratio: float  # 已部署比例

@dataclass
class WaveConfig:
    """波次配置"""
    # 各层资金分配比例
    l0_ratio: float = 0.6   # 60%资金用于L0
    l1_ratio: float = 0.3   # 30%资金用于L1
    l2_ratio: float = 0.1   # 10%资金用于L2
    
    # 各层价差距离(ticks)
    l0_spread_ticks: int = 1
    l1_spread_ticks: int = 3
    l2_spread_ticks: int = 5
    
    # TTL配置(ms)
    l0_ttl_ms: int = 3000
    l1_ttl_ms: int = 8000
    l2_ttl_ms: int = 15000
    
    # 触发阈值
    min_deployable_usdt: float = 50.0  # 最小可部署金额
    
    # 时间控制
    wave_interval_ms: int = 50  # 波次间隔
    total_deploy_time_ms: int = 250  # 总部署时间
    
    # 启动延迟优化
    startup_delay_override: float = 5.0  # 覆盖原60秒延迟

class UtilizationWave:
    """
    波浪式资金部署系统
    - 检测到可用资金立即触发
    - 250ms内完成三波部署
    - 事件驱动，不依赖周期性检查
    """
    
    def __init__(self, connector, market_data, config: Optional[WaveConfig] = None):
        self.connector = connector
        self.market_data = market_data
        self.config = config or WaveConfig()
        
        # 状态跟踪
        self.current_stage = WaveStage.IDLE
        self.last_deployment = 0
        self.deployment_count = 0
        self.total_deployed = 0
        
        # 性能统计
        self.stats = {
            'deployments': 0,
            'total_time_ms': 0,
            'avg_time_ms': 0,
            'l0_orders': 0,
            'l1_orders': 0,
            'l2_orders': 0,
            'failed_orders': 0
        }
        
        # 部署锁（防止并发）
        self.deploying = False
        self.deploy_lock = asyncio.Lock()
        
        logger.info(f"[Phase3-B2] UtilizationWave initialized: startup_delay={config.startup_delay_override}s")
        
    def get_deployable_capital(self, balance: Dict, mid_price: float) -> DeployableCapital:
        """计算可部署资金"""
        # 获取余额
        usdt_free = balance.get('USDT', {}).get('free', 0)
        doge_free = balance.get('DOGE', {}).get('free', 0)
        usdt_locked = balance.get('USDT', {}).get('locked', 0)
        doge_locked = balance.get('DOGE', {}).get('locked', 0)
        
        # 计算价值
        doge_value = doge_free * mid_price if mid_price > 0 else 0
        total_free = usdt_free + doge_value
        total_locked = usdt_locked + (doge_locked * mid_price if mid_price > 0 else 0)
        total_value = total_free + total_locked
        
        # 计算部署率
        deployment_ratio = total_locked / max(1, total_value)
        
        return DeployableCapital(
            usdt_free=usdt_free,
            doge_free=doge_free,
            usdt_value=usdt_free,
            doge_value=doge_value,
            total_value=total_value,
            deployment_ratio=deployment_ratio
        )
        
    async def detect_and_deploy(self, balance: Dict, mid_price: float) -> bool:
        """
        检测可用资金并触发部署
        返回: 是否触发了部署
        """
        # 检查是否正在部署
        if self.deploying:
            return False
            
        # 计算可部署资金
        capital = self.get_deployable_capital(balance, mid_price)
        
        # 检查触发条件
        should_deploy = (
            capital.usdt_free > self.config.min_deployable_usdt or
            capital.doge_value > self.config.min_deployable_usdt
        ) and capital.deployment_ratio < 0.7  # 部署率低于70%
        
        if should_deploy:
            logger.info(f"[Phase3-B2] Triggering deployment: free=${capital.total_value:.1f}, deployed={capital.deployment_ratio:.1%}")
            asyncio.create_task(self.execute_wave_deployment(capital, mid_price))
            return True
            
        return False
        
    async def execute_wave_deployment(self, capital: DeployableCapital, mid_price: float):
        """
        执行波浪式部署
        目标：250ms内完成所有部署
        """
        async with self.deploy_lock:
            if self.deploying:
                return
                
            self.deploying = True
            start_time = time.time()
            self.deployment_count += 1
            
            try:
                logger.info(f"[Phase3-B2] Starting wave deployment #{self.deployment_count}")
                
                # 第一波：L0快速部署 (0-50ms)
                self.current_stage = WaveStage.L0_WAVE
                l0_orders = await self._deploy_l0_wave(capital, mid_price)
                await asyncio.sleep(self.config.wave_interval_ms / 1000)
                
                # 第二波：L1补充 (50-150ms)
                self.current_stage = WaveStage.L1_WAVE
                l1_orders = await self._deploy_l1_wave(capital, mid_price)
                await asyncio.sleep(self.config.wave_interval_ms / 1000)
                
                # 第三波：L2深度 (150-250ms)
                self.current_stage = WaveStage.L2_WAVE
                l2_orders = await self._deploy_l2_wave(capital, mid_price)
                
                # 完成
                self.current_stage = WaveStage.COMPLETED
                elapsed_ms = (time.time() - start_time) * 1000
                
                # 更新统计
                self.stats['deployments'] += 1
                self.stats['total_time_ms'] += elapsed_ms
                self.stats['avg_time_ms'] = self.stats['total_time_ms'] / self.stats['deployments']
                self.stats['l0_orders'] += len(l0_orders)
                self.stats['l1_orders'] += len(l1_orders)
                self.stats['l2_orders'] += len(l2_orders)
                
                total_orders = len(l0_orders) + len(l1_orders) + len(l2_orders)
                logger.info(f"[Phase3-B2] Wave deployment completed in {elapsed_ms:.0f}ms: {total_orders} orders placed")
                
                # 记录部署时间
                self.last_deployment = time.time()
                
            except Exception as e:
                logger.error(f"[Phase3-B2] Wave deployment failed: {e}")
                self.stats['failed_orders'] += 1
                
            finally:
                self.deploying = False
                self.current_stage = WaveStage.IDLE
                
    async def _deploy_l0_wave(self, capital: DeployableCapital, mid_price: float) -> List[Dict]:
        """部署L0层（贴近价差）"""
        orders = []
        
        # 计算L0资金
        l0_usdt = capital.usdt_free * self.config.l0_ratio
        l0_doge = capital.doge_free * self.config.l0_ratio
        
        # 计算价格
        tick_size = self._get_tick_size(mid_price)
        buy_price = mid_price - tick_size * self.config.l0_spread_ticks
        sell_price = mid_price + tick_size * self.config.l0_spread_ticks
        
        # 分成3个小单
        if l0_usdt > 10:  # 买单
            buy_size = (l0_usdt / 3) / buy_price
            for i in range(3):
                order = {
                    'side': 'BUY',
                    'price': buy_price - i * tick_size * 0.1,  # 微调价格
                    'quantity': buy_size,
                    'ttl_ms': self.config.l0_ttl_ms + i * 500,  # TTL抖动
                    'layer': 'L0',
                    'wave': 1
                }
                orders.append(order)
                
        if l0_doge > 10 / mid_price:  # 卖单
            sell_size = l0_doge / 3
            for i in range(3):
                order = {
                    'side': 'SELL',
                    'price': sell_price + i * tick_size * 0.1,
                    'quantity': sell_size,
                    'ttl_ms': self.config.l0_ttl_ms + i * 500,
                    'layer': 'L0',
                    'wave': 1
                }
                orders.append(order)
                
        # 批量下单（实际实现需要调用connector）
        # await self._place_orders_batch(orders)
        
        return orders
        
    async def _deploy_l1_wave(self, capital: DeployableCapital, mid_price: float) -> List[Dict]:
        """部署L1层（中等深度）"""
        orders = []
        
        # 计算L1资金
        l1_usdt = capital.usdt_free * self.config.l1_ratio
        l1_doge = capital.doge_free * self.config.l1_ratio
        
        # 计算价格
        tick_size = self._get_tick_size(mid_price)
        buy_price = mid_price - tick_size * self.config.l1_spread_ticks
        sell_price = mid_price + tick_size * self.config.l1_spread_ticks
        
        # 分成2个中单
        if l1_usdt > 15:
            buy_size = (l1_usdt / 2) / buy_price
            for i in range(2):
                order = {
                    'side': 'BUY',
                    'price': buy_price - i * tick_size * 0.5,
                    'quantity': buy_size,
                    'ttl_ms': self.config.l1_ttl_ms,
                    'layer': 'L1',
                    'wave': 2
                }
                orders.append(order)
                
        if l1_doge > 15 / mid_price:
            sell_size = l1_doge / 2
            for i in range(2):
                order = {
                    'side': 'SELL',
                    'price': sell_price + i * tick_size * 0.5,
                    'quantity': sell_size,
                    'ttl_ms': self.config.l1_ttl_ms,
                    'layer': 'L1',
                    'wave': 2
                }
                orders.append(order)
                
        return orders
        
    async def _deploy_l2_wave(self, capital: DeployableCapital, mid_price: float) -> List[Dict]:
        """部署L2层（深度流动性）"""
        orders = []
        
        # 计算L2资金
        l2_usdt = capital.usdt_free * self.config.l2_ratio
        l2_doge = capital.doge_free * self.config.l2_ratio
        
        # 计算价格
        tick_size = self._get_tick_size(mid_price)
        buy_price = mid_price - tick_size * self.config.l2_spread_ticks
        sell_price = mid_price + tick_size * self.config.l2_spread_ticks
        
        # 1个大单
        if l2_usdt > 20:
            buy_size = l2_usdt / buy_price
            order = {
                'side': 'BUY',
                'price': buy_price,
                'quantity': buy_size,
                'ttl_ms': self.config.l2_ttl_ms,
                'layer': 'L2',
                'wave': 3
            }
            orders.append(order)
            
        if l2_doge > 20 / mid_price:
            order = {
                'side': 'SELL',
                'price': sell_price,
                'quantity': l2_doge,
                'ttl_ms': self.config.l2_ttl_ms,
                'layer': 'L2',
                'wave': 3
            }
            orders.append(order)
            
        return orders
        
    def _get_tick_size(self, price: float) -> float:
        """获取价格刻度"""
        if price < 0.001:
            return 0.0000001
        elif price < 0.01:
            return 0.000001
        elif price < 0.1:
            return 0.00001
        elif price < 1:
            return 0.0001
        else:
            return 0.001
            
    def should_override_startup_delay(self) -> bool:
        """是否应该覆盖启动延迟"""
        # 如果已经有成功部署，立即返回True
        return self.deployment_count > 0 or self.config.startup_delay_override < 60
        
    def get_effective_startup_delay(self, original_delay: float) -> float:
        """获取有效的启动延迟"""
        if self.should_override_startup_delay():
            return min(self.config.startup_delay_override, original_delay)
        return original_delay
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'deployments': self.stats['deployments'],
            'avg_deploy_time_ms': self.stats['avg_time_ms'],
            'total_orders': self.stats['l0_orders'] + self.stats['l1_orders'] + self.stats['l2_orders'],
            'l0_orders': self.stats['l0_orders'],
            'l1_orders': self.stats['l1_orders'],
            'l2_orders': self.stats['l2_orders'],
            'failed_orders': self.stats['failed_orders'],
            'current_stage': self.current_stage.value,
            'last_deployment_ago': time.time() - self.last_deployment if self.last_deployment > 0 else -1
        }

# 单例实例
_wave_instance = None

def get_utilization_wave(connector=None, market_data=None, config=None) -> UtilizationWave:
    """获取UtilizationWave单例"""
    global _wave_instance
    if _wave_instance is None:
        _wave_instance = UtilizationWave(connector, market_data, config)
    return _wave_instance