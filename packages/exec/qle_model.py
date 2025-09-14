"""
Phase 9: 队列位置与成交强度模型 (QLE + Fill-intensity)
对标世界级做市商的队列排位估计与期望成交时间计算
"""
import time
import math
import logging
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


class QueueTracker:
    """Phase 9 C Fix 2: 队位快照+消耗模型（对标顶级做市商）"""
    
    def __init__(self):
        self.snapshots = {}  # key: (side, price)
        
    def on_our_order_place(self, side: str, price: float, best_qty: float):
        """记录队位快照"""
        key = (side, price)
        self.snapshots[key] = {
            "ts": time.time(),
            "qty_at_snapshot": best_qty,
            "consumed": 0.0
        }
        logger.info(f"[QLE] snapshot side={side} px={price:.5f} best_qty={best_qty:.1f}")
    
    def on_agg_trade(self, side: str, price: float, trade_qty: float, is_taker_sell: bool):
        """更新消耗量（方向过滤）"""
        # BUY订单被taker卖吃掉，SELL订单被taker买吃掉
        if (side == 'BUY' and is_taker_sell) or (side == 'SELL' and not is_taker_sell):
            key = (side, price)
            if key in self.snapshots:
                old_consumed = self.snapshots[key]["consumed"]
                self.snapshots[key]["consumed"] += trade_qty
                ahead_est = self.get_ahead_estimate(side, price)
                
                # P0-1 Fix: 提升consume打点级别到INFO，增强证据可见性
                logger.info(f"[QLE] consume side={side} at {price:.5f} Δ={trade_qty:.1f} "
                          f"total_consumed={self.snapshots[key]['consumed']:.1f} ahead_qty={ahead_est:.1f}")
                
                # Phase 3 增强：记录消耗事件用于差分逻辑
                self._record_consume_event(side, price, trade_qty, ahead_est)
    
    def get_ahead_estimate(self, side: str, price: float) -> float:
        """获取队位估计"""
        key = (side, price)
        if key not in self.snapshots:
            return 0.0
        snap = self.snapshots[key]
        return max(snap["qty_at_snapshot"] - snap["consumed"], 0.0)
    
    def reset_price_level(self, side: str, price: float):
        """价位变动时重置"""
        key = (side, price)
        if key in self.snapshots:
            del self.snapshots[key]
    
    def _record_consume_event(self, side: str, price: float, delta_qty: float, ahead_est: float):
        """P0-1 Fix: 记录消耗事件，用于差分逻辑和证据链路"""
        # 简单差分逻辑：记录消耗趋势
        if not hasattr(self, '_consume_events'):
            self._consume_events = deque(maxlen=100)  # 保留最近100个消耗事件
        
        event = {
            'ts': time.time(),
            'side': side,
            'price': price,
            'delta': delta_qty,
            'ahead': ahead_est,
        }
        self._consume_events.append(event)
    
    def record_our_orders(self, side: str, price: float, total_qty: float, earliest_ts: float = None):
        """P0-1 Fix: 统一记录我方订单数量，用于证据闭环"""
        if earliest_ts is None:
            earliest_ts = time.time()
            
        # 打点our_orders信息
        logger.info(f"[QLE] our_orders at {price:.5f}: side={side} total={total_qty:.1f} earliest_ts={earliest_ts:.3f}")
        
        # 更新快照中的参考信息（可选）
        key = (side, price)
        if key in self.snapshots:
            self.snapshots[key]['our_qty'] = total_qty
            self.snapshots[key]['our_earliest'] = earliest_ts


def ewma(prev: Optional[float], new: float, alpha: float = 0.2) -> float:
    """Phase 9 C Fix 2: EWMA平滑（避免噪声）"""
    return new if prev is None else (alpha * new + (1 - alpha) * prev)


