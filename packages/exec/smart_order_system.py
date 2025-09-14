"""
Smart Order System - 智能下单系统 
QPE + 三重上限 + 微批滴灌 + PriceGuard

对标Jane Street/Citadel级别智能下单，包含：
- Queue Position Estimation (QPE): 队列位置估计
- Triple Limits: 深度上限 + 流速上限 + 硬性上限
- Micro-lot Dripping: 微批滴灌策略
- PriceGuard: LIMIT_MAKER安全边界
"""

import time
import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from enum import Enum
import statistics

logger = logging.getLogger(__name__)


class OrderLevel(Enum):
    L0 = "L0"  # 最优级别
    L1 = "L1"  # 次优级别 
    L2 = "L2"  # 深度级别


@dataclass
class MarketSnapshot:
    """市场快照"""
    bid: Decimal
    ask: Decimal
    mid: Decimal
    spread: Decimal
    spread_bps: float
    bid_size: Decimal = Decimal('0')
    ask_size: Decimal = Decimal('0')
    last_trade_time: float = 0
    last_trade_size: Decimal = Decimal('0')


@dataclass
class QueueEstimation:
    """队列位置估计"""
    ahead_qty: Decimal  # 队列前方数量
    fill_lambda: float  # 成交率(1/秒)
    expected_fill_time: float  # 预期成交时间(秒)
    confidence: float  # 置信度(0-1)


@dataclass
class OrderSizeHint:
    """订单尺寸建议"""
    level: OrderLevel
    side: str
    price: Decimal
    qty: Decimal
    rationale: str  # 决策理由


class QueuePositionEstimator:
    """队列位置估计器 (QPE)"""
    
    def __init__(self):
        self.trade_history: List[Dict] = []
        self.queue_history: List[Dict] = []
        self.fill_rates = {'BUY': [], 'SELL': []}
        
        # 估算参数
        self.lookback_seconds = 60  # 回看时间窗口
        self.min_samples = 5  # 最小样本数
        
        logger.info("[QPE] 队列位置估计器初始化完成")
    
    def update_trade(self, price: Decimal, qty: Decimal, side: str, timestamp: float):
        """更新成交记录"""
        self.trade_history.append({
            'price': price,
            'qty': qty,
            'side': side,
            'timestamp': timestamp
        })
        
        # 保留最近历史
        cutoff = timestamp - self.lookback_seconds
        self.trade_history = [t for t in self.trade_history if t['timestamp'] > cutoff]
    
    def estimate_queue_position(self, price: Decimal, side: str, 
                              market: MarketSnapshot) -> QueueEstimation:
        """估计队列位置"""
        try:
            # 计算历史成交流速
            recent_trades = [
                t for t in self.trade_history 
                if t['side'] == side and abs(float(t['price'] - price)) < 0.001
            ]
            
            if len(recent_trades) < self.min_samples:
                # 使用默认估算
                return QueueEstimation(
                    ahead_qty=Decimal('100'),  # 保守估计
                    fill_lambda=0.1,  # 低成交率
                    expected_fill_time=10.0,  # 10秒
                    confidence=0.3
                )
            
            # 计算成交流速 (trades/second)
            time_span = max(30.0, recent_trades[-1]['timestamp'] - recent_trades[0]['timestamp'])
            fill_lambda = len(recent_trades) / time_span
            
            # 估计队列前方数量 (基于订单簿厚度)
            book_size = market.bid_size if side == 'BUY' else market.ask_size
            ahead_qty = book_size * Decimal('0.3')  # 假设我们在队列30%位置
            
            # 预期成交时间
            expected_fill_time = float(ahead_qty) / max(fill_lambda, 0.01)
            
            # 置信度 (样本数越多置信度越高)
            confidence = min(0.9, len(recent_trades) / 20.0)
            
            return QueueEstimation(
                ahead_qty=ahead_qty,
                fill_lambda=fill_lambda,
                expected_fill_time=expected_fill_time,
                confidence=confidence
            )
            
        except Exception as e:
            logger.error("[QPE] 队列估算失败: %s", str(e))
            return QueueEstimation(
                ahead_qty=Decimal('50'),
                fill_lambda=0.05,
                expected_fill_time=20.0,
                confidence=0.2
            )


