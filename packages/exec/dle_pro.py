#!/usr/bin/env python3
"""
DLE Pro - Dynamic Liquidity Engine Professional Version  
专业版动态流动性引擎 with分层控制+每价位限制+自适应调整
"""

import time
import math
import random
import logging
import asyncio
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict, deque
from ..risk.awg_pro import get_awg_pro
from ..risk.shadow_balance import get_shadow_balance
from .adaptive_sizer import AdaptiveSizer, SizerConfig, MarketSnapshot, InventoryState
from .utilization_planner import UtilizationPlanner, PlannerConfig

logger = logging.getLogger(__name__)


class OrderPlan:
    """订单计划"""
    
    def __init__(self, side: str, price: float, qty: float, layer: str, 
                 ttl_ms: int = 12000, priority: int = 0):
        self.side = side
        self.price = price  
        self.qty = qty
        self.layer = layer
        self.ttl_ms = ttl_ms
        self.priority = priority  # 优先级（越大越优先）
        self.client_oid = None
        self.created_at = time.time()
        
    @property
    def notional(self) -> float:
        """名义额"""
        return self.price * self.qty
        
    def __repr__(self):
        return f"OrderPlan({self.side} {self.qty:.1f}@{self.price:.5f} L={self.layer})"


class LayerConfig:
    """分层配置"""
    
    def __init__(self, name: str, weight: float, ticks: List[int], 
                 ttl_ms: int, priority: int = 0):
        self.name = name
        self.weight = weight  # 预算权重
        self.ticks = ticks   # tick偏移列表
        self.ttl_ms = ttl_ms # TTL毫秒
        self.priority = priority
        
    def __repr__(self):
        return f"Layer({self.name} w={self.weight} ticks={self.ticks})"


