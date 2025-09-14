#!/usr/bin/env python3
"""
Dynamic Liquidity Engine (DLE) - 改进版
集成AWG权重管理和MockExchange支持
"""

import os
import time
import math
import random
import logging
from typing import Dict, List, Tuple, Optional
from ..risk.awg import get_awg

logger = logging.getLogger(__name__)


class DLEngine:
    """动态流动性引擎 - 智能管理订单流动性"""
    
    def __init__(self, exchange, config: Dict = None):
        """
        初始化DLE
        
        Args:
            exchange: 交易所接口（真实或模拟）
            config: 配置字典
        """
        self.ex = exchange
        self.awg = get_awg()
        
        # 加载配置
        self.cfg = config or self._load_config()
        
        # 统计
        self.stats = {
            'planned': 0,
            'placed': 0,
            'rejected_awg': 0,
            'rejected_maker': 0,
            'rejected_notional': 0,
            'rejected_balance': 0,
        }
        
    def _load_config(self) -> Dict:
        """从环境变量加载配置"""
        return {
            'enabled': int(os.getenv('DLE_ENABLE', '1')),
            'target_util': float(os.getenv('DLE_TARGET_UTIL', '0.95')),
            'order_usd_min': float(os.getenv('DLE_ORDER_USD_MIN', '6')),
            'order_usd_max_frac': float(os.getenv('DLE_ORDER_USD_MAX_FRAC', '0.015')),
            'maker_guard_ticks': int(os.getenv('MAKER_GUARD_TICKS', '2')),
            'burst_ttl_ms': int(os.getenv('BURST_TTL_MS', '12000')),
            'soft_cap_new': int(os.getenv('DLE_SOFT_CAP_NEW', '40')),
            'hard_cap_new': int(os.getenv('DLE_HARD_CAP_NEW', '80')),
            'cushion_usdt': float(os.getenv('CUSHION_USDT', '10')),
            'cushion_doge': float(os.getenv('CUSHION_DOGE', '30')),
            'ticks_l0': [int(x) for x in os.getenv('DLE_TICKS_L0', '2,3,5,8').split(',')],
            'ticks_l1': [int(x) for x in os.getenv('DLE_TICKS_L1', '5,8,13').split(',')],
            'ticks_l2': [int(x) for x in os.getenv('DLE_TICKS_L2', '8,13,21').split(',')],
            'verbose': int(os.getenv('DLE_VERBOSE_LOG', '1')),
        }
        
    def align_price(self, px: float, tick: float, precision: int = 5) -> float:
        """对齐价格到tick"""
        return round(round(px / tick) * tick, precision)
        
    def maker_guard_price(self, side: str, desired: float, best_bid: float, 
                         best_ask: float, tick: float) -> float:
        """
        Maker-Guard价格保护
        确保价格满足LIMIT_MAKER要求，避免立即成交
        """
        guard_ticks = self.cfg['maker_guard_ticks']
        
        if side == 'BUY':
            # 买单价格必须低于最佳买价
            p = min(desired, best_bid - guard_ticks * tick)
        else:
            # 卖单价格必须高于最佳卖价
            p = max(desired, best_ask + guard_ticks * tick)
            
        return self.align_price(p, tick)
        
    def align_qty_notional(self, qty: float, px: float, step: float, 
                          min_notional: float) -> float:
        """
        对齐数量并检查最小名义额
        """
        # 对齐到步进
        qty = math.floor(qty / step) * step
        
        # 检查最小名义额
        if qty * px < min_notional:
            # 尝试调整到最小名义额
            min_qty = math.ceil(min_notional / px / step) * step
            if min_qty * px >= min_notional:
                return min_qty
            return 0.0
            
        return qty
        
    def plan_burst(self, side: str, budget_usd: float, mid: float, ticks: List[int],
                  best_bid: float, best_ask: float, tick: float, step: float,
                  min_notional: float) -> List[Tuple[float, float]]:
        """
        生成扇形挂单计划
        
        Returns:
            [(price, qty), ...]
        """
        if budget_usd <= 0 or not ticks:
            return []
            
        # 计算每单金额
        equity_estimate = budget_usd * 10  # 估算总权益
        order_usd_max = equity_estimate * self.cfg['order_usd_max_frac']
        order_usd_min = self.cfg['order_usd_min']
        
        per_order_usd = min(order_usd_max, max(order_usd_min, budget_usd / len(ticks)))
        
        orders = []
        for tick_offset in ticks:
            # 计算原始价格
            if side == 'BUY':
                raw_price = mid - tick_offset * tick
            else:
                raw_price = mid + tick_offset * tick
                
            # 应用Maker-Guard
            px = self.maker_guard_price(side, raw_price, best_bid, best_ask, tick)
            
            # 计算数量
            qty = self.align_qty_notional(per_order_usd / px, px, step, min_notional)
            
            if qty > 0:
                orders.append((px, qty))
                
        return orders
        
    async def apply(self, deficit_buy_usd: float, deficit_sell_usd: float,
                   market: Dict, balances: Dict, limits: Dict) -> int:
        """
        应用动态流动性
        
        Args:
            deficit_buy_usd: 买侧缺口（USD）
            deficit_sell_usd: 卖侧缺口（USD）
            market: 市场数据 {'mid', 'bid', 'ask'}
            balances: 余额 {'usdt', 'doge'}
            limits: 交易限制 {'tick', 'step', 'min_notional'}
            
        Returns:
            成功下单数量
        """
        if not self.cfg['enabled']:
            return 0
            
        # 速率预算
        max_new = min(self.cfg['soft_cap_new'], self.cfg['hard_cap_new'])
        if max_new <= 0:
            logger.warning("[DLE] 速率预算为0，跳过")
            return 0
            
        # 市场数据
        mid = market.get('mid', 0.24)
        best_bid = market.get('bid', mid - 0.0001)
        best_ask = market.get('ask', mid + 0.0001)
        
        # 交易限制
        tick = limits.get('tick', 0.00001)
        step = limits.get('step', 1.0)
        min_notional = limits.get('min_notional', 5.0)
        
        # 计算可用余额（扣除cushion）
        free_usdt = max(0.0, balances.get('usdt', 0) - self.cfg['cushion_usdt'])
        free_doge = max(0.0, balances.get('doge', 0) - self.cfg['cushion_doge'])
        
        # 计算实际预算
        buy_budget = min(deficit_buy_usd, free_usdt)
        sell_budget = min(deficit_sell_usd, free_doge * mid)
        
        # 生成扇形计划
        l0, l1, l2 = self.cfg['ticks_l0'], self.cfg['ticks_l1'], self.cfg['ticks_l2']
        
        plan = []
        
        # 买侧计划（50% L0, 30% L1, 20% L2）
        if buy_budget >= self.cfg['order_usd_min']:
            plan.append(('BUY', self.plan_burst('BUY', buy_budget * 0.5, mid, l0,
                                               best_bid, best_ask, tick, step, min_notional)))
            plan.append(('BUY', self.plan_burst('BUY', buy_budget * 0.3, mid, l1,
                                               best_bid, best_ask, tick, step, min_notional)))
            plan.append(('BUY', self.plan_burst('BUY', buy_budget * 0.2, mid, l2,
                                               best_bid, best_ask, tick, step, min_notional)))
                                               
        # 卖侧计划
        if sell_budget >= self.cfg['order_usd_min']:
            plan.append(('SELL', self.plan_burst('SELL', sell_budget * 0.5, mid, l0,
                                                best_bid, best_ask, tick, step, min_notional)))
            plan.append(('SELL', self.plan_burst('SELL', sell_budget * 0.3, mid, l1,
                                                best_bid, best_ask, tick, step, min_notional)))
            plan.append(('SELL', self.plan_burst('SELL', sell_budget * 0.2, mid, l2,
                                                best_bid, best_ask, tick, step, min_notional)))
                                                
        # 统计计划订单数
        total_planned = sum(len(orders) for _, orders in plan)
        self.stats['planned'] += total_planned
        
        if total_planned == 0:
            logger.debug("[DLE] 无有效订单计划")
            return 0
            
        # 执行下单（受AWG限流）
        placed = 0
        ttl_ms = self.cfg['burst_ttl_ms']
        
        for side, orders in plan:
            for px, qty in orders:
                if placed >= max_new:
                    break
                    
                # 检查AWG配额
                if not self.awg.acquire('new_order'):
                    self.stats['rejected_awg'] += 1
                    if self.cfg['verbose']:
                        logger.debug(f"[DLE] AWG拒绝: {side} {qty:.1f}@{px:.5f}")
                    continue
                    
                # 下单
                try:
                    # 生成客户端订单ID
                    client_oid = f"DLE-{side[0]}-{int(time.time()*1000)}-{random.randint(1000,9999)}"
                    
                    # 调用交易所接口
                    if hasattr(self.ex, 'post_only_limit'):
                        # MockExchange接口
                        ok, reason = await self.ex.post_only_limit(side, px, qty, ttl_ms)
                    else:
                        # 真实交易所接口（需要适配）
                        result = await self.ex.create_order_v2(
                            symbol='DOGEUSDT',
                            side=side,
                            order_type='LIMIT_MAKER',
                            quantity=qty,
                            price=px,
                            client_order_id=client_oid
                        )
                        ok = result is not None
                        reason = str(result) if not ok else client_oid
                        
                    if ok:
                        placed += 1
                        if self.cfg['verbose']:
                            logger.debug(f"[DLE] 下单成功: {side} {qty:.1f}@{px:.5f}")
                    else:
                        # 分析拒单原因
                        self._analyze_rejection(reason)
                        if self.cfg['verbose']:
                            logger.warning(f"[DLE] 下单失败: {side} {qty:.1f}@{px:.5f} - {reason}")
                            
                except Exception as e:
                    logger.error(f"[DLE] 下单异常: {e}")
                    
        # 记录统计
        self.stats['placed'] += placed
        
        # 输出日志
        logger.info(
            f"[DLE] 📊 计划={total_planned} 成功={placed} "
            f"买预算=${buy_budget:.1f} 卖预算=${sell_budget:.1f} "
            f"AWG拒={self.stats['rejected_awg']} "
            f"Maker拒={self.stats['rejected_maker']}"
        )
        
        return placed
        
    def _analyze_rejection(self, reason: str):
        """分析拒单原因并统计"""
        reason_lower = str(reason).lower()
        
        if 'would immediately match' in reason_lower or 'maker' in reason_lower:
            self.stats['rejected_maker'] += 1
        elif 'min_notional' in reason_lower:
            self.stats['rejected_notional'] += 1
        elif 'insufficient' in reason_lower or 'balance' in reason_lower:
            self.stats['rejected_balance'] += 1
        else:
            # 其他原因
            pass
            
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()
        
    def reset_stats(self):
        """重置统计"""
        for key in self.stats:
            self.stats[key] = 0