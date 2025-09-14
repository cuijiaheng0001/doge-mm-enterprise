"""
Liquidity Envelope - 流动性包络系统
对标Jane Street/Citadel机构级流动性管理标准

核心功能:
1. 定义目标在册名义额 Q_target = α · Equity
2. 侧向目标 Q_side（随库存偏斜动态调整）
3. 分层：L0/L1/L2 = 70%/25%/5%
4. 零空档守卫：L0槽位硬约束≥8
"""
import logging
import time
from decimal import Decimal
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum
import asyncio

logger = logging.getLogger(__name__)


class Side(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderLevel(Enum):
    """订单层级"""
    L0 = 0  # 最优层级
    L1 = 1  # 第二层级
    L2 = 2  # 第三层级


@dataclass
class LayerConfig:
    """层级配置"""
    level: OrderLevel
    allocation_ratio: float  # 资金分配比例
    min_slots: int          # 最小槽位数
    max_slots: int          # 最大槽位数
    price_offset_bps: float # 价格偏移(基点)


@dataclass
class SideTarget:
    """单侧目标配置"""
    side: Side
    target_notional: Decimal    # 目标名义额
    current_notional: Decimal   # 当前名义额
    layer_targets: Dict[OrderLevel, Decimal]  # 各层级目标
    active_orders: int          # 活跃订单数
    l0_slots: int              # L0槽位数


@dataclass
class LiquiditySnapshot:
    """流动性快照"""
    timestamp: int
    total_equity: Decimal
    target_allocation: Decimal  # α * Equity
    buy_side: SideTarget
    sell_side: SideTarget
    inventory_skew: float      # 库存偏斜 (-1 to 1)
    spread_bps: float          # 当前价差(基点)


class LiquidityEnvelope:
    """
    流动性包络 - 机构级流动性管理系统
    
    核心原理:
    1. 织网策略：确保订单簿有充足的流动性覆盖
    2. 动态调整：根据库存偏斜调整买卖侧配置  
    3. 硬约束：L0层级必须满足最小槽位要求
    4. 零空档：实时监控并自动补单
    """
    
    def __init__(self, 
                 alpha: float = 0.10,           # 权益配置比例 (10%)
                 min_l0_slots: int = 8,         # L0最小槽位数
                 max_l0_slots: int = 15,        # L0最大槽位数
                 spread_sensitivity: float = 2.0):  # 价差敏感度
        """
        初始化流动性包络
        
        Args:
            alpha: 权益配置比例，决定总在册名义额
            min_l0_slots: L0层级最小槽位数
            max_l0_slots: L0层级最大槽位数
            spread_sensitivity: 价差敏感度系数
        """
        self.alpha = alpha
        self.min_l0_slots = min_l0_slots
        self.max_l0_slots = max_l0_slots
        self.spread_sensitivity = spread_sensitivity
        
        # 层级配置
        self.layer_configs = {
            OrderLevel.L0: LayerConfig(OrderLevel.L0, 0.70, min_l0_slots, max_l0_slots, 0.0),
            OrderLevel.L1: LayerConfig(OrderLevel.L1, 0.25, 3, 8, 2.0),
            OrderLevel.L2: LayerConfig(OrderLevel.L2, 0.05, 1, 3, 5.0)
        }
        
        # 性能指标
        self.metrics = {
            'envelopes_calculated': 0,
            'gap_violations_detected': 0,
            'gap_violations_fixed': 0,
            'l0_slot_violations': 0,
            'rebalance_triggers': 0,
            'total_orders_managed': 0
        }
        
        # 状态
        self.last_snapshot: Optional[LiquiditySnapshot] = None
        self.gap_guard_active = False
        
        logger.info(
            "[LiquidityEnvelope] 初始化完成: alpha=%.1f%% min_l0_slots=%d",
            alpha * 100, min_l0_slots
        )
    
    def calculate_liquidity_targets(self, 
                                  total_equity: Decimal, 
                                  doge_balance: Decimal,
                                  usdt_balance: Decimal,
                                  current_price: Decimal,
                                  spread_bps: float) -> LiquiditySnapshot:
        """
        计算流动性目标
        
        Args:
            total_equity: 总权益
            doge_balance: DOGE余额
            usdt_balance: USDT余额  
            current_price: 当前价格
            spread_bps: 价差(基点)
            
        Returns:
            LiquiditySnapshot: 流动性快照
        """
        # 计算库存偏斜
        total_value = doge_balance * current_price + usdt_balance
        if total_value > 0:
            doge_ratio = (doge_balance * current_price) / total_value
            inventory_skew = (doge_ratio - Decimal('0.5')) * Decimal('2')  # -1 to 1
        else:
            inventory_skew = Decimal('0.0')
        
        # 计算目标配置
        target_allocation = total_equity * Decimal(str(self.alpha))
        
        # 根据库存偏斜调整侧向配置
        # inventory_skew > 0: DOGE过多，减少SELL侧配置，增加BUY侧
        # inventory_skew < 0: DOGE过少，减少BUY侧配置，增加SELL侧
        base_ratio = Decimal('0.5')
        skew_adjustment = inventory_skew * Decimal('0.15')  # 最大调整15%
        
        buy_ratio = base_ratio - skew_adjustment  # DOGE多时增加buy
        sell_ratio = base_ratio + skew_adjustment # DOGE多时减少sell
        
        # 确保比例在合理范围内
        buy_ratio = max(Decimal('0.35'), min(Decimal('0.65'), buy_ratio))
        sell_ratio = max(Decimal('0.35'), min(Decimal('0.65'), sell_ratio))
        
        # 归一化
        total_ratio = buy_ratio + sell_ratio
        buy_ratio /= total_ratio
        sell_ratio /= total_ratio
        
        # 计算各侧目标
        buy_target = target_allocation * Decimal(str(buy_ratio))
        sell_target = target_allocation * Decimal(str(sell_ratio))
        
        # 计算层级目标
        buy_layers = self._calculate_layer_targets(buy_target, spread_bps)
        sell_layers = self._calculate_layer_targets(sell_target, spread_bps)
        
        # 创建侧向目标
        buy_side = SideTarget(
            side=Side.BUY,
            target_notional=buy_target,
            current_notional=Decimal(0),  # 需要外部提供
            layer_targets=buy_layers,
            active_orders=0,  # 需要外部提供
            l0_slots=0  # 需要外部提供
        )
        
        sell_side = SideTarget(
            side=Side.SELL,
            target_notional=sell_target,
            current_notional=Decimal(0),  # 需要外部提供
            layer_targets=sell_layers,
            active_orders=0,  # 需要外部提供
            l0_slots=0  # 需要外部提供
        )
        
        # 创建快照
        snapshot = LiquiditySnapshot(
            timestamp=time.time_ns(),
            total_equity=total_equity,
            target_allocation=target_allocation,
            buy_side=buy_side,
            sell_side=sell_side,
            inventory_skew=inventory_skew,
            spread_bps=spread_bps
        )
        
        self.last_snapshot = snapshot
        self.metrics['envelopes_calculated'] += 1
        
        logger.debug(
            "[LiquidityEnvelope] 目标计算: equity=%s target=%s buy_ratio=%.1f%% sell_ratio=%.1f%% skew=%.2f",
            total_equity, target_allocation, buy_ratio*100, sell_ratio*100, inventory_skew
        )
        
        return snapshot
    
    def _calculate_layer_targets(self, side_target: Decimal, spread_bps: float) -> Dict[OrderLevel, Decimal]:
        """计算各层级目标"""
        layer_targets = {}
        
        # 根据价差调整L0配置
        spread_factor = Decimal('1.0') + (Decimal(str(spread_bps)) - Decimal('10.0')) / Decimal('100.0')  # 基准10bps
        l0_ratio = max(Decimal('0.60'), min(Decimal('0.80'), Decimal(str(self.layer_configs[OrderLevel.L0].allocation_ratio)) * spread_factor))
        
        # 重新计算比例确保和为1
        l1_ratio = Decimal(str(self.layer_configs[OrderLevel.L1].allocation_ratio))
        l2_ratio = Decimal(str(self.layer_configs[OrderLevel.L2].allocation_ratio))
        
        total_ratio = l0_ratio + l1_ratio + l2_ratio
        l0_ratio /= total_ratio
        l1_ratio /= total_ratio
        l2_ratio /= total_ratio
        
        layer_targets[OrderLevel.L0] = side_target * l0_ratio
        layer_targets[OrderLevel.L1] = side_target * l1_ratio
        layer_targets[OrderLevel.L2] = side_target * l2_ratio
        
        return layer_targets
    
    def update_current_state(self, buy_orders: List[Dict], sell_orders: List[Dict]):
        """
        更新当前状态
        
        Args:
            buy_orders: 买单列表 [{'level': 0, 'notional': Decimal('100'), 'price': Decimal('0.26')}]
            sell_orders: 卖单列表
        """
        if not self.last_snapshot:
            logger.warning("[LiquidityEnvelope] 无快照，跳过状态更新")
            return
        
        # 统计买单
        buy_notional = Decimal(0)
        buy_l0_count = 0
        buy_total = len(buy_orders)
        
        for order in buy_orders:
            buy_notional += order.get('notional', Decimal(0))
            if order.get('level', 99) == 0:  # L0层级
                buy_l0_count += 1
        
        # 统计卖单
        sell_notional = Decimal(0)
        sell_l0_count = 0
        sell_total = len(sell_orders)
        
        for order in sell_orders:
            sell_notional += order.get('notional', Decimal(0))
            if order.get('level', 99) == 0:  # L0层级
                sell_l0_count += 1
        
        # 更新快照
        self.last_snapshot.buy_side.current_notional = buy_notional
        self.last_snapshot.buy_side.active_orders = buy_total
        self.last_snapshot.buy_side.l0_slots = buy_l0_count
        
        self.last_snapshot.sell_side.current_notional = sell_notional
        self.last_snapshot.sell_side.active_orders = sell_total
        self.last_snapshot.sell_side.l0_slots = sell_l0_count
        
        self.metrics['total_orders_managed'] = buy_total + sell_total
        
        logger.debug(
            "[LiquidityEnvelope] 状态更新: BUY(orders=%d l0=%d notional=%s) SELL(orders=%d l0=%d notional=%s)",
            buy_total, buy_l0_count, buy_notional, sell_total, sell_l0_count, sell_notional
        )
    
    def detect_violations(self) -> List[Dict]:
        """
        检测违规情况
        
        Returns:
            List[Dict]: 违规列表
        """
        violations = []
        
        if not self.last_snapshot:
            return violations
        
        snapshot = self.last_snapshot
        
        # 检测L0槽位违规
        if snapshot.buy_side.l0_slots < self.min_l0_slots:
            violations.append({
                'type': 'L0_SLOT_VIOLATION',
                'side': 'BUY',
                'current': snapshot.buy_side.l0_slots,
                'required': self.min_l0_slots,
                'deficit': self.min_l0_slots - snapshot.buy_side.l0_slots
            })
            self.metrics['l0_slot_violations'] += 1
        
        if snapshot.sell_side.l0_slots < self.min_l0_slots:
            violations.append({
                'type': 'L0_SLOT_VIOLATION',
                'side': 'SELL',
                'current': snapshot.sell_side.l0_slots,
                'required': self.min_l0_slots,
                'deficit': self.min_l0_slots - snapshot.sell_side.l0_slots
            })
            self.metrics['l0_slot_violations'] += 1
        
        # 检测零空档
        if snapshot.buy_side.l0_slots == 0:
            violations.append({
                'type': 'ZERO_GAP',
                'side': 'BUY',
                'severity': 'CRITICAL'
            })
            self.metrics['gap_violations_detected'] += 1
        
        if snapshot.sell_side.l0_slots == 0:
            violations.append({
                'type': 'ZERO_GAP',
                'side': 'SELL',
                'severity': 'CRITICAL'
            })
            self.metrics['gap_violations_detected'] += 1
        
        # 检测名义额偏差
        buy_deviation = abs(snapshot.buy_side.current_notional - snapshot.buy_side.target_notional)
        buy_deviation_ratio = buy_deviation / snapshot.buy_side.target_notional if snapshot.buy_side.target_notional > 0 else 0
        
        if buy_deviation_ratio > Decimal('0.3'):  # 偏差超过30%
            violations.append({
                'type': 'NOTIONAL_DEVIATION',
                'side': 'BUY',
                'current': float(snapshot.buy_side.current_notional),
                'target': float(snapshot.buy_side.target_notional),
                'deviation_ratio': float(buy_deviation_ratio)
            })
        
        sell_deviation = abs(snapshot.sell_side.current_notional - snapshot.sell_side.target_notional)
        sell_deviation_ratio = sell_deviation / snapshot.sell_side.target_notional if snapshot.sell_side.target_notional > 0 else 0
        
        if sell_deviation_ratio > Decimal('0.3'):  # 偏差超过30%
            violations.append({
                'type': 'NOTIONAL_DEVIATION',
                'side': 'SELL',
                'current': float(snapshot.sell_side.current_notional),
                'target': float(snapshot.sell_side.target_notional),
                'deviation_ratio': float(sell_deviation_ratio)
            })
        
        if violations:
            logger.warning(
                "[LiquidityEnvelope] 检测到 %d 个违规: %s",
                len(violations), [v['type'] for v in violations]
            )
        
        return violations
    
    def generate_rebalance_orders(self, current_price: Decimal, spread_bps: float) -> List[Dict]:
        """
        生成再平衡订单
        
        Args:
            current_price: 当前价格
            spread_bps: 价差(基点)
            
        Returns:
            List[Dict]: 订单建议列表
        """
        orders = []
        violations = self.detect_violations()
        
        if not violations:
            return orders
        
        snapshot = self.last_snapshot
        spread = current_price * Decimal(str(spread_bps / 10000))
        
        for violation in violations:
            if violation['type'] == 'L0_SLOT_VIOLATION':
                side = violation['side']
                deficit = violation['deficit']
                
                # 计算每个订单的目标金额
                side_target = snapshot.buy_side if side == 'BUY' else snapshot.sell_side
                l0_target = side_target.layer_targets[OrderLevel.L0]
                
                # 将L0目标分配到更多槽位
                target_slots = self.min_l0_slots
                order_size = l0_target / Decimal(str(target_slots))
                
                # 生成补单
                for i in range(deficit):
                    if side == 'BUY':
                        price = current_price - spread * Decimal(str(0.1 + i * 0.05))  # 递减价格
                        qty = order_size / price
                    else:  # SELL
                        price = current_price + spread * Decimal(str(0.1 + i * 0.05))  # 递增价格
                        qty = order_size / price  # SELL时qty就是DOGE数量
                    
                    orders.append({
                        'side': side,
                        'level': 0,  # L0
                        'price': price,
                        'qty': qty,
                        'notional': order_size,
                        'reason': f'L0_SLOT_REBALANCE_{i+1}',
                        'priority': 'HIGH'
                    })
            
            elif violation['type'] == 'ZERO_GAP':
                # 零空档紧急补单
                side = violation['side']
                
                # 紧急生成2个L0订单
                side_target = snapshot.buy_side if side == 'BUY' else snapshot.sell_side
                l0_target = side_target.layer_targets[OrderLevel.L0]
                emergency_size = l0_target / Decimal('4')  # 分成4份，先放2份
                
                for i in range(2):
                    if side == 'BUY':
                        price = current_price - spread * Decimal(str(0.05 + i * 0.03))
                        qty = emergency_size / price
                    else:  # SELL
                        price = current_price + spread * Decimal(str(0.05 + i * 0.03))
                        qty = emergency_size / price
                    
                    orders.append({
                        'side': side,
                        'level': 0,  # L0
                        'price': price,
                        'qty': qty,
                        'notional': emergency_size,
                        'reason': f'ZERO_GAP_EMERGENCY_{i+1}',
                        'priority': 'CRITICAL'
                    })
        
        if orders:
            logger.info(
                "[LiquidityEnvelope] 生成再平衡订单: %d个订单 violations=%d",
                len(orders), len(violations)
            )
            self.metrics['rebalance_triggers'] += 1
        
        return orders
    
    def get_envelope_health(self) -> Dict[str, any]:
        """获取包络健康状态"""
        if not self.last_snapshot:
            return {
                'status': 'NOT_INITIALIZED',
                'health_score': 0.0,
                'metrics': self.metrics.copy()
            }
        
        snapshot = self.last_snapshot
        
        # 计算健康得分
        health_factors = []
        
        # L0槽位健康度
        buy_l0_health = min(1.0, snapshot.buy_side.l0_slots / self.min_l0_slots)
        sell_l0_health = min(1.0, snapshot.sell_side.l0_slots / self.min_l0_slots)
        health_factors.append((buy_l0_health + sell_l0_health) / 2.0)
        
        # 名义额匹配度
        if snapshot.buy_side.target_notional > 0:
            buy_match = min(1.0, snapshot.buy_side.current_notional / snapshot.buy_side.target_notional)
            health_factors.append(buy_match)
        
        if snapshot.sell_side.target_notional > 0:
            sell_match = min(1.0, snapshot.sell_side.current_notional / snapshot.sell_side.target_notional)
            health_factors.append(sell_match)
        
        # 综合健康得分
        health_score = sum(health_factors) / len(health_factors) if health_factors else 0.0
        
        # 状态判定
        if health_score >= 0.9:
            status = 'HEALTHY'
        elif health_score >= 0.7:
            status = 'WARNING'
        else:
            status = 'CRITICAL'
        
        return {
            'status': status,
            'health_score': health_score,
            'buy_l0_slots': snapshot.buy_side.l0_slots,
            'sell_l0_slots': snapshot.sell_side.l0_slots,
            'buy_l0_target': self.min_l0_slots,
            'sell_l0_target': self.min_l0_slots,
            'buy_notional_ratio': float(snapshot.buy_side.current_notional / snapshot.buy_side.target_notional) if snapshot.buy_side.target_notional > 0 else 0,
            'sell_notional_ratio': float(snapshot.sell_side.current_notional / snapshot.sell_side.target_notional) if snapshot.sell_side.target_notional > 0 else 0,
            'inventory_skew': snapshot.inventory_skew,
            'violations': len(self.detect_violations()),
            'metrics': self.metrics.copy()
        }
    
    def get_health_metrics(self) -> Dict[str, any]:
        """获取健康度指标 (get_envelope_health的别名)"""
        health = self.get_envelope_health()
        
        # 转换为预期格式
        return {
            'active_l0_slots': health.get('buy_l0_slots', 0) + health.get('sell_l0_slots', 0),
            'min_l0_slots': self.min_l0_slots,
            'target_achievement_rate': health.get('health_score', 0.0) * 100.0,
            'status': health.get('status', 'UNKNOWN'),
            'violations': health.get('violations', 0),
            'buy_l0_slots': health.get('buy_l0_slots', 0),
            'sell_l0_slots': health.get('sell_l0_slots', 0)
        }