class DLEPro:
    """动态流动性引擎专业版"""
    
    def __init__(self, exchange, config: Dict = None):
        """
        初始化DLE Pro
        
        Args:
            exchange: 交易所接口
            config: 配置字典
        """
        self.exchange = exchange
        self.awg = get_awg_pro()
        self.shadow = get_shadow_balance()
        
        # 加载配置
        self.cfg = config or self._load_config()
        
        # 分层配置
        self.layers = {
            'L0': LayerConfig('L0', 0.5, self.cfg['ticks_l0'], 5000, priority=3),
            'L1': LayerConfig('L1', 0.3, self.cfg['ticks_l1'], 10000, priority=2), 
            'L2': LayerConfig('L2', 0.2, self.cfg['ticks_l2'], 20000, priority=1)
        }
        
        # 自适应参数
        self.adaptive = {
            'maker_guard_base': self.cfg['maker_guard_base'],
            'maker_guard_stress': self.cfg['maker_guard_stress'],
            'spread_factor': 1.0,  # 动态扩散因子
            'size_factor': 1.0,    # 动态大小因子
            'stress_mode': False   # 压力模式
        }
        
        # 每价位限制
        self.per_price_limit = self.cfg['per_price_limit']
        self.active_price_orders = defaultdict(int)  # price -> count
        
        # 统计
        self.stats = {
            'planned_total': 0,
            'planned_by_layer': defaultdict(int),
            'placed_total': 0,
            'placed_by_layer': defaultdict(int),
            'rejected_awg': 0,
            'rejected_shadow': 0,
            'rejected_maker': 0,
            'rejected_notional': 0,
            'rejected_balance': 0,
            'rejected_price_limit': 0,
            'stress_mode_entries': 0
        }
        
        # 近期拒单历史（用于自适应）
        self.rejection_history = deque(maxlen=100)
        
        # Phase 4: TTL撤单管理
        self.ttl_tasks = {}  # order_id -> asyncio.Task
        self.ttl_cleanup_interval = 60  # 每60s清理过期任务
        
        # Phase 5: 订单生命周期管理
        self.live_orders = {}  # orderId -> {'cid': client_oid, 'price': price, 'reserve_asset': asset, 'reserve_amount': amount}
        
        # Phase 6 P0-2: 并发锁保护
        self._live_lock = asyncio.Lock()
        self._ttl_lock = asyncio.Lock()
        
        # Phase 6 P0-4: 暖启动坡道
        self.start_ts = time.time()
        
        # Phase 7: 智能资金利用模块 - 延迟初始化
        self.sizer_enabled = bool(self.cfg.get('adaptive_sizer_enable', 1))
        self.sizer = None  # 将在首次apply时初始化
        logger.info(f"🔍 [Phase7.1-Debug] sizer_enabled={self.sizer_enabled}, cfg.adaptive_sizer_enable={self.cfg.get('adaptive_sizer_enable')}")
            
        if self.cfg.get('util_planner_enable', 1):
            planner_config = PlannerConfig(
                target_util=self.cfg.get('target_util', 0.93),
                keep_usdt_cushion=self.cfg.get('util_usdt_cushion', 0.12),
                layer_weights=self._parse_layer_weights(self.cfg.get('util_layer_weights', '0:0.20,1:0.35,2:0.45'))
            )
            self.util_planner = UtilizationPlanner(planner_config, logger)
            logger.info(f"✅ [Phase7.1] UtilizationPlanner initialized with target_util={planner_config.target_util}")
        else:
            self.util_planner = None
            logger.info(f"❌ [Phase7.1-Debug] UtilizationPlanner disabled: util_planner_enable={self.cfg.get('util_planner_enable')}")
            
        # Phase 7: 不变量检查统计
        self.invariants = {
            'budget_violations': 0,
            'shadow_mismatches': 0,
            'data_stale_events': 0
        }
        
    def _load_config(self) -> Dict:
        """从环境变量加载配置"""
        import os
        return {
            'enabled': int(os.getenv('DLE_PRO_ENABLE', '1')),
            'target_util': float(os.getenv('DLE_TARGET_UTIL', '0.95')),
            'order_usd_min': float(os.getenv('DLE_ORDER_USD_MIN', '6')),
            'order_usd_max': float(os.getenv('DLE_ORDER_USD_MAX', '50')),
            'maker_guard_base': int(os.getenv('MAKER_GUARD_BASE', '2')),
            'maker_guard_stress': int(os.getenv('MAKER_GUARD_STRESS', '5')),
            'per_price_limit': int(os.getenv('DLE_PER_PRICE_LIMIT', '5')),
            'soft_cap_new': int(os.getenv('DLE_SOFT_CAP_NEW', '40')),
            'hard_cap_new': int(os.getenv('DLE_HARD_CAP_NEW', '80')),
            'ticks_l0': [int(x) for x in os.getenv('DLE_TICKS_L0', '1,2,3').split(',')],
            'ticks_l1': [int(x) for x in os.getenv('DLE_TICKS_L1', '3,5,8').split(',')],
            'ticks_l2': [int(x) for x in os.getenv('DLE_TICKS_L2', '8,13,21').split(',')],
            'cushion_base_usdt': float(os.getenv('CUSHION_BASE_USDT', '10')),
            'cushion_base_doge': float(os.getenv('CUSHION_BASE_DOGE', '30')),
            'cushion_max_pct': float(os.getenv('CUSHION_MAX_PCT', '0.05')),
            'cushion_volatility_factor': float(os.getenv('CUSHION_VOLATILITY_FACTOR', '2')),
            'verbose': int(os.getenv('DLE_PRO_VERBOSE', '1')),
            # Phase 7 parameters
            'adaptive_sizer_enable': int(os.getenv('ADAPTIVE_SIZER_ENABLE', '1')),
            'sizer_max_single_notional': float(os.getenv('SIZER_MAX_SINGLE_NOTIONAL', '500')),
            'sizer_layer_mult': os.getenv('SIZER_LAYER_MULT', '0:0.5,1:1.0,2:1.5,3:2.0'),
            'util_planner_enable': int(os.getenv('UTIL_PLANNER_ENABLE', '1')),
            'util_usdt_cushion': float(os.getenv('UTIL_USDT_CUSHION', '0.12')),
            'util_layer_weights': os.getenv('UTIL_LAYER_WEIGHTS', '0:0.20,1:0.35,2:0.45'),
            'doge_target_ratio': float(os.getenv('DOGE_TARGET_RATIO', '0.50')),
            'mirror_stale_sec': int(os.getenv('MIRROR_STALE_SEC', '10')),
            'uds_stale_sec': int(os.getenv('UDS_STALE_SEC', '3'))
        }
        
    def _detect_stress_mode(self) -> bool:
        """检测是否进入压力模式"""
        # 检查AWG状态
        awg_status = self.awg.get_status()
        if awg_status['state'] in ['DEGRADED', 'CIRCUIT_OPEN']:
            return True
            
        # 检查近期拒单率
        if len(self.rejection_history) >= 10:
            recent_rejects = sum(1 for r in list(self.rejection_history)[-10:] 
                               if r['type'] == 'maker')
            reject_rate = recent_rejects / 10
            if reject_rate > 0.3:  # 30%拒单率
                return True
                
        return False
        
    def _parse_layer_mult(self, mult_str: str) -> Dict[int, float]:
        """解析层级倍率配置"""
        result = {}
        for pair in mult_str.split(','):
            layer, mult = pair.split(':')
            result[int(layer)] = float(mult)
        return result
        
    def _parse_layer_weights(self, weights_str: str) -> Dict[int, float]:
        """解析层级权重配置"""
        result = {}
        for pair in weights_str.split(','):
            layer, weight = pair.split(':')
            result[int(layer)] = float(weight)
        return result
        
    def _ensure_sizer_initialized(self, limits: Dict):
        """延迟初始化AdaptiveSizer（需要limits信息）- Phase 7.1 增强版"""
        if self.sizer_enabled and self.sizer is None:
            try:
                logger.info(f"🔧 [Phase7.1] Starting AdaptiveSizer initialization with limits: {limits}")
                
                # 兼容主程序的limits键名
                min_qty = limits.get('min_qty', 1.0)  # 直接取
                step_size = limits.get('step_size', limits.get('step', 1.0))  # 兼容'step'
                min_notional = limits.get('min_notional', 10.0)  # 直接取
                max_single_notional = self.cfg.get('sizer_max_single_notional', 500.0)
                layer_mult_str = self.cfg.get('sizer_layer_mult', '0:0.5,1:1.0,2:1.5,3:2.0')
                
                logger.info(f"📊 [Phase7.1] Parsed parameters: min_qty={min_qty}, step_size={step_size}, min_notional={min_notional}")
                logger.info(f"🎯 [Phase7.1] Config: max_single_notional={max_single_notional}, layer_mult='{layer_mult_str}'")
                
                layer_mult = self._parse_layer_mult(layer_mult_str)
                
                sizer_config = SizerConfig(
                    min_qty=min_qty,
                    step_size=step_size,
                    min_notional=min_notional,
                    max_single_notional=max_single_notional,
                    layer_mult=layer_mult
                )
                
                self.sizer = AdaptiveSizer(sizer_config, logger)
                logger.info(f"✅ [Phase7.1] AdaptiveSizer SUCCESSFULLY initialized!")
                logger.info(f"📈 [Phase7.1] Final config - min_qty={min_qty}, step_size={step_size}, min_notional={min_notional}")
                logger.info(f"🚀 [Phase7.1] Layer multipliers: {layer_mult}")
                logger.info(f"💰 [Phase7.1] Max single notional: {max_single_notional} USDT")
                
            except Exception as e:
                logger.error(f"❌ [Phase7.1] AdaptiveSizer initialization FAILED: {e}")
                logger.error(f"🔍 [Phase7.1] Debug - limits keys: {list(limits.keys()) if limits else 'None'}")
                logger.error(f"🔍 [Phase7.1] Debug - sizer_enabled: {self.sizer_enabled}")
                self.sizer = None
            
    def _check_invariants(self, market: Dict, equity_usdt: float, plan_result: Dict = None) -> Dict[str, bool]:
        """检查Phase 7不变量"""
        results = {}
        
        # I1: 预算一致性
        if plan_result and hasattr(self, 'live_orders'):
            live_notional_buy = sum(order['price'] * order['remain_qty'] 
                                  for order in self.live_orders.values() 
                                  if order['side'] == 'BUY')
            live_notional_sell = sum(order['price'] * order['remain_qty'] 
                                   for order in self.live_orders.values() 
                                   if order['side'] == 'SELL')
            
            budget_buy = sum(plan_result.get('buy', {}).values())
            budget_sell = sum(plan_result.get('sell', {}).values())
            
            epsilon = 0.02  # 2% 冗余
            budget_ok_buy = live_notional_buy <= budget_buy * (1 + epsilon)
            budget_ok_sell = live_notional_sell <= budget_sell * (1 + epsilon)
            
            results['budget_buy'] = budget_ok_buy
            results['budget_sell'] = budget_ok_sell
            
            if not budget_ok_buy or not budget_ok_sell:
                self.invariants['budget_violations'] += 1
                logger.warning(f"[DLE-Pro] 预算不变量违背: live_buy={live_notional_buy:.1f} budget_buy={budget_buy:.1f} live_sell={live_notional_sell:.1f} budget_sell={budget_sell:.1f}")
        
        # I2: Shadow一致性
        if hasattr(self.shadow, 'get_locked') and hasattr(self, 'live_orders'):
            shadow_locked_usdt = self.shadow.get_locked('USDT')
            shadow_locked_doge = self.shadow.get_locked('DOGE')
            
            expected_locked_usdt = sum(order['price'] * order['remain_qty'] 
                                     for order in self.live_orders.values() 
                                     if order['side'] == 'BUY')
            expected_locked_doge = sum(order['remain_qty'] 
                                     for order in self.live_orders.values() 
                                     if order['side'] == 'SELL')
            
            shadow_ok_usdt = abs(shadow_locked_usdt - expected_locked_usdt) < expected_locked_usdt * 0.05
            shadow_ok_doge = abs(shadow_locked_doge - expected_locked_doge) < expected_locked_doge * 0.05
            
            results['shadow_usdt'] = shadow_ok_usdt
            results['shadow_doge'] = shadow_ok_doge
            
            if not shadow_ok_usdt or not shadow_ok_doge:
                self.invariants['shadow_mismatches'] += 1
                logger.warning(f"[DLE-Pro] Shadow不一致: shadow_usdt={shadow_locked_usdt:.1f} expected={expected_locked_usdt:.1f} shadow_doge={shadow_locked_doge:.1f} expected={expected_locked_doge:.1f}")
        
        # I3: 数据新鲜性
        import time
        uds_fresh = True
        mirror_fresh = True
        
        if hasattr(self, 'user_stream') and hasattr(self.user_stream, 'last_msg_ts'):
            uds_age = time.time() - (self.user_stream.last_msg_ts or 0)
            uds_fresh = uds_age < self.cfg.get('uds_stale_sec', 3)
            
        if hasattr(self, 'order_mirror') and hasattr(self.order_mirror, 'last_sync_time'):
            mirror_age = time.time() - (self.order_mirror.last_sync_time or 0)
            mirror_fresh = mirror_age < self.cfg.get('mirror_stale_sec', 10)
            
        results['data_fresh'] = uds_fresh and mirror_fresh
        
        if not results['data_fresh']:
            self.invariants['data_stale_events'] += 1
            
        return results
        
    def _update_adaptive_params(self):
        """更新自适应参数"""
        # 检查是否需要进入压力模式
        new_stress_mode = self._detect_stress_mode()
        
        if new_stress_mode and not self.adaptive['stress_mode']:
            # 进入压力模式
            self.adaptive['stress_mode'] = True
            self.adaptive['spread_factor'] *= 1.5  # 增大扇形宽度
            self.adaptive['size_factor'] *= 0.8    # 减小订单大小
            self.stats['stress_mode_entries'] += 1
            logger.warning("[DLE Pro] 进入压力模式")
            
        elif not new_stress_mode and self.adaptive['stress_mode']:
            # 退出压力模式
            self.adaptive['stress_mode'] = False
            self.adaptive['spread_factor'] = 1.0
            self.adaptive['size_factor'] = 1.0
            logger.info("[DLE Pro] 退出压力模式")
            
    def _calculate_dynamic_cushion(self, equity: float, volatility: float, 
                                  fills_per_min: float) -> Dict[str, float]:
        """计算动态Cushion"""
        base_usdt = self.cfg['cushion_base_usdt']
        base_doge = self.cfg['cushion_base_doge']
        
        # 波动率调整
        vol_factor = 1 + volatility * self.cfg['cushion_volatility_factor']
        
        # 成交频率调整
        fill_factor = 1 + fills_per_min / 10
        
        # 权益比例限制
        max_cushion_pct = self.cfg['cushion_max_pct']
        
        cushion_usdt = min(
            base_usdt * vol_factor * fill_factor,
            equity * max_cushion_pct
        )
        
        cushion_doge = min(
            base_doge * vol_factor * fill_factor,
            equity * max_cushion_pct
        )
        
        return {
            'USDT': cushion_usdt,
            'DOGE': cushion_doge
        }
        
    def _align_price(self, px: float, tick: float, precision: int = 5) -> float:
        """对齐价格到tick"""
        return round(round(px / tick) * tick, precision)
        
    def _maker_guard_price(self, side: str, desired: float, best_bid: float, 
                          best_ask: float, tick: float) -> float:
        """Maker-Guard价格保护，按方向对齐"""
        import math
        
        guard_ticks = self.adaptive['maker_guard_stress'] if self.adaptive['stress_mode'] else self.adaptive['maker_guard_base']
        
        if side == 'BUY':
            p = min(desired, best_bid - guard_ticks * tick)
            p = math.floor(p / tick) * tick  # 向下对齐，确保不会上抬价格
        else:  # SELL
            p = max(desired, best_ask + guard_ticks * tick)
            p = math.ceil(p / tick) * tick   # 向上对齐
            
        return round(p, 5)
        
    def _align_qty_notional(self, qty: float, px: float, step: float, 
                           min_notional: float) -> float:
        """对齐数量并检查最小名义额"""
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
        
    def _plan_layer_orders(self, side: str, budget_usd: float, mid: float, 
                          layer: LayerConfig, best_bid: float, best_ask: float,
                          tick: float, step: float, min_notional: float) -> List[OrderPlan]:
        """生成单层订单计划"""
        if budget_usd <= 0 or not layer.ticks:
            return []
            
        # 应用自适应因子
        effective_ticks = [
            int(t * self.adaptive['spread_factor']) 
            for t in layer.ticks
        ]
        
        # 计算每单金额
        per_order_usd = min(
            self.cfg['order_usd_max'],
            max(self.cfg['order_usd_min'], budget_usd / len(effective_ticks))
        )
        per_order_usd *= self.adaptive['size_factor']
        
        orders = []
        for tick_offset in effective_ticks:
            # 计算原始价格
            if side == 'BUY':
                raw_price = mid - tick_offset * tick
            else:
                raw_price = mid + tick_offset * tick
                
            # 应用Maker-Guard
            px = self._maker_guard_price(side, raw_price, best_bid, best_ask, tick)
            
            # 检查每价位限制
            if self.active_price_orders.get(self._price_key(px), 0) >= self.per_price_limit:
                continue
                
            # 计算数量
            qty = self._align_qty_notional(per_order_usd / px, px, step, min_notional)
            
            if qty > 0:
                order = OrderPlan(
                    side, px, qty, layer.name, 
                    layer.ttl_ms, layer.priority
                )
                orders.append(order)
                
        return orders
        
    def _generate_master_plan(self, deficit_buy_usd: float, deficit_sell_usd: float,
                             market: Dict, limits: Dict, 
                             dynamic_cushion: Dict) -> List[OrderPlan]:
        """生成主计划"""
        # 市场数据
        mid = market['mid']
        best_bid = market['bid']  
        best_ask = market['ask']
        
        # 交易限制
        tick = limits['tick']
        step = limits['step']
        min_notional = limits['min_notional']
        
        # 影子余额
        shadow_usdt = self.shadow.get_available('USDT')
        shadow_doge = self.shadow.get_available('DOGE')
        
        # 扣除动态Cushion
        usable_usdt = max(0, shadow_usdt - dynamic_cushion['USDT'])
        usable_doge = max(0, shadow_doge - dynamic_cushion['DOGE'])
        
        # 计算实际预算
        buy_budget = min(deficit_buy_usd, usable_usdt)
        sell_budget = min(deficit_sell_usd, usable_doge * mid)
        
        master_plan = []
        
        # 按层生成计划
        for layer in self.layers.values():
            # 买侧
            if buy_budget >= self.cfg['order_usd_min']:
                layer_budget = buy_budget * layer.weight
                layer_orders = self._plan_layer_orders(
                    'BUY', layer_budget, mid, layer,
                    best_bid, best_ask, tick, step, min_notional
                )
                master_plan.extend(layer_orders)
                
            # 卖侧  
            if sell_budget >= self.cfg['order_usd_min']:
                layer_budget = sell_budget * layer.weight
                layer_orders = self._plan_layer_orders(
                    'SELL', layer_budget, mid, layer,
                    best_bid, best_ask, tick, step, min_notional
                )
                master_plan.extend(layer_orders)
                
        # 按优先级排序
        master_plan.sort(key=lambda x: x.priority, reverse=True)
        
        return master_plan
        
    def _ramp_new_limit(self):
        """Phase 6 P0-4: 暖启动坡道（避免解封瞬间冲量）"""
        elapsed = time.time() - self.start_ts
        if elapsed < 60:   return 1  # 前1分钟只允许1单
        if elapsed < 120:  return 2  # 前2分钟只允许2单
        return min(self.cfg.get('soft_cap_new', 2), 4)  # 之后恢复正常但不超过4
    
    async def _execute_plan(self, master_plan: List[OrderPlan]) -> int:
        """执行订单计划"""
        if not master_plan:
            return 0
            
        # Phase 6 P0-4: 应用暖启动坡道限制
        max_new = self._ramp_new_limit()
        master_plan = master_plan[:max_new]
        
        placed = 0
        
        for order in master_plan:
            if placed >= max_new:
                break
                
            # 预留Shadow Balance
            reserve_asset = 'USDT' if order.side == 'BUY' else 'DOGE'
            reserve_amount = order.notional if order.side == 'BUY' else order.qty
            
            order.client_oid = f"DLE-{order.side[0]}-{int(time.time()*1000)}-{random.randint(1000,9999)}"
            
            if not self.shadow.reserve(order.client_oid, reserve_asset, reserve_amount):
                self.stats['rejected_shadow'] += 1
                self._record_rejection('shadow', order)
                continue
                
            # 检查AWG配额
            if not self.awg.acquire('new_order'):
                self.stats['rejected_awg'] += 1
                self._record_rejection('awg', order)
                self.shadow.release(order.client_oid, 'awg_denied')
                continue
                
            # 执行下单
            try:
                if hasattr(self.exchange, 'post_only_limit'):
                    # MockExchange接口
                    ok, reason = await self.exchange.post_only_limit(
                        order.side, order.price, order.qty, order.ttl_ms
                    )
                else:
                    # 真实交易所接口
                    result = await self.exchange.create_order_v2(
                        symbol='DOGEUSDT',
                        side=order.side,
                        order_type='LIMIT_MAKER',
                        quantity=order.qty,
                        price=order.price,
                        client_order_id=order.client_oid
                    )
                    ok = result is not None
                    reason = str(result) if not ok else order.client_oid
                    
                if ok:
                    placed += 1
                    key = self._price_key(order.price)
                    self.active_price_orders[key] = self.active_price_orders.get(key, 0) + 1
                    self.stats['placed_total'] += 1
                    self.stats['placed_by_layer'][order.layer] += 1
                    
                    # Phase 5 + UDS Phase 1: 订单生命周期登记（扩展字段）
                    order_id = None
                    if 'result' in locals():
                        order_id = result.get('orderId') if isinstance(result, dict) else getattr(result, 'orderId', None)
                    if order_id:
                        reserve_asset = 'USDT' if order.side == 'BUY' else 'DOGE'
                        reserve_amount = order.notional if order.side == 'BUY' else order.qty
                        self.live_orders[order_id] = {
                            'cid': order.client_oid,
                            'side': order.side,
                            'price': order.price,
                            'orig_qty': order.qty,
                            'filled_qty': 0.0,  # UDS will update this
                            'remain_qty': order.qty,  # UDS will update this
                            'reserve_asset': reserve_asset,
                            'reserve_amount': reserve_amount,
                            'timestamp': time.time() * 1000
                        }
                        # TTL 调度
                        self.schedule_ttl(order_id, order.price, order.ttl_ms)
                    else:
                        # 无法获取orderId时立即释放Shadow（兜底）
                        self.shadow.release(order.client_oid, 'placed_no_orderid')
                    
                    if self.cfg['verbose']:
                        logger.debug(f"[DLE Pro] 下单成功: {order}")
                        
                else:
                    # 分析拒单原因
                    rejection_type = self._analyze_rejection(reason)
                    self.stats[f'rejected_{rejection_type}'] += 1
                    self._record_rejection(rejection_type, order, reason)
                    
                    # 释放预留
                    self.shadow.release(order.client_oid, f'rejected_{rejection_type}')
                    
                    if self.cfg['verbose']:
                        logger.warning(f"[DLE Pro] 下单失败: {order} - {reason}")
                        
            except Exception as e:
                logger.error(f"[DLE Pro] 下单异常: {e}")
                self.shadow.release(order.client_oid, 'exception')
                
        return placed

    async def _schedule_ttl_cancel(self, order_id: str, price: float, ttl_ms: int):
        """TTL撤单调度"""
        try:
            await asyncio.sleep(ttl_ms / 1000)
            if self.awg.acquire('cancel'):
                await self.exchange.cancel_order(order_id=order_id)
                self.on_order_closed(price)
        except Exception as e:
            logger.error(f"[DLE Pro] TTL撤单异常 {order_id}: {e}")
        
    def _analyze_rejection(self, reason: str) -> str:
        """分析拒单原因"""
        reason_lower = str(reason).lower()
        
        if 'would immediately match' in reason_lower or 'maker' in reason_lower:
            return 'maker'
        elif 'min_notional' in reason_lower:
            return 'notional'
        elif 'insufficient' in reason_lower or 'balance' in reason_lower:
            return 'balance'
        else:
            return 'other'
            
    def _record_rejection(self, rejection_type: str, order: OrderPlan, reason: str = ''):
        """记录拒单历史"""
        self.rejection_history.append({
            'timestamp': time.time(),
            'type': rejection_type,
            'order': order,
            'reason': reason
        })
        
    async def apply(self, deficit_buy_usd: float, deficit_sell_usd: float,
                   market: Dict, balances: Dict, limits: Dict,
                   volatility: float = 0.001, fills_per_min: float = 0) -> int:
        """
        应用动态流动性
        
        Args:
            deficit_buy_usd: 买侧缺口
            deficit_sell_usd: 卖侧缺口  
            market: 市场数据
            balances: 余额数据
            limits: 交易限制
            volatility: 价格波动率
            fills_per_min: 成交频率
            
        Returns:
            成功下单数量
        """
        if not self.cfg['enabled']:
            return 0
            
        # P1-2: Mirror 同步节拍检查
        import time
        if hasattr(self, 'order_mirror') and self.order_mirror:
            mirror_age = time.time() - (getattr(self.order_mirror, 'last_sync_time', 0) or 0)
            if mirror_age > 10:  # Mirror 超过10s未同步
                logger.debug(f"[DLE-Pro] Mirror数据过期({mirror_age:.1f}s)，暂缓补单")
                self.stats['skipped_mirror_stale'] = self.stats.get('skipped_mirror_stale', 0) + 1
                return 0
            
        # 同步Shadow Balance
        self.shadow.sync_actual_balance({
            'USDT': {'free': balances['usdt'], 'locked': 0},
            'DOGE': {'free': balances['doge'], 'locked': 0}
        })
        
        # Phase 7: 初始化AdaptiveSizer（如果需要）
        self._ensure_sizer_initialized(limits)
        
        # 更新自适应参数
        self._update_adaptive_params()
        
        # Phase 7: 新的智能计划逻辑
        logger.info(f"🔍 [Phase7.1-Apply] sizer={self.sizer is not None}, util_planner={self.util_planner is not None}")
        if self.sizer and self.util_planner:
            placed = await self._apply_phase7_logic(market, balances, limits, volatility, fills_per_min)
        else:
            # 备用：老版Phase 6逻辑
            equity = balances['usdt'] + balances['doge'] * market['mid']
            dynamic_cushion = self._calculate_dynamic_cushion(equity, volatility, fills_per_min)
            
            master_plan = self._generate_master_plan(
                deficit_buy_usd, deficit_sell_usd, market, limits, dynamic_cushion
            )
            
            self.stats['planned_total'] += len(master_plan)
            for order in master_plan:
                self.stats['planned_by_layer'][order.layer] += 1
                
            placed = await self._execute_plan(master_plan)
        
        # Phase 7: 不变量检查
        if hasattr(self, 'invariants'):
            equity = balances['usdt'] + balances['doge'] * market['mid']
            invariant_results = self._check_invariants(market, equity)
            
        # 输出日志
        if self.cfg['verbose']:
            awg_status = self.awg.get_usage_stats()
            shadow_status = self.shadow.get_summary()
            
            if hasattr(self, 'last_plan_result'):
                # Phase 7新日志格式
                plan = getattr(self, 'last_plan_result', {})
                logger.info(
                    f"[DLE Pro Phase7] 成功={placed} util={plan.get('util_eff', 0):.1%} "
                    f"买预算={sum(plan.get('buy', {}).values()):.1f} 卖预算={sum(plan.get('sell', {}).values()):.1f} "
                    f"压力模式={self.adaptive['stress_mode']} AWG=[{awg_status}] Shadow=[{shadow_status}]"
                )
            else:
                # 备用：老版日志格式
                logger.info(
                    f"[DLE Pro] 成功={placed} "
                    f"压力模式={self.adaptive['stress_mode']} AWG=[{awg_status}] Shadow=[{shadow_status}]"
                )
            
        return placed
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'stats': self.stats.copy(),
            'adaptive': self.adaptive.copy(),
            'active_price_orders': dict(self.active_price_orders),
            'rejection_history_size': len(self.rejection_history)
        }
        
    async def _apply_phase7_logic(self, market: Dict, balances: Dict, limits: Dict, volatility: float, fills_per_min: float) -> int:
        """应用Phase 7智能资金利用逻辑"""
        import time
        
        # ① 收集观测（都来自本地，不加REST压力）
        mid = market['mid']
        spread = market.get('spread', mid * 0.001)  # 默认点0.1%点差
        vol_30s = volatility or 0.03
        fill_rate = fills_per_min / 10.0 if fills_per_min else 0.3  # 简单转换
        bid_top_sz = market.get('bid_size_top', 100.0)
        ask_top_sz = market.get('ask_size_top', 100.0)
        
        mkt = MarketSnapshot(
            mid=mid, spread=spread,
            bid_size_top=bid_top_sz, ask_size_top=ask_top_sz,
            realized_vol_30s=vol_30s, recent_fill_rate=fill_rate
        )
        
        # ② 库存与余额（来自Shadow/UDS）
        avail_usdt = self.shadow.get_available('USDT')
        avail_doge = self.shadow.get_available('DOGE')
        total_doge_value = balances['doge'] * mid
        total_value = balances['usdt'] + total_doge_value
        doge_ratio = total_doge_value / total_value if total_value > 0 else 0.5
        
        inv = InventoryState(doge_ratio=doge_ratio, target_ratio=self.cfg.get('doge_target_ratio', 0.5))
        
        # ③ 风险信号
        risk_signals = {
            'awg': getattr(self.awg, 'state_name', lambda: 'NORMAL')(),
            'mirror_age': 0,
            'uds_age': 0
        }
        
        if hasattr(self, 'order_mirror') and hasattr(self.order_mirror, 'last_sync_time'):
            risk_signals['mirror_age'] = time.time() - (self.order_mirror.last_sync_time or 0)
        if hasattr(self, 'user_stream') and hasattr(self.user_stream, 'last_msg_ts'):
            risk_signals['uds_age'] = time.time() - (self.user_stream.last_msg_ts or 0)
        
        # ④ 预算
        plan = self.util_planner.plan(
            equity_usdt=total_value,
            price=mid,
            avail_usdt=avail_usdt,
            avail_doge=avail_doge,
            doge_ratio=doge_ratio,
            risk_signals=risk_signals,
            target_doge_ratio=self.cfg.get('doge_target_ratio', 0.5)
        )
        
        # 保存计划结果供日志使用
        self.last_plan_result = plan
        
        # ⑤ 逐层逐侧生成订单量
        master_plan = []
        
        for side in ['BUY', 'SELL']:
            side_budget_dict = plan[side.lower()]
            for layer in [0, 1, 2]:  # L0, L1, L2
                layer_budget = side_budget_dict.get(layer, 0.0)
                if layer_budget <= 0:
                    continue
                    
                # 获取该层级tick配置
                layer_name = f'L{layer}'
                if layer_name not in self.layers:
                    continue
                    
                layer_config = self.layers[layer_name]
                n_orders = len(layer_config.ticks)  # 每层订单数 = tick数量
                budget_per_order = layer_budget / n_orders if n_orders > 0 else 0
                
                for i, tick_offset in enumerate(layer_config.ticks):
                    # 计算价格
                    tick_size = limits.get('tick_size', mid * 0.0001)
                    if side == 'BUY':
                        desired_price = mid - tick_offset * tick_size
                    else:  # SELL
                        desired_price = mid + tick_offset * tick_size
                        
                    # Maker-Guard保护
                    best_bid = market.get('best_bid', mid * 0.999)
                    best_ask = market.get('best_ask', mid * 1.001)
                    final_price = self._maker_guard_price(side, desired_price, best_bid, best_ask, tick_size)
                    
                    # 智能量计算
                    qty = self.sizer.suggest_qty(
                        side=side,
                        price=final_price,
                        layer=layer,
                        budget_per_order=budget_per_order,
                        avail_usdt=avail_usdt,
                        avail_doge=avail_doge,
                        mkt=mkt,
                        inv=inv
                    )
                    
                    if qty <= 0:
                        continue
                        
                    # 交易所规则对齐
                    final_qty = self._align_qty_notional(
                        qty, final_price, 
                        limits.get('step_size', 1.0), 
                        limits.get('min_notional', 10.0)
                    )
                    
                    if final_qty > 0:
                        order_plan = OrderPlan(
                            side=side,
                            price=final_price,
                            qty=final_qty,
                            layer=layer_name,
                            ttl_ms=layer_config.ttl_ms,
                            priority=layer_config.priority
                        )
                        master_plan.append(order_plan)
        
        # 统计计划
        self.stats['planned_total'] += len(master_plan)
        for order in master_plan:
            self.stats['planned_by_layer'][order.layer] += 1
        
        # 执行计划
        placed = await self._execute_plan(master_plan)
        
        return placed
        
    def onbook_from_live_orders(self):
        """Phase 6 Fix 1: 精确按订单方向累计，拒绝50/50平分"""
        buy_notional = sell_notional = 0.0
        for oid, info in self.live_orders.items():
            # 只统计活跃订单（NEW或PARTIALLY_FILLED状态）
            status = info.get('status', 'NEW')
            if status in ('NEW', 'PARTIALLY_FILLED'):
                price = float(info.get('price', 0))
                if info.get('reserve_asset') == 'DOGE':
                    # SELL订单：DOGE数量 * 价格
                    notional = float(info.get('reserve_amount', 0)) * price
                    sell_notional += notional  # 卖单锁DOGE
                else:
                    # BUY订单：直接是USDT数量
                    notional = float(info.get('reserve_amount', 0))
                    buy_notional += notional   # 买单锁USDT
        return round(buy_notional, 2), round(sell_notional, 2)
    
    def get_status_line(self) -> str:
        """获取状态行（Phase 7.1增强版）"""
        import time
        
        # Phase 7.1分支指示器
        phase_branch = "Phase6" if not (self.sizer and self.util_planner) else "Phase7.1✅"
        
        base_info = (
            f"DLE_Pro[{phase_branch}](计划={self.stats['planned_total']} "
            f"成功={self.stats['placed_total']} "
            f"AWG拒={self.stats['rejected_awg']} "
            f"Shadow拒={self.stats['rejected_shadow']} "
            f"Maker拒={self.stats['rejected_maker']} "
            f"压力={self.adaptive['stress_mode']})"
        )
        
        # Phase 7增强信息
        if hasattr(self, 'last_plan_result') and hasattr(self, 'invariants'):
            plan = self.last_plan_result
            util_eff = plan.get('util_eff', 0.0)
            target_onbook = plan.get('target_onbook', 0.0)
            
            # 计算实际onbook
            actual_onbook = 0.0
            if hasattr(self, 'live_orders'):
                actual_onbook = sum(order['price'] * order['remain_qty'] 
                                  for order in self.live_orders.values())
            
            # 风险信号年龄
            uds_age = mirror_age = 0.0
            if hasattr(self, 'user_stream') and hasattr(self.user_stream, 'last_msg_ts'):
                uds_age = time.time() - (self.user_stream.last_msg_ts or 0)
            if hasattr(self, 'order_mirror') and hasattr(self.order_mirror, 'last_sync_time'):
                mirror_age = time.time() - (self.order_mirror.last_sync_time or 0)
            
            # AWG状态
            awg_state = 'unknown'
            if hasattr(self.awg, 'state_name'):
                awg_state = self.awg.state_name()
            
            phase7_info = (
                f" Phase7[util={util_eff:.1%}(target={target_onbook:.1f}) "
                f"onbook={actual_onbook:.1f} uds_age={uds_age:.1f}s "
                f"mirror_age={mirror_age:.1f}s awg={awg_state} "
                f"violations(budget={self.invariants.get('budget_violations', 0)}, "
                f"shadow={self.invariants.get('shadow_mismatches', 0)}, "
                f"stale={self.invariants.get('data_stale_events', 0)})]"
            )
            
            return base_info + phase7_info
        else:
            return base_info
        
    def reset_stats(self):
        """重置统计"""
        for key in self.stats:
            if isinstance(self.stats[key], dict):
                self.stats[key].clear()
            else:
                self.stats[key] = 0
        self.rejection_history.clear()
        self.active_price_orders.clear()
    
    def _price_key(self, px: float) -> str:
        """统一价位键，避免浮点误差"""
        return f"{px:.5f}"

    def on_order_closed(self, order_id_or_price, price=None):
        """订单关闭回调（支持两种签名）"""
        if price is None:
            # 旧签名：on_order_closed(price)
            price = order_id_or_price
            key = self._price_key(price)
            cnt = self.active_price_orders.get(key, 0)
            if cnt > 0:
                self.active_price_orders[key] = cnt - 1
                logger.debug(f"[DLE-Pro] 价位 {price} 计数回落: {cnt} -> {cnt-1}")
        else:
            # 新签名：on_order_closed(order_id, price) - 优先使用统一关闭处理
            order_id = order_id_or_price
            if order_id in self.live_orders:
                self._close_and_release(order_id)
            else:
                # 兜底：仅做价位回落
                key = self._price_key(price)
                cnt = self.active_price_orders.get(key, 0)
                if cnt > 0:
                    self.active_price_orders[key] = cnt - 1
                    logger.debug(f"[DLE-Pro] 价位 {price} 计数回落: {cnt} -> {cnt-1}")
            
    async def register_order_from_uds(self, order_id: str, side: str, price: float, orig_qty: float):
        """UDS Phase 1: 从User Data Stream事件登记新订单"""
        async with self._live_lock:
            if order_id not in self.live_orders:
                reserve_asset = 'USDT' if side == 'BUY' else 'DOGE'
                reserve_amount = (orig_qty * price) if side == 'BUY' else orig_qty
                
                self.live_orders[order_id] = {
                    'cid': f"UDS-{order_id}",  # UDS订单没有client_order_id
                    'side': side,
                    'price': price,
                    'orig_qty': orig_qty,
                    'filled_qty': 0.0,
                    'remain_qty': orig_qty,
                    'reserve_asset': reserve_asset,
                    'reserve_amount': reserve_amount,
                    'timestamp': time.time() * 1000
                }
                
                # 更新价位计数
                key = self._price_key(price)
                self.active_price_orders[key] = self.active_price_orders.get(key, 0) + 1
    
    async def update_filled_from_uds(self, order_id: str, filled_qty: float):
        """UDS Phase 1: 从User Data Stream更新成交量"""
        async with self._live_lock:
            if order_id in self.live_orders:
                info = self.live_orders[order_id]
                info['filled_qty'] = filled_qty
                info['remain_qty'] = info['orig_qty'] - filled_qty
                
                # 如果完全成交，触发释放流程
                if info['remain_qty'] <= 0:
                    await self._close_and_release(order_id)
    
    async def _close_and_release(self, order_id: str):
        """统一的订单关闭处理（供 Mirror/TTL 共用）"""
        # Phase 6 P0-1: ① 优先取消TTL任务，避免已关单后TTL再打一枪
        await self.cancel_ttl(order_id)
        
        # Phase 6 P0-2: 使用锁保护live_orders
        async with self._live_lock:
            info = self.live_orders.pop(order_id, None)
            if not info: 
                return
            
        # ② 释放Shadow（幂等）
        try:
            self.shadow.release(info['cid'], reason='closed')
            logger.debug(f"[DLE-Pro] Shadow释放成功: {order_id} -> {info['cid']}")
        except Exception as e:
            logger.warning(f"[DLE-Pro] Shadow释放失败 {order_id}: {e}")
            
        # ③ 价位计数回落
        self.on_order_closed(info['price'])

    def register_order_local(self, oid: str, side: str, price: float, orig_qty: float, filled_qty: float, ts: float):
        """Phase 4 Patch C: 立刻登记live_orders，避免onbook=0"""
        reserve_asset = 'USDT' if side == 'BUY' else 'DOGE'
        reserve_amount = (orig_qty * price) if side == 'BUY' else orig_qty
        
        self.live_orders[oid] = {
            "side": side, 
            "price": price, 
            "orig_qty": orig_qty,
            "filled_qty": filled_qty, 
            "remain_qty": max(0.0, orig_qty - filled_qty),
            "ts": ts, 
            "src": "local_submit",
            "reserve_asset": reserve_asset,
            "reserve_amount": reserve_amount,
            "timestamp": time.time() * 1000
        }
        self._recompute_onbook_src_hint("live")  # 让状态行 src=live

    def _recompute_onbook_src_hint(self, hint: str):
        """Phase 4 Patch C: 更新onbook数据源提示"""
        # 设置源提示属性，供状态行使用
        if not hasattr(self, '_onbook_src_hint'):
            self._onbook_src_hint = hint
        else:
            self._onbook_src_hint = hint

    async def _schedule_ttl_cancel(self, order_id: str, price: float, ttl_ms: int):
        """TTL撤单调度 - 完善版"""
        import asyncio
        try:
            # 等待TTL
            await asyncio.sleep(ttl_ms / 1000.0)
            
            # Phase 6 P0-1: sleep之后先短路检查
            if order_id not in self.live_orders:
                logger.debug(f"[DLE-Pro] TTL跳过: {order_id} 已关闭/释放")
                return
            
            # 检查AWG配额
            if not self.awg.acquire('cancel'):
                logger.warning(f"[DLE-Pro] TTL撤单 {order_id} AWG配额不足")
                return
                
            # 执行撤单
            try:
                result = await self.exchange.cancel_order(order_id=order_id, symbol='DOGEUSDT')
                if result:
                    logger.info(f"[DLE-Pro] TTL撤单成功: {order_id}")
                    # 使用统一的关闭处理
                    self._close_and_release(order_id)
                else:
                    logger.warning(f"[DLE-Pro] TTL撤单失败: {order_id}")
            except Exception as cancel_e:
                logger.error(f"[DLE-Pro] TTL撤单异常 {order_id}: {cancel_e}")
                
        except asyncio.CancelledError:
            logger.debug(f"[DLE-Pro] TTL任务被取消: {order_id}")
        except Exception as e:
            logger.error(f"[DLE-Pro] TTL调度异常 {order_id}: {e}")
        finally:
            # 清理任务引用
            self.ttl_tasks.pop(order_id, None)
    
    async def schedule_ttl(self, order_id: str, price: float, ttl_ms: int):
        """调度TTL撤单任务"""
        import asyncio
        if ttl_ms <= 0:
            return
            
        # Phase 6 P0-2: 使用锁保护ttl_tasks
        async with self._ttl_lock:
            # 取消旧任务（如果存在）
            if order_id in self.ttl_tasks:
                self.ttl_tasks[order_id].cancel()
                
            # 创建新任务
            task = asyncio.create_task(self._schedule_ttl_cancel(order_id, price, ttl_ms))
            self.ttl_tasks[order_id] = task
            logger.debug(f"[DLE-Pro] 调度TTL撤单: {order_id} in {ttl_ms}ms")
        
    async def cancel_ttl(self, order_id: str):
        """取消TTL任务（订单已手动撤销或成交）"""
        async with self._ttl_lock:
            if order_id in self.ttl_tasks:
                self.ttl_tasks[order_id].cancel()
                del self.ttl_tasks[order_id]
                logger.debug(f"[DLE-Pro] 取消TTL任务: {order_id}")


