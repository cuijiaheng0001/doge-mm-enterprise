#!/usr/bin/env python3
"""
TWAP Rebalancer - 时间加权平均价格再平衡器
分片执行大额再平衡操作，减少市场冲击
"""

import time
import math
import random
import asyncio
import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RebalanceSlice:
    """再平衡分片"""
    slice_id: str
    side: str  # 'BUY' or 'SELL'
    target_qty: float
    target_notional: float
    max_price_impact: float
    timeout_ms: int
    created_at: float
    executed: bool = False
    actual_qty: float = 0.0
    actual_price: float = 0.0
    
    @property
    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.timeout_ms / 1000


class TWAPRebalancer:
    """TWAP再平衡器"""
    
    def __init__(self, exchange, config: Dict = None):
        """
        初始化TWAP再平衡器
        
        Args:
            exchange: 交易所接口
            config: 配置字典
        """
        self.exchange = exchange
        self.cfg = config or self._load_config()
        
        # 执行状态
        self.active_rebalances = {}  # rebalance_id -> slices
        self.execution_history = []
        
        # 统计
        self.stats = {
            'rebalances_started': 0,
            'rebalances_completed': 0,
            'rebalances_failed': 0,
            'total_slices': 0,
            'successful_slices': 0,
            'failed_slices': 0,
            'total_volume': 0.0,
            'avg_price_impact': 0.0
        }
        
    def _load_config(self) -> Dict:
        """加载配置"""
        import os
        return {
            'target_duration_sec': int(os.getenv('TWAP_TARGET_DURATION', '300')),  # 5分钟
            'slice_interval_sec': int(os.getenv('TWAP_SLICE_INTERVAL', '10')),     # 10秒
            'max_slice_pct': float(os.getenv('TWAP_MAX_SLICE_PCT', '0.1')),        # 10%
            'max_price_impact': float(os.getenv('TWAP_MAX_PRICE_IMPACT', '0.002')), # 0.2%
            'slice_timeout_sec': int(os.getenv('TWAP_SLICE_TIMEOUT', '30')),        # 30秒
            'adaptive_sizing': int(os.getenv('TWAP_ADAPTIVE_SIZING', '1')),         # 自适应分片
            'market_order_threshold': float(os.getenv('TWAP_MARKET_THRESHOLD', '0.5')), # 市价单阈值
            'verbose': int(os.getenv('TWAP_VERBOSE', '1'))
        }
        
    def _calculate_slices(self, target_imbalance_usd: float, mid_price: float, 
                         is_buy: bool) -> List[RebalanceSlice]:
        """
        计算再平衡分片
        
        Args:
            target_imbalance_usd: 目标失衡金额
            mid_price: 中间价
            is_buy: 是否为买入方向
            
        Returns:
            分片列表
        """
        if abs(target_imbalance_usd) < 5.0:  # 小于5美元不执行
            return []
            
        # 基本参数
        total_duration = self.cfg['target_duration_sec']
        slice_interval = self.cfg['slice_interval_sec']
        max_slice_pct = self.cfg['max_slice_pct']
        
        # 计算分片数量
        num_slices = max(1, total_duration // slice_interval)
        
        # 计算总数量
        if is_buy:
            # 买入：用USD除以价格
            total_qty = abs(target_imbalance_usd) / mid_price
        else:
            # 卖出：直接是DOGE数量，但目标是USD价值
            total_qty = abs(target_imbalance_usd) / mid_price
            
        # 分配分片
        slices = []
        remaining_qty = total_qty
        remaining_usd = abs(target_imbalance_usd)
        
        for i in range(num_slices):
            if remaining_qty <= 0:
                break
                
            # 基础分片大小
            base_slice_qty = remaining_qty / (num_slices - i)
            
            # 应用最大分片限制
            max_slice_qty = total_qty * max_slice_pct
            slice_qty = min(base_slice_qty, max_slice_qty)
            
            # 自适应调整（前期小一点，后期大一点）
            if self.cfg['adaptive_sizing']:
                progress = i / num_slices
                size_factor = 0.8 + 0.4 * progress  # 从80%到120%
                slice_qty *= size_factor
                
            # 确保不超过剩余量
            slice_qty = min(slice_qty, remaining_qty)
            
            # 计算分片名义额
            slice_notional = slice_qty * mid_price
            
            # 创建分片
            slice_id = f"TWAP-{int(time.time())}-{i:03d}"
            side = 'BUY' if is_buy else 'SELL'
            
            slice_obj = RebalanceSlice(
                slice_id=slice_id,
                side=side,
                target_qty=slice_qty,
                target_notional=slice_notional,
                max_price_impact=self.cfg['max_price_impact'],
                timeout_ms=self.cfg['slice_timeout_sec'] * 1000,
                created_at=time.time()
            )
            
            slices.append(slice_obj)
            
            remaining_qty -= slice_qty
            remaining_usd -= slice_notional
            
        logger.info(
            f"[TWAP] 生成 {len(slices)} 个分片，总量 {total_qty:.1f} "
            f"({abs(target_imbalance_usd):.1f} USD)"
        )
        
        return slices
        
    async def _execute_slice(self, slice_obj: RebalanceSlice, 
                            market_data: Dict) -> bool:
        """
        TWAP分片执行，接入AWG限流
        
        Args:
            slice_obj: 分片对象
            market_data: 市场数据
            
        Returns:
            执行是否成功
        """
        if slice_obj.is_expired:
            logger.warning(f"[TWAP] 分片 {slice_obj.slice_id} 已过期")
            return False
            
        # AWG限流检查
        try:
            from ..risk.awg_pro import get_awg_pro
            
            awg = get_awg_pro()
            if not awg.acquire('new_order'):
                logger.warning(f"[TWAP] AWG配额不足，跳过分片 {slice_obj.slice_id}")
                self.stats['rejected_awg'] = self.stats.get('rejected_awg', 0) + 1
                return False
        except Exception as e:
            logger.debug(f"[TWAP] AWG检查异常，继续执行: {e}")
            
        try:
            mid_price = market_data['mid']
            best_bid = market_data['bid']
            best_ask = market_data['ask']
            
            # 计算执行价格
            if slice_obj.side == 'BUY':
                # 买入：使用稍高于最佳卖价的价格
                target_price = best_ask * (1 + slice_obj.max_price_impact / 2)
                # 检查是否应该使用市价单
                if (target_price - mid_price) / mid_price > self.cfg['market_order_threshold']:
                    # 使用限价单接近市价
                    exec_price = mid_price * (1 + slice_obj.max_price_impact)
                else:
                    exec_price = target_price
            else:
                # 卖出：使用稍低于最佳买价的价格
                target_price = best_bid * (1 - slice_obj.max_price_impact / 2)
                if (mid_price - target_price) / mid_price > self.cfg['market_order_threshold']:
                    # 使用限价单接近市价
                    exec_price = mid_price * (1 - slice_obj.max_price_impact)
                else:
                    exec_price = target_price
                    
            # 调整数量（基于实际执行价格）
            exec_qty = slice_obj.target_notional / exec_price
            
            # 执行订单
            if hasattr(self.exchange, 'create_order_immediate'):
                # 使用立即成交订单（IOC）
                result = await self.exchange.create_order_immediate(
                    symbol='DOGEUSDT',
                    side=slice_obj.side,
                    quantity=exec_qty,
                    price=exec_price
                )
            else:
                # 使用普通限价单
                client_oid = f"TWAP-{slice_obj.slice_id}-{random.randint(1000,9999)}"
                result = await self.exchange.create_order_v2(
                    symbol='DOGEUSDT',
                    side=slice_obj.side,
                    order_type='LIMIT',
                    quantity=exec_qty,
                    price=exec_price,
                    client_order_id=client_oid,
                    time_in_force='IOC'  # 立即成交或取消
                )
                
            if result:
                # 解析执行结果
                filled_qty = float(result.get('executedQty', 0))
                avg_price = float(result.get('price', exec_price))
                
                slice_obj.executed = True
                slice_obj.actual_qty = filled_qty
                slice_obj.actual_price = avg_price
                
                # 计算价格冲击
                price_impact = abs(avg_price - mid_price) / mid_price
                
                # 更新统计
                self.stats['successful_slices'] += 1
                self.stats['total_volume'] += filled_qty * avg_price
                self.stats['avg_price_impact'] = (
                    self.stats['avg_price_impact'] * 0.9 + price_impact * 0.1
                )
                
                if self.cfg['verbose']:
                    logger.info(
                        f"[TWAP] 分片执行成功: {slice_obj.slice_id} "
                        f"{filled_qty:.1f}@{avg_price:.5f} "
                        f"冲击={price_impact:.3%}"
                    )
                    
                return True
                
            else:
                logger.warning(f"[TWAP] 分片执行失败: {slice_obj.slice_id}")
                return False
                
        except Exception as e:
            logger.error(f"[TWAP] 分片执行异常 {slice_obj.slice_id}: {e}")
            return False
            
    async def execute_rebalance(self, target_imbalance_usd: float, 
                               market_data: Dict) -> str:
        """
        执行TWAP再平衡
        
        Args:
            target_imbalance_usd: 目标失衡金额（正数=需要买入，负数=需要卖出）
            market_data: 市场数据
            
        Returns:
            再平衡ID
        """
        if abs(target_imbalance_usd) < 5.0:
            logger.debug("[TWAP] 失衡金额太小，跳过再平衡")
            return ""
            
        rebalance_id = f"REB-{int(time.time())}"
        is_buy = target_imbalance_usd > 0
        
        # 生成分片
        slices = self._calculate_slices(
            target_imbalance_usd,
            market_data['mid'],
            is_buy
        )
        
        if not slices:
            logger.warning("[TWAP] 无法生成有效分片")
            return ""
            
        # 注册再平衡任务
        self.active_rebalances[rebalance_id] = slices
        self.stats['rebalances_started'] += 1
        self.stats['total_slices'] += len(slices)
        
        logger.info(
            f"[TWAP] 开始再平衡 {rebalance_id}: "
            f"{'买入' if is_buy else '卖出'} {abs(target_imbalance_usd):.1f} USD "
            f"分片数: {len(slices)}"
        )
        
        # 启动执行任务
        asyncio.create_task(self._execute_rebalance_loop(rebalance_id, market_data))
        
        return rebalance_id
        
    async def _execute_rebalance_loop(self, rebalance_id: str, market_data: Dict):
        """再平衡执行循环"""
        slices = self.active_rebalances.get(rebalance_id, [])
        if not slices:
            return
            
        successful_slices = 0
        failed_slices = 0
        
        try:
            for i, slice_obj in enumerate(slices):
                # 等待执行间隔
                if i > 0:
                    await asyncio.sleep(self.cfg['slice_interval_sec'])
                    
                # 获取最新市场数据
                # TODO: 实际实现中应该获取实时数据
                current_market = market_data  
                
                # 执行分片
                success = await self._execute_slice(slice_obj, current_market)
                
                if success:
                    successful_slices += 1
                else:
                    failed_slices += 1
                    self.stats['failed_slices'] += 1
                    
                # 检查是否应该提前终止
                if failed_slices > len(slices) * 0.5:  # 超过50%失败
                    logger.warning(f"[TWAP] 再平衡 {rebalance_id} 失败率过高，提前终止")
                    break
                    
            # 完成统计
            if successful_slices > 0:
                self.stats['rebalances_completed'] += 1
                logger.info(
                    f"[TWAP] 再平衡 {rebalance_id} 完成: "
                    f"成功={successful_slices} 失败={failed_slices}"
                )
            else:
                self.stats['rebalances_failed'] += 1
                logger.warning(f"[TWAP] 再平衡 {rebalance_id} 完全失败")
                
            # 记录历史
            self.execution_history.append({
                'rebalance_id': rebalance_id,
                'timestamp': time.time(),
                'total_slices': len(slices),
                'successful_slices': successful_slices,
                'failed_slices': failed_slices,
                'executed_volume': sum(s.actual_qty * s.actual_price for s in slices if s.executed),
                'avg_price_impact': sum(
                    abs(s.actual_price - market_data['mid']) / market_data['mid'] 
                    for s in slices if s.executed
                ) / max(1, successful_slices)
            })
            
        except Exception as e:
            logger.error(f"[TWAP] 再平衡循环异常 {rebalance_id}: {e}")
            self.stats['rebalances_failed'] += 1
            
        finally:
            # 清理
            self.active_rebalances.pop(rebalance_id, None)
            
    def get_active_rebalances(self) -> List[Dict]:
        """获取活跃再平衡"""
        active = []
        for rebalance_id, slices in self.active_rebalances.items():
            executed_slices = sum(1 for s in slices if s.executed)
            total_slices = len(slices)
            
            active.append({
                'rebalance_id': rebalance_id,
                'progress': executed_slices / total_slices if total_slices > 0 else 0,
                'executed_slices': executed_slices,
                'total_slices': total_slices,
                'start_time': slices[0].created_at if slices else 0
            })
            
        return active
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()
        
    def get_status(self) -> str:
        """获取状态摘要"""
        active = len(self.active_rebalances)
        success_rate = (
            self.stats['successful_slices'] / max(1, self.stats['total_slices']) * 100
        )
        
        return (
            f"TWAP(active={active} "
            f"success={success_rate:.0f}% "
            f"impact={self.stats['avg_price_impact']:.3%})"
        )


if __name__ == "__main__":
    # 简单测试
    class MockExchange:
        async def create_order_v2(self, **kwargs):
            # 模拟执行
            return {
                'executedQty': str(kwargs['quantity'] * 0.95),  # 95%成交
                'price': str(kwargs['price'])
            }
    
    async def test():
        rebalancer = TWAPRebalancer(MockExchange())
        
        # 模拟市场数据
        market = {
            'mid': 0.24,
            'bid': 0.23995,
            'ask': 0.24005
        }
        
        # 执行再平衡（买入100 USD）
        rebalance_id = await rebalancer.execute_rebalance(100.0, market)
        
        # 等待完成
        await asyncio.sleep(5)
        
        print(f"再平衡 {rebalance_id} 统计: {rebalancer.get_stats()}")
        print(f"状态: {rebalancer.get_status()}")
        
# 全局实例
_global_twap_rebalancer = None

def get_twap_rebalancer(exchange=None, **kwargs):
    """获取TWAP Rebalancer实例（单例）"""
    global _global_twap_rebalancer
    
    if _global_twap_rebalancer is None and exchange:
        slice_usd = kwargs.get('slice_usd', 40)
        interval_ms = kwargs.get('interval_ms', 5000)
        max_retries = kwargs.get('max_retries', 3)
        
        _global_twap_rebalancer = TWAPRebalancer(
            exchange=exchange,
            slice_usd=slice_usd,
            interval_ms=interval_ms,
            max_retries=max_retries
        )
        logger.info(f"[TWAPRebalancer] 创建新实例: slice_usd={slice_usd}")
    
    return _global_twap_rebalancer

def reset_twap_rebalancer():
    """重置TWAP Rebalancer实例（测试用）"""
    global _global_twap_rebalancer
    _global_twap_rebalancer = None

if __name__ == "__main__":
    asyncio.run(test())