class QueuePositionEstimator:
    """队列位置与成交强度模型 - 对标Jane Street/Citadel"""
    
    def __init__(self):
        # 参数配置
        self.history_window = 30.0  # 30秒历史窗口
        self.min_samples = 5        # 最小样本数
        
        # 历史数据存储
        self.trade_history = deque()     # (timestamp, qty, side, price)
        self.cancel_history = deque()    # (timestamp, qty, side, price)
        self.depth_snapshots = deque()   # (timestamp, bids, asks)
        
        # 成交强度指标
        self.last_trade_intensity = {'BUY': 0.0, 'SELL': 0.0}
        self.last_cancel_rate = {'BUY': 0.0, 'SELL': 0.0}
        
        # 队列位置缓存
        self.queue_position_cache = {}
        self.last_update_time = 0
        
        # Phase 9 C Fix: Connector引用（用于获取aggTrade数据）
        self.connector = None
        
        # Phase 9 C Fix 2: 新增组件
        self.queue_tracker = QueueTracker()  # 快照+消耗模型
        self.get_our_orders = None           # 获取我方订单的回调
        self.take_rate_ewma = None           # EWMA平滑的take_rate
        self.cancel_rate_ewma = None         # EWMA平滑的cancel_rate
        
        # B3: 微价格与危险率模型
        self.microprice_cache = None
        self.microprice_update_time = 0
        self.fill_hazard_cache = {'BUY': 0.0, 'SELL': 0.0}
        self.queue_metrics = {'BUY': {}, 'SELL': {}}  # q_bid, q_ask, arrivals_λ
        
        logger.info("[QLE] QueuePositionEstimator initialized")
    
    def set_connector(self, connector):
        """Phase 9 C Fix: 设置connector引用以获取aggTrade数据"""
        self.connector = connector
        logger.info("[QLE] Connector reference set for aggTrade data access")
    
    def set_get_our_orders(self, callback):
        """Phase 9 C Fix 2: 设置获取我方订单的回调"""
        self.get_our_orders = callback
        logger.info("[QLE] get_our_orders callback set")
    
    def update_trade(self, qty: float, side: str, price: float):
        """更新成交记录"""
        now = time.time()
        self.trade_history.append((now, qty, side, price))
        self._cleanup_old_data()
        
    def update_cancel(self, qty: float, side: str, price: float):
        """更新撤单记录"""
        now = time.time()
        self.cancel_history.append((now, qty, side, price))
        self._cleanup_old_data()
        
    def update_depth(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]):
        """更新订单簿深度"""
        now = time.time()
        self.depth_snapshots.append((now, bids[:10], asks[:10]))  # 保留前10档
        self._cleanup_old_data()
        
    def _cleanup_old_data(self):
        """清理过期数据"""
        now = time.time()
        cutoff = now - self.history_window
        
        while self.trade_history and self.trade_history[0][0] < cutoff:
            self.trade_history.popleft()
        while self.cancel_history and self.cancel_history[0][0] < cutoff:
            self.cancel_history.popleft()
        while self.depth_snapshots and self.depth_snapshots[0][0] < cutoff:
            self.depth_snapshots.popleft()
    
    def calculate_trade_intensity(self, side: str, level: str = 'ALL', target_price: float = None) -> float:
        """Phase 9 C Fix 2: 增强的成交强度计算 - 方向过滤 + EWMA平滑"""
        
        logger.debug(f"[QLE] calculate_trade_intensity side={side} level={level} target_price={target_price}")
        
        # Phase 9 C Fix 2: 使用connector的aggTrade数据（带方向过滤）
        if self.connector and hasattr(self.connector, 'ws_trades') and target_price:
            try:
                # 获取深度数据以计算自适应价差容忍
                depth = getattr(self.connector, 'latest_depth', {})
                bid = depth.get('bids', [[0.24, 0]])[0][0] if depth.get('bids') else 0.24
                ask = depth.get('asks', [[0.25, 0]])[0][0] if depth.get('asks') else 0.25
                spread_ticks = max(1, int((ask - bid) / 0.00001))
                tolerance_ticks = max(1, min(spread_ticks // 2, 6))
                
                logger.debug(f"[QLE] price_window ticks={tolerance_ticks} spread_ticks={spread_ticks}")
                
                # 方向过滤：BUY单看taker卖，SELL单看taker买
                now = time.time()
                relevant_trades = []
                direction = "to_bid" if side == 'BUY' else "to_ask"
                
                for trade in self.connector.ws_trades:
                    if now - trade['ts'] > 30:  # 30秒窗口
                        continue
                    
                    # 价格容忍范围
                    price_diff = abs(trade['price'] - target_price)
                    if price_diff > tolerance_ticks * 0.00001:
                        continue
                    
                    # 方向过滤
                    is_buyer_maker = trade.get('is_maker', trade.get('isBuyerMaker', False))
                    if side == 'BUY' and is_buyer_maker:  # taker卖打过来
                        relevant_trades.append(trade)
                    elif side == 'SELL' and not is_buyer_maker:  # taker买打过来
                        relevant_trades.append(trade)
                
                if relevant_trades:
                    total_vol = sum(t['qty'] for t in relevant_trades)
                    time_span = max(1.0, now - relevant_trades[0]['ts'])
                    raw_rate = total_vol / time_span
                    
                    # EWMA平滑
                    self.take_rate_ewma = ewma(self.take_rate_ewma, raw_rate)
                    
                    logger.info(f"[aggTrade] target={target_price:.5f} matches={len(relevant_trades)} "
                              f"vol={total_vol:.1f} rate={raw_rate:.1f}/s dir={direction}")
                    logger.debug(f"[QLE] take_rate_raw={raw_rate:.1f} ewma={self.take_rate_ewma:.1f}")
                    
                    return self.take_rate_ewma if self.take_rate_ewma else raw_rate
                    
            except Exception as e:
                logger.debug(f"[QLE] aggTrade处理失败: {e}")
        
        # 回退到原有历史数据计算
        if len(self.trade_history) < self.min_samples:
            return 0.0
            
        now = time.time()
        relevant_trades = [
            (ts, qty, price) for ts, qty, trade_side, price in self.trade_history 
            if trade_side == side and now - ts <= self.history_window
        ]
        
        if not relevant_trades:
            return 0.0
        
        # 根据level筛选交易
        if level == 'L1':
            filtered_trades = self._filter_l1_trades(relevant_trades, side)
        elif level == 'L2':
            filtered_trades = self._filter_l2_trades(relevant_trades, side)
        else:
            filtered_trades = relevant_trades
        
        if not filtered_trades:
            return 0.0
            
        # 使用加权平均计算强度 (最近的交易权重更高)
        total_weighted_qty = 0.0
        total_weight = 0.0
        
        for ts, qty, price in filtered_trades:
            age = now - ts
            # 指数衰减权重：最近的交易权重更高
            weight = math.exp(-age / (self.history_window / 3))
            total_weighted_qty += qty * weight
            total_weight += weight
        
        if total_weight <= 0:
            return 0.0
        
        # 计算加权平均强度
        avg_qty_per_trade = total_weighted_qty / total_weight
        trade_frequency = len(filtered_trades) / self.history_window
        
        intensity = avg_qty_per_trade * trade_frequency
        self.last_trade_intensity[side] = intensity
        return intensity
    
    def _filter_l1_trades(self, trades: List[Tuple[float, float, float]], side: str) -> List[Tuple[float, float, float]]:
        """过滤L1层级的交易 - 在最优价位附近"""
        if not trades:
            return []
        
        # 获取最近的价格作为参考
        latest_price = trades[-1][2]
        tick_size = 0.00001
        
        # L1定义：在最优价位±1 tick内的交易
        l1_threshold = tick_size * 1.5
        
        return [
            (ts, qty, price) for ts, qty, price in trades
            if abs(price - latest_price) <= l1_threshold
        ]
    
    def _filter_l2_trades(self, trades: List[Tuple[float, float, float]], side: str) -> List[Tuple[float, float, float]]:
        """过滤L2层级的交易 - 远离最优价位"""
        if not trades:
            return []
        
        latest_price = trades[-1][2]
        tick_size = 0.00001
        l1_threshold = tick_size * 1.5
        l2_threshold = tick_size * 5.0
        
        return [
            (ts, qty, price) for ts, qty, price in trades
            if l1_threshold < abs(price - latest_price) <= l2_threshold
        ]
    
    def calculate_cancel_rate(self, side: str) -> float:
        """计算撤单率 (qty/sec)"""
        if len(self.cancel_history) < self.min_samples:
            return 0.0
            
        now = time.time()
        relevant_cancels = [
            (ts, qty) for ts, qty, cancel_side, price in self.cancel_history
            if cancel_side == side and now - ts <= self.history_window
        ]
        
        if not relevant_cancels:
            return 0.0
            
        total_qty = sum(qty for ts, qty in relevant_cancels)
        time_span = max(1.0, now - relevant_cancels[0][0])
        
        cancel_rate = total_qty / time_span
        self.last_cancel_rate[side] = cancel_rate
        return cancel_rate
    
    def estimate_queue_position(self, side: str, price: float, current_depth: Dict, 
                               level: str = 'L1', mode: str = 'candidate', order_id: str = None) -> float:
        """Phase 9 C Fix 2: 专业化排队位置估计 - 新单vs既有单分离
        
        mode='candidate': 新单或替换到新价层（我方已有量算在ahead里）
        mode='existing': 对已有订单做QLE评估（减去我方已有量）
        """
        try:
            # 首先尝试使用快照+消耗模型
            snapshot_ahead = self.queue_tracker.get_ahead_estimate(side, price)
            if snapshot_ahead > 0:
                logger.debug(f"[QLE] using snapshot ahead={snapshot_ahead:.1f}")
                return snapshot_ahead
            
            # 计算基础ahead_qty
            if side == 'BUY':
                best_bid = current_depth.get('best_bid', price)
                bid_qty = current_depth.get('bid_qty', 0)
                
                if price < best_bid:
                    # L2或更远层级
                    if level == 'L2':
                        base_ahead = bid_qty + self._estimate_l2_queue_buy(price, current_depth)
                    else:
                        base_ahead = bid_qty * 2.0
                elif price == best_bid:
                    # L1层级
                    if level == 'L1':
                        queue_density = self._calculate_queue_density(side)
                        base_ahead = bid_qty * (0.3 + 0.4 * queue_density)
                    else:
                        base_ahead = bid_qty * 0.5
                else:
                    return 0.0  # 比最优价更好
                    
            else:  # SELL
                best_ask = current_depth.get('best_ask', price)
                ask_qty = current_depth.get('ask_qty', 0)
                
                if price > best_ask:
                    # L2或更远层级
                    if level == 'L2':
                        base_ahead = ask_qty + self._estimate_l2_queue_sell(price, current_depth)
                    else:
                        base_ahead = ask_qty * 2.0
                elif price == best_ask:
                    # L1层级
                    if level == 'L1':
                        queue_density = self._calculate_queue_density(side)
                        base_ahead = ask_qty * (0.3 + 0.4 * queue_density)
                    else:
                        base_ahead = ask_qty * 0.5
                else:
                    return 0.0
            
            # Phase 9 C Fix 2: 根据mode调整我方订单的处理
            our_at_price, our_ahead_of_me = self._our_qty_profile(side, price, order_id)
            
            if mode == 'candidate':
                # 新单或替换到新价层：我方已有量都在我前面
                final_ahead = base_ahead + our_at_price
                logger.debug(f"[QLE] mode=candidate base={base_ahead:.1f} our={our_at_price:.1f} ahead={final_ahead:.1f}")
            else:
                # existing：已有单，减去我方在后面的量
                final_ahead = max(0.0, base_ahead - (our_at_price - our_ahead_of_me))
                logger.debug(f"[QLE] mode=existing base={base_ahead:.1f} our={our_at_price:.1f} "
                           f"ahead_of_me={our_ahead_of_me:.1f} ahead={final_ahead:.1f}")
            
            return final_ahead
                    
        except Exception as e:
            logger.warning(f"[QLE] Queue position estimation error: {e}")
            return 0.0
    
    def _our_qty_profile(self, side: str, price: float, order_id: str = None) -> Tuple[float, float]:
        """Phase 9 C Fix 2: 计算我方在该价层的订单分布
        
        Returns:
            our_at_price: 我们在该价档的总未成交量
            our_ahead_of_me: 时间戳早于order_id的我方量（仅existing模式需要）
        """
        our_at_price = 0.0
        our_ahead_of_me = 0.0
        
        try:
            # 使用事件驱动的订单数据源
            if self.get_our_orders:
                our_orders = self.get_our_orders()
                
                orders_at_price = [o for o in our_orders 
                                 if o.get('side') == side and abs(o.get('price', 0) - price) < 0.000001]
                
                if orders_at_price:
                    our_at_price = sum(o.get('remaining_qty', 0) for o in orders_at_price)
                    logger.info(f"[QLE] our_orders at {price:.5f} : cnt={len(orders_at_price)} "
                              f"total={our_at_price:.1f} earliest_ts={min(o.get('ts', 0) for o in orders_at_price)}")
                    
                    # 如果指定了order_id，计算在它前面的量
                    if order_id:
                        target_order = next((o for o in orders_at_price if o.get('order_id') == order_id), None)
                        if target_order:
                            target_ts = target_order.get('ts', float('inf'))
                            our_ahead_of_me = sum(o.get('remaining_qty', 0) for o in orders_at_price 
                                                if o.get('ts', 0) < target_ts)
            else:
                # 回退到connector.open_orders（可能陈旧）
                logger.debug("[QLE] get_our_orders not available, using fallback")
                our_at_price = self._calculate_our_queue_position(side, price)
                
        except Exception as e:
            logger.warning(f"[QLE] _our_qty_profile error: {e}")
            
        return our_at_price, our_ahead_of_me
    
    def _estimate_l2_queue_buy(self, price: float, current_depth: Dict) -> float:
        """估计L2买侧队列长度"""
        # 简化版：基于价差估计L2队列密度
        best_bid = current_depth.get('best_bid', price)
        tick_distance = max(1, abs(price - best_bid) / 0.00001)  # tick数量
        return tick_distance * 50.0  # 每个tick估计50的队列
    
    def _estimate_l2_queue_sell(self, price: float, current_depth: Dict) -> float:
        """估计L2卖侧队列长度"""
        best_ask = current_depth.get('best_ask', price)
        tick_distance = max(1, abs(price - best_ask) / 0.00001)
        return tick_distance * 50.0
    
    def _calculate_queue_density(self, side: str) -> float:
        """计算队列密度 [0,1] - 基于成交vs撤单比例"""
        take_rate = self.last_trade_intensity.get(side, 0)
        cancel_rate = self.last_cancel_rate.get(side, 0)
        total_activity = take_rate + cancel_rate
        
        if total_activity <= 0:
            return 0.5  # 默认中等密度
        
        # 成交比例越高，队列密度越高（竞争越激烈）
        fill_ratio = take_rate / total_activity
        return min(1.0, max(0.0, fill_ratio))
    
    def _calculate_our_queue_position(self, side: str, target_price: float, price_tolerance: float = 0.00001) -> float:
        """Phase 9 C Fix Step 3: 计算我方在指定价层的队列位置
        
        Args:
            side: 'BUY' or 'SELL'
            target_price: 目标价格
            price_tolerance: 价格容差（默认1 tick）
            
        Returns:
            我方在该价层的总订单量 (如果没有订单则返回0)
        """
        try:
            # 尝试从connector获取我方订单信息
            if not self.connector or not hasattr(self.connector, 'open_orders'):
                return 0.0
                
            our_orders = getattr(self.connector, 'open_orders', [])
            if not our_orders:
                return 0.0
            
            our_qty_at_price = 0.0
            
            for order in our_orders:
                if not isinstance(order, dict):
                    continue
                    
                order_side = order.get('side', '').upper()
                order_price = float(order.get('price', 0))
                order_qty = float(order.get('origQty', 0)) - float(order.get('executedQty', 0))
                
                # 只统计同侧订单
                if order_side != side.upper():
                    continue
                    
                # 检查价格是否在容差范围内
                if abs(order_price - target_price) <= price_tolerance:
                    our_qty_at_price += order_qty
                    
            return our_qty_at_price
            
        except Exception as e:
            logger.debug(f"[QLE] Calculate our queue position error: {e}")
            return 0.0
    
    def calculate_eta(self, side: str, price: float, current_depth: Dict) -> float:
        """计算期望成交时间 E[τ] (毫秒)"""
        # 1. 估计排在我前面的量
        ahead_qty = self.estimate_queue_position(side, price, current_depth)
        
        # 2. 计算成交强度和撤单率
        take_rate = self.calculate_trade_intensity(side)
        cancel_rate = self.calculate_cancel_rate(side)
        
        # 3. 计算净成交速度 (考虑撤单竞争)
        # 当没有足够历史数据时，使用保守估计
        if take_rate == 0.0 and cancel_rate == 0.0:
            net_fill_rate = 1.0  # 默认每秒1个DOGE成交速度
        else:
            net_fill_rate = max(0.1, take_rate - cancel_rate * 0.3)  # 30%撤单影响
        
        # 4. 期望成交时间
        if net_fill_rate <= 0.1:
            eta_seconds = 30.0  # 30秒默认，而非5分钟
        else:
            eta_seconds = ahead_qty / net_fill_rate
            eta_seconds = min(eta_seconds, 300.0)  # 最大5分钟
        
        return eta_seconds * 1000.0  # 转换为毫秒
    
    def recommend_action(self, eta_ms: float, ttl_ms: float, level: str = 'L1') -> str:
        """增强的自适应撤换策略 - 基于eta vs TTL关系优化撤换时机"""
        eta_ttl_ratio = eta_ms / max(1.0, ttl_ms)
        
        # 根据层级调整策略阈值
        if level == 'L1':
            # L1更激进的撤换策略
            high_threshold = 0.85  # 85%
            extend_threshold = 0.25 # 25%
            reduce_threshold = 0.08 # 8%
        elif level == 'L2':
            # L2更保守的撤换策略  
            high_threshold = 0.95  # 95%
            extend_threshold = 0.35 # 35%
            reduce_threshold = 0.12 # 12%
        else:
            # 默认策略
            high_threshold = 0.9
            extend_threshold = 0.3
            reduce_threshold = 0.1
        
        if eta_ttl_ratio > high_threshold:
            return "cancel_replace"  # 立即撤换 - 时间不够了
        elif eta_ttl_ratio > 0.7:
            return "prepare_replace" # 准备撤换 - 开始监控
        elif eta_ttl_ratio < extend_threshold:
            if eta_ttl_ratio < reduce_threshold:
                return "size_up"     # 太激进了，可以加量
            else:
                return "extend_ttl"  # 延长TTL或保持
        else:
            return "optimal"         # 当前时机最优
    
    def calculate_optimal_ttl(self, eta_ms: float, current_ttl_ms: float, level: str = 'L1') -> float:
        """计算最优TTL - 基于期望成交时间"""
        # 目标：TTL应该是eta的1.2-1.5倍，留出安全边际
        if level == 'L1':
            safety_multiplier = 1.3  # L1需要更快响应
            min_ttl = 3000   # 3秒最小TTL
            max_ttl = 12000  # 12秒最大TTL
        elif level == 'L2':
            safety_multiplier = 1.5  # L2可以更耐心
            min_ttl = 8000   # 8秒最小TTL
            max_ttl = 30000  # 30秒最大TTL
        else:
            safety_multiplier = 1.4
            min_ttl = 5000
            max_ttl = 20000
        
        optimal_ttl = eta_ms * safety_multiplier
        
        # 限制在合理范围内
        optimal_ttl = max(min_ttl, min(max_ttl, optimal_ttl))
        
        # 平滑调整：不要剧烈变化TTL
        adjustment_rate = 0.3  # 30%调整率
        new_ttl = current_ttl_ms * (1 - adjustment_rate) + optimal_ttl * adjustment_rate
        
        return new_ttl
    
    def analyze_order(self, side: str, price: float, ttl_ms: float, current_depth: Dict, 
                     level: str = 'L1', mode: str = 'candidate', order_id: str = None) -> Dict[str, Any]:
        """Phase 9 C Fix 2: 增强的订单队列分析 - 支持新单vs既有单分离"""
        # 基础计算 - 使用增强的方法
        ahead_qty = self.estimate_queue_position(side, price, current_depth, level, mode, order_id)
        # Phase 9 C Fix Step 4: 传递price参数以启用aggTrade数据
        take_rate = self.calculate_trade_intensity(side, level, price)
        cancel_rate = self.calculate_cancel_rate(side)
        eta_ms = self.calculate_eta(side, price, current_depth)
        action = self.recommend_action(eta_ms, ttl_ms, level)
        
        # 计算最优TTL建议
        optimal_ttl_ms = self.calculate_optimal_ttl(eta_ms, ttl_ms, level)
        
        # 计算额外指标
        eta_ttl_ratio = eta_ms / max(1.0, ttl_ms)
        fill_probability = self._calculate_fill_probability(eta_ms, ttl_ms, level)
        queue_density = self._calculate_queue_density(side)
        
        # 缓存结果
        cache_key = f"{side}_{price:.5f}_{level}"
        self.queue_position_cache[cache_key] = {
            'ahead_qty': ahead_qty,
            'take_rate': take_rate,
            'cancel_rate': cancel_rate,
            'eta_ms': eta_ms,
            'action': action,
            'level': level,
            'timestamp': time.time()
        }
        
        return {
            'side': side,
            'price': price,
            'level': level,
            'ahead_qty': ahead_qty,
            'take_rate': take_rate,
            'cancel_rate': cancel_rate,
            'eta_ms': eta_ms,
            'ttl_ms': ttl_ms,
            'optimal_ttl_ms': optimal_ttl_ms,
            'eta_ttl_ratio': eta_ttl_ratio,
            'fill_probability': fill_probability,
            'queue_density': queue_density,
            'action': action
        }
    
    def _calculate_fill_probability(self, eta_ms: float, ttl_ms: float, level: str) -> float:
        """计算在TTL内成交的概率"""
        if eta_ms <= 0 or ttl_ms <= 0:
            return 0.0
        
        # 使用指数分布模型：P(fill) = 1 - exp(-ttl/eta)
        # 但考虑市场微观结构，添加修正因子
        base_prob = 1.0 - math.exp(-ttl_ms / eta_ms)
        
        # 根据层级调整概率
        if level == 'L1':
            level_factor = 1.1  # L1更容易成交
        elif level == 'L2':
            level_factor = 0.8  # L2成交概率较低
        else:
            level_factor = 1.0
        
        return min(1.0, max(0.0, base_prob * level_factor))
    
    def log_analysis(self, analysis: Dict[str, Any]):
        """按照Phase 9.3模板输出QLE状态线"""
        logger.info(
            f"[QLE] side={analysis['side']} px={analysis['price']:.5f} "
            f"ahead_qty={analysis['ahead_qty']:.1f} take_rate={analysis['take_rate']:.3f} cancel_rate={analysis['cancel_rate']:.3f} "
            f"eta_ms={analysis['eta_ms']:.0f} ttl_ms={analysis['ttl_ms']:.0f} "
            f"action={analysis['action']}"
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取QLE统计信息"""
        return {
            'trade_samples': len(self.trade_history),
            'cancel_samples': len(self.cancel_history),
            'depth_samples': len(self.depth_snapshots),
            'buy_intensity': self.last_trade_intensity.get('BUY', 0.0),
            'sell_intensity': self.last_trade_intensity.get('SELL', 0.0),
            'buy_cancel_rate': self.last_cancel_rate.get('BUY', 0.0),
            'sell_cancel_rate': self.last_cancel_rate.get('SELL', 0.0)
        }
    
    # ========== B3: 微价格与危险率模型 ==========
    
    def update_queue_metrics(self, market_data: Dict[str, Any]) -> None:
        """B3: 更新q_bid, q_ask, arrivals_λ (50-200ms更新频率)"""
        now = time.time()
        if now - self.microprice_update_time < 0.1:  # 100ms更新频率
            return
            
        try:
            # 提取订单簿数据
            best_bid = market_data.get('best_bid', 0.0)
            best_ask = market_data.get('best_ask', 0.0) 
            bid_qty = market_data.get('bid_qty', 0.0)
            ask_qty = market_data.get('ask_qty', 0.0)
            
            if best_bid <= 0 or best_ask <= 0:
                return
                
            # 更新队列位置估计
            self.queue_metrics['BUY'] = {
                'q_position': self.estimate_queue_position('BUY', best_bid, market_data),
                'arrivals_lambda': self.calculate_trade_intensity('BUY'),
                'best_price': best_bid,
                'best_qty': bid_qty
            }
            
            self.queue_metrics['SELL'] = {
                'q_position': self.estimate_queue_position('SELL', best_ask, market_data), 
                'arrivals_lambda': self.calculate_trade_intensity('SELL'),
                'best_price': best_ask,
                'best_qty': ask_qty
            }
            
            # 计算微价格
            self.microprice_cache = self.calculate_microprice(best_bid, best_ask, bid_qty, ask_qty)
            self.microprice_update_time = now
            
            logger.debug(f"[B3-QM] microprice={self.microprice_cache:.5f} "
                        f"q_bid={self.queue_metrics['BUY']['q_position']:.1f} "
                        f"q_ask={self.queue_metrics['SELL']['q_position']:.1f} "
                        f"λ_buy={self.queue_metrics['BUY']['arrivals_lambda']:.3f} "
                        f"λ_sell={self.queue_metrics['SELL']['arrivals_lambda']:.3f}")
                        
        except Exception as e:
            logger.warning(f"[B3-QM] update_queue_metrics error: {e}")
    
    def calculate_microprice(self, bid: float, ask: float, vol_bid: float, vol_ask: float) -> float:
        """B3: microprice = (ask*vol_bid + bid*vol_ask)/(vol_bid+vol_ask)决定偏置"""
        try:
            if vol_bid <= 0 and vol_ask <= 0:
                return (bid + ask) / 2.0  # 退化为中价
                
            total_vol = vol_bid + vol_ask
            if total_vol <= 0:
                return (bid + ask) / 2.0
                
            microprice = (ask * vol_bid + bid * vol_ask) / total_vol
            return microprice
            
        except Exception as e:
            logger.warning(f"[B3-MP] microprice calculation error: {e}")
            return (bid + ask) / 2.0
    
    def calculate_fill_hazard(self, side: str) -> float:
        """B3: 危险率模型 fill_hazard ≈ λ / q 估期望fill时间"""
        try:
            metrics = self.queue_metrics.get(side, {})
            q_position = metrics.get('q_position', 100.0)  # 默认较深队列
            arrivals_lambda = metrics.get('arrivals_lambda', 0.001)  # 默认很低到达率
            
            if q_position <= 0:
                q_position = 1.0  # 避免除零
                
            # fill_hazard = λ / q (单位: 1/秒)
            fill_hazard = arrivals_lambda / q_position
            
            # 缓存结果
            self.fill_hazard_cache[side] = fill_hazard
            
            return fill_hazard
            
        except Exception as e:
            logger.warning(f"[B3-FH] fill_hazard calculation error for {side}: {e}")
            return 0.001  # 默认很低hazard rate
    
    def estimate_expected_fill_time(self, side: str) -> float:
        """B3: 估计期望成交时间 E[T] = 1 / fill_hazard"""
        hazard = self.calculate_fill_hazard(side)
        if hazard <= 0:
            return float('inf')
        return 1.0 / hazard
    
    def should_avoid_adverse_selection(self, side: str, current_price: float) -> Tuple[bool, float]:
        """B3: E[PnL_markout]为负时不挂或后撤1-2 tick"""
        try:
            if self.microprice_cache is None:
                return False, 0.0
                
            microprice = self.microprice_cache
            tick_size = 0.00001  # DOGEUSDT tick size
            
            # 简化的adverse selection检测
            if side == 'BUY':
                # 买单: 如果microprice明显低于当前价格，说明卖压大
                adverse_signal = microprice < current_price - 2 * tick_size
                suggested_retreat = current_price - 1 * tick_size if adverse_signal else current_price
            else:  # SELL
                # 卖单: 如果microprice明显高于当前价格，说明买压大  
                adverse_signal = microprice > current_price + 2 * tick_size
                suggested_retreat = current_price + 1 * tick_size if adverse_signal else current_price
                
            logger.debug(f"[B3-AS] side={side} px={current_price:.5f} microprice={microprice:.5f} "
                        f"adverse={adverse_signal} suggested={suggested_retreat:.5f}")
                        
            return adverse_signal, suggested_retreat
            
        except Exception as e:
            logger.warning(f"[B3-AS] adverse selection check error: {e}")
            return False, current_price
    
    def get_b3_metrics(self) -> Dict[str, Any]:
        """B3: 获取队列感知与微价格指标"""
        return {
            'microprice': self.microprice_cache,
            'queue_metrics': self.queue_metrics.copy(),
            'fill_hazard': self.fill_hazard_cache.copy(),
            'expected_fill_time': {
                'BUY': self.estimate_expected_fill_time('BUY'),
                'SELL': self.estimate_expected_fill_time('SELL')
            }
        }


class TTLSurvivalOptimizer:
    """
    B4: TTL = 生存曲线（Survival Curve）而非常数
    拟合历史time_to_fill为S(t)，TTL取argmax PnL(t)
    """
    
    def __init__(self, history_window: int = 100):
        self.history_window = history_window
        self.fill_history = deque(maxlen=history_window)  # 历史成交记录
        self.cancel_history = deque(maxlen=history_window)  # 历史撤单记录
        
        # B4: 分层TTL优化缓存
        self.optimal_ttl_cache = {
            'BUY': {'L0': 5.0, 'L1': 10.0, 'L2': 20.0},
            'SELL': {'L0': 5.0, 'L1': 10.0, 'L2': 20.0}
        }
        
        # B4: 生存函数拟合参数
        self.survival_params = {
            'BUY': {'lambda': 0.1, 'sigma': 0.02},  # 指数衰减率、波动率
            'SELL': {'lambda': 0.1, 'sigma': 0.02}
        }
        
        logger.info("[B4] TTLSurvivalOptimizer initialized")
    
    def record_fill_event(self, side: str, layer: str, time_to_fill: float, 
                         pnl_realized: float, price: float, qty: float):
        """B4: 记录成交事件用于拟合生存曲线"""
        event = {
            'side': side,
            'layer': layer, 
            'time_to_fill': time_to_fill,
            'pnl_realized': pnl_realized,
            'price': price,
            'qty': qty,
            'timestamp': time.time(),
            'event_type': 'FILL'
        }
        self.fill_history.append(event)
        
        # 实时更新生存函数参数
        self._update_survival_params(side)
        
        logger.debug(f"[B4-Fill] side={side} L{layer} ttf={time_to_fill:.1f}s pnl={pnl_realized:.4f}")
    
    def record_cancel_event(self, side: str, layer: str, time_to_cancel: float, 
                          opportunity_cost: float, price: float):
        """B4: 记录撤单事件"""
        event = {
            'side': side,
            'layer': layer,
            'time_to_cancel': time_to_cancel,
            'opportunity_cost': opportunity_cost,
            'price': price,
            'timestamp': time.time(),
            'event_type': 'CANCEL'
        }
        self.cancel_history.append(event)
        
        logger.debug(f"[B4-Cancel] side={side} L{layer} ttc={time_to_cancel:.1f}s cost={opportunity_cost:.4f}")
    
    def _update_survival_params(self, side: str):
        """B4: 基于历史数据更新生存函数参数"""
        try:
            # 提取该方向的成交时间
            fill_times = [event['time_to_fill'] for event in self.fill_history 
                         if event['side'] == side and event['event_type'] == 'FILL']
            
            if len(fill_times) < 5:
                return  # 样本不足
            
            # 简化的指数分布拟合：λ = 1/mean(time_to_fill)
            mean_fill_time = sum(fill_times) / len(fill_times)
            if mean_fill_time > 0:
                self.survival_params[side]['lambda'] = 1.0 / mean_fill_time
                
            # 计算波动率
            variance = sum((t - mean_fill_time)**2 for t in fill_times) / len(fill_times)
            self.survival_params[side]['sigma'] = math.sqrt(variance) / mean_fill_time
            
        except Exception as e:
            logger.warning(f"[B4] survival params update error for {side}: {e}")
    
    def survival_function(self, t: float, side: str) -> float:
        """B4: 生存函数 S(t) = P(T > t) = exp(-λt)"""
        try:
            lambda_param = self.survival_params[side]['lambda']
            return math.exp(-lambda_param * t)
        except Exception as e:
            logger.warning(f"[B4] survival function error for {side} at t={t}: {e}")
            return 0.5  # 默认50%生存率
    
    def hazard_function(self, t: float, side: str) -> float:
        """B4: 危险函数 h(t) = λ (常数hazard rate for指数分布)"""
        return self.survival_params[side]['lambda']
    
    def expected_pnl_at_ttl(self, ttl: float, side: str, layer: str, 
                           current_price: float, spread_bps: float) -> float:
        """
        B4: 计算给定TTL下的期望PnL
        E[PnL] = P(fill) * E[PnL|fill] - P(cancel) * E[cost|cancel]
        """
        try:
            # 成交概率 = 1 - S(ttl)
            fill_prob = 1.0 - self.survival_function(ttl, side)
            cancel_prob = self.survival_function(ttl, side)
            
            # 历史平均PnL
            recent_fills = [e for e in self.fill_history 
                           if e['side'] == side and e['layer'] == layer]
            
            if recent_fills:
                avg_fill_pnl = sum(e['pnl_realized'] for e in recent_fills[-20:]) / len(recent_fills[-20:])
            else:
                # 默认：spread capture - adverse selection估计
                base_pnl = spread_bps * current_price / 10000
                adverse_cost = base_pnl * 0.3  # 30%逆向选择成本
                avg_fill_pnl = base_pnl - adverse_cost
            
            # 撤单机会成本 (简化)
            avg_cancel_cost = spread_bps * current_price / 10000 * 0.1  # 10%的spread价值作为机会成本
            
            expected_pnl = fill_prob * avg_fill_pnl - cancel_prob * avg_cancel_cost
            
            return expected_pnl
            
        except Exception as e:
            logger.warning(f"[B4] expected PnL calculation error: {e}")
            return 0.0
    
    def optimize_ttl(self, side: str, layer: str, current_price: float, 
                    spread_bps: float, gate_usage_pct: float = 0.0) -> float:
        """
        B4: TTL优化 - 联合S(t)、σ(t)、gate_usage、queue_growth决定TTL
        返回最优TTL（秒）
        """
        try:
            # TTL候选值 (秒)
            ttl_candidates = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 60.0]
            
            best_ttl = 5.0  # 默认值
            best_pnl = float('-inf')
            
            for ttl in ttl_candidates:
                expected_pnl = self.expected_pnl_at_ttl(ttl, side, layer, current_price, spread_bps)
                
                # gate使用率惩罚：使用率高时偏向更长TTL减少撤单
                gate_penalty = gate_usage_pct * 0.01 * (1.0 / max(ttl, 1.0))
                adjusted_pnl = expected_pnl - gate_penalty
                
                if adjusted_pnl > best_pnl:
                    best_pnl = adjusted_pnl
                    best_ttl = ttl
            
            # 缓存最优TTL
            self.optimal_ttl_cache[side][layer] = best_ttl
            
            logger.debug(f"[B4-Opt] side={side} L{layer} optimal_ttl={best_ttl:.1f}s "
                        f"expected_pnl={best_pnl:.6f} gate_usage={gate_usage_pct:.1f}%")
            
            return best_ttl
            
        except Exception as e:
            logger.warning(f"[B4] TTL optimization error for {side} L{layer}: {e}")
            # 返回分层默认值
            defaults = {'L0': 5.0, 'L1': 10.0, 'L2': 20.0}
            return defaults.get(layer, 5.0)
    
    def get_optimal_ttl(self, side: str, layer: str, 
                       current_market_conditions: Optional[Dict] = None) -> float:
        """B4: 获取缓存的最优TTL，可选择性考虑当前市场条件"""
        try:
            base_ttl = self.optimal_ttl_cache[side][layer]
            
            if current_market_conditions:
                # 失败窗口内增量让价调整
                recent_cancels = len([e for e in self.cancel_history 
                                    if time.time() - e['timestamp'] < 300])  # 5分钟内撤单
                
                if recent_cancels > 3:  # 撤单频繁
                    base_ttl *= 1.2  # 延长20%
                    logger.debug(f"[B4] TTL extended due to frequent cancels: {base_ttl:.1f}s")
            
            return base_ttl
            
        except Exception as e:
            logger.warning(f"[B4] get optimal TTL error: {e}")
            defaults = {'L0': 5.0, 'L1': 10.0, 'L2': 20.0}
            return defaults.get(layer, 5.0)
    
    def get_b4_metrics(self) -> Dict[str, Any]:
        """B4: 获取TTL优化指标"""
        return {
            'optimal_ttl_cache': self.optimal_ttl_cache.copy(),
            'survival_params': self.survival_params.copy(),
            'fill_history_size': len(self.fill_history),
            'cancel_history_size': len(self.cancel_history),
            'ttl_optimality_gap': self._calculate_optimality_gap(),
            'cancel_per_fill_ratio': self._calculate_cancel_fill_ratio()
        }
    
    def _calculate_optimality_gap(self) -> float:
        """计算TTL最优性差距"""
        try:
            if not self.fill_history:
                return 0.0
            
            # 实际平均成交时间 vs 理论最优
            actual_times = [e['time_to_fill'] for e in self.fill_history]
            avg_actual = sum(actual_times) / len(actual_times)
            
            # 理论最优 ≈ 1/λ
            theoretical_optimal = 1.0 / self.survival_params['BUY']['lambda']
            
            return abs(avg_actual - theoretical_optimal) / theoretical_optimal
            
        except Exception as e:
            return 0.0
    
    def _calculate_cancel_fill_ratio(self) -> float:
        """计算撤单成交比"""
        try:
            recent_fills = len([e for e in self.fill_history 
                              if time.time() - e['timestamp'] < 3600])  # 1小时内
            recent_cancels = len([e for e in self.cancel_history 
                                if time.time() - e['timestamp'] < 3600])
            
            if recent_fills == 0:
                return float('inf') if recent_cancels > 0 else 0.0
            
            return recent_cancels / recent_fills
            
        except Exception as e:
            return 0.0