# 全局实例
_dle_pro_instance = None

def get_dle_pro(exchange=None, config: Dict = None) -> DLEPro:
    """获取DLE Pro实例（单例模式）"""
    global _dle_pro_instance
    
    if _dle_pro_instance is None:
        if exchange is None:
            raise ValueError("First call to get_dle_pro requires exchange parameter")
        _dle_pro_instance = DLEPro(exchange, config or {})
        logger.info("[DLE-Pro] 创建新实例")
    
    return _dle_pro_instance

def reset_dle_pro():
    """重置DLE Pro实例（测试用）"""
    global _dle_pro_instance
    _dle_pro_instance = None


if __name__ == "__main__":
    # 简单测试
    from ..connectors.mock_exchange import MockExchange
    
    async def test_dle_pro():
        mock_ex = MockExchange()
        dle = DLEPro(mock_ex)
        
        # 模拟市场数据
        market = {'mid': 0.24, 'bid': 0.23995, 'ask': 0.24005}
        balances = {'usdt': 1000, 'doge': 5000}
        limits = {'tick': 0.00001, 'step': 1.0, 'min_notional': 5.0}
        
        # 应用DLE
        placed = await dle.apply(100, 100, market, balances, limits)
        print(f"成功下单: {placed}")
        print(f"统计: {dle.get_stats()}")
        
    import asyncio
    asyncio.run(test_dle_pro())