class TripleLimitsEngine:
    """三重上限引擎"""
    
    def __init__(self):
        # 上限参数
        self.depth_beta = 0.08  # β1: 深度上限系数 (8%)
        self.flow_beta = 0.015  # β2: 流速上限系数 (1.5%) 
        
        # 硬性上限 (USD)
        self.hard_caps = {
            OrderLevel.L0: Decimal('15'),   # L0: $15
            OrderLevel.L1: Decimal('25'),   # L1: $25  
            OrderLevel.L2: Decimal('40')    # L2: $40
        }
        
        # 流速统计
        self.volume_1s = Decimal('0')  # 1秒成交量
        self.volume_window = []
        
        logger.info("[TripleLimits] 三重上限引擎初始化完成")
    
    def update_volume(self, qty: Decimal, timestamp: float):
        """更新成交量统计"""
        self.volume_window.append({'qty': qty, 'timestamp': timestamp})
        
        # 保留1秒窗口
        cutoff = timestamp - 1.0
        self.volume_window = [v for v in self.volume_window if v['timestamp'] > cutoff]
        
        # 计算1秒成交量
        self.volume_1s = sum(v['qty'] for v in self.volume_window)
    
    def calculate_size_limits(self, level: OrderLevel, market: MarketSnapshot,
                            current_price: Decimal) -> Dict[str, Decimal]:
        """计算订单尺寸限制"""
        
        # 1. 深度上限: q ≤ β1 * Q_bbo
        book_depth = market.bid_size if current_price <= market.mid else market.ask_size
        depth_limit = book_depth * Decimal(str(self.depth_beta))
        
        # 2. 流速上限: q ≤ β2 * V_1s  
        flow_limit = self.volume_1s * Decimal(str(self.flow_beta))
        
        # 3. 硬性上限: USD cap
        hard_limit_usd = self.hard_caps[level]
        hard_limit_qty = hard_limit_usd / current_price
        
        return {
            'depth_limit': max(Decimal('20'), depth_limit),  # 最小20 DOGE
            'flow_limit': max(Decimal('20'), flow_limit),
            'hard_limit': hard_limit_qty,
            'final_limit': min(depth_limit, flow_limit, hard_limit_qty)
        }


class MicroLotEngine:
    """微批滴灌引擎"""
    
    def __init__(self):
        self.equity = Decimal('1000')  # 权益基准
        self.trade_size_history = []
        self.fill_rate_history = {'L0': [], 'L1': [], 'L2': []}
        
        # 微批参数
        self.lot_ratios = {
            OrderLevel.L0: [Decimal('0.4'), Decimal('0.35'), Decimal('0.25')],  # 3个小单
            OrderLevel.L1: [Decimal('0.6'), Decimal('0.4')],  # 2个中单
            OrderLevel.L2: [Decimal('1.0')]  # 1个大单
        }
        
        logger.info("[MicroLot] 微批滴灌引擎初始化完成")
    
    def update_trade_stats(self, size: Decimal, fill_time: float, level: str):
        """更新成交统计"""
        self.trade_size_history.append(size)
        if len(self.trade_size_history) > 100:
            self.trade_size_history.pop(0)
        
        # 记录成交率
        fill_success = 1 if fill_time < 10.0 else 0  # 10秒内成交算成功
        self.fill_rate_history[level].append(fill_success)
        if len(self.fill_rate_history[level]) > 20:
            self.fill_rate_history[level].pop(0)
    
    def calculate_typical_trade_size(self) -> Decimal:
        """计算典型成交尺寸"""
        if not self.trade_size_history:
            return Decimal('50')  # 默认50 DOGE
        
        return Decimal(str(statistics.median([float(s) for s in self.trade_size_history])))
    
    def generate_micro_lots(self, level: OrderLevel, target_qty: Decimal,
                          current_price: Decimal) -> List[Decimal]:
        """生成微批订单尺寸"""
        typical_size = self.calculate_typical_trade_size()
        
        # 基准尺寸计算
        if level == OrderLevel.L0:
            base_size = min(
                typical_size * Decimal('0.8'),  # 略小于平均成交
                self.equity * Decimal('0.002'),  # 0.2%权益上限
                target_qty
            )
        elif level == OrderLevel.L1:
            base_size = min(
                typical_size * Decimal('1.2'),
                self.equity * Decimal('0.003'),  # 0.3%权益上限
                target_qty
            )
        else:  # L2
            base_size = min(
                typical_size * Decimal('1.8'),
                self.equity * Decimal('0.005'),  # 0.5%权益上限
                target_qty
            )
        
        # 分配到多个微批
        lots = []
        ratios = self.lot_ratios[level]
        
        for ratio in ratios:
            lot_size = base_size * ratio
            if lot_size >= Decimal('20'):  # 最小单位
                lots.append(lot_size)
        
        return lots if lots else [max(Decimal('20'), target_qty)]


class PriceGuard:
    """LIMIT_MAKER安全边界守护"""
    
    def __init__(self):
        self.tick_size = Decimal('0.00001')  # DOGEUSDT tick size
        self.safety_ticks = 2  # 安全边界tick数
        
        logger.info("[PriceGuard] 价格守护初始化完成")
    
    def validate_price(self, side: str, price: Decimal, 
                      market: MarketSnapshot) -> Tuple[bool, Optional[Decimal]]:
        """验证价格安全性"""
        try:
            if side == 'BUY':
                # 买单价格必须 <= ask - N*tick
                max_safe_price = market.ask - self.safety_ticks * self.tick_size
                if price <= max_safe_price:
                    return True, price
                else:
                    safe_price = max_safe_price
                    logger.warning(
                        "[PriceGuard] 买单价格调整: %s -> %s (ask=%s)",
                        price, safe_price, market.ask
                    )
                    return True, safe_price
            
            else:  # SELL
                # 卖单价格必须 >= bid + N*tick  
                min_safe_price = market.bid + self.safety_ticks * self.tick_size
                if price >= min_safe_price:
                    return True, price
                else:
                    safe_price = min_safe_price
                    logger.warning(
                        "[PriceGuard] 卖单价格调整: %s -> %s (bid=%s)",
                        price, safe_price, market.bid
                    )
                    return True, safe_price
                    
        except Exception as e:
            logger.error("[PriceGuard] 价格验证失败: %s", str(e))
            return False, None


class SmartOrderSystem:
    """智能下单系统集成器"""
    
    def __init__(self):
        self.qpe = QueuePositionEstimator()
        self.triple_limits = TripleLimitsEngine()
        self.micro_lot = MicroLotEngine()
        self.price_guard = PriceGuard()
        
        # 统计
        self.metrics = {
            'orders_generated': 0,
            'price_adjustments': 0,
            'size_reductions': 0,
            'queue_estimates': 0
        }
        
        logger.info("[SmartOrderSystem] 智能下单系统初始化完成")
    
    def generate_smart_orders(self, target_qty: Decimal, level: OrderLevel,
                            side: str, target_price: Decimal,
                            market: MarketSnapshot) -> List[OrderSizeHint]:
        """生成智能订单建议"""
        try:
            smart_orders = []
            
            # 1. QPE队列估算
            queue_est = self.qpe.estimate_queue_position(target_price, side, market)
            self.metrics['queue_estimates'] += 1
            
            # 2. 三重上限计算
            limits = self.triple_limits.calculate_size_limits(level, market, target_price)
            effective_qty = min(target_qty, limits['final_limit'])
            
            if effective_qty < target_qty:
                self.metrics['size_reductions'] += 1
                logger.debug(
                    "[SmartOrder] 尺寸限制: %s -> %s (depth=%s, flow=%s, hard=%s)",
                    target_qty, effective_qty,
                    limits['depth_limit'], limits['flow_limit'], limits['hard_limit']
                )
            
            # 3. 价格安全验证
            is_safe, safe_price = self.price_guard.validate_price(side, target_price, market)
            if not is_safe:
                logger.error("[SmartOrder] 价格验证失败，跳过订单")
                return []
            
            if safe_price != target_price:
                self.metrics['price_adjustments'] += 1
                target_price = safe_price
            
            # 4. 微批滴灌分解
            micro_lots = self.micro_lot.generate_micro_lots(level, effective_qty, target_price)
            
            # 5. 生成最终订单建议
            for i, lot_size in enumerate(micro_lots):
                if lot_size >= Decimal('20'):  # 最小单位检查
                    
                    # 微调价格避免重复
                    adjusted_price = target_price + Decimal(str(i)) * self.price_guard.tick_size
                    if side == 'SELL':
                        adjusted_price = target_price + Decimal(str(i)) * self.price_guard.tick_size
                    else:
                        adjusted_price = target_price - Decimal(str(i)) * self.price_guard.tick_size
                    
                    # 再次价格验证
                    is_safe, final_price = self.price_guard.validate_price(side, adjusted_price, market)
                    if is_safe and final_price:
                        
                        rationale = f"QPE:{queue_est.confidence:.1f} Limits:{limits['final_limit']} Lot:{i+1}/{len(micro_lots)}"
                        
                        smart_orders.append(OrderSizeHint(
                            level=level,
                            side=side,
                            price=final_price,
                            qty=lot_size,
                            rationale=rationale
                        ))
                        
                        self.metrics['orders_generated'] += 1
            
            logger.debug(
                "[SmartOrder] 生成订单: level=%s side=%s 目标=%s 实际=%d个微批",
                level.value, side, target_qty, len(smart_orders)
            )
            
            return smart_orders
            
        except Exception as e:
            logger.error("[SmartOrder] 智能订单生成失败: %s", str(e))
            return []
    
    def update_market_data(self, market: MarketSnapshot, trade_qty: Optional[Decimal] = None):
        """更新市场数据"""
        timestamp = time.time()
        
        # 更新流速统计
        if trade_qty:
            self.triple_limits.update_volume(trade_qty, timestamp)
            
            # 更新QPE交易历史
            # 简化版：假设成交在mid价格
            self.qpe.update_trade(market.mid, trade_qty, 'UNKNOWN', timestamp)
    
    def get_system_metrics(self) -> Dict:
        """获取系统指标"""
        return {
            'metrics': self.metrics.copy(),
            'qpe_trades': len(self.qpe.trade_history),
            'volume_1s': float(self.triple_limits.volume_1s),
            'typical_size': float(self.micro_lot.calculate_typical_trade_size())
        }