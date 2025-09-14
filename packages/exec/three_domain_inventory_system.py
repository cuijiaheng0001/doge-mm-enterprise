"""
Three Domain Inventory System - 库存管理三时域系统
毫秒补位 + 秒级倾斜 + 纯Maker积极TWAP

对标Jane Street/Citadel级别库存管理，包含：
- Millisecond Domain: FILL触发瞬时补位（对侧优先/同侧次之，按偏斜倾斜尺寸与贴近）
- Second Domain: ISQ（Inventory-Skew Quoter）对spread和size做倾斜  
- Minute Domain: 纯Maker积极TWAP（紧急时更贴近市价，但始终保持maker角色）
"""

import time
import asyncio
import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Callable, Tuple
from enum import Enum
import statistics

logger = logging.getLogger(__name__)


class InventoryDomain(Enum):
    MILLISECOND = "millisecond"   # 毫秒级补位
    SECOND = "second"            # 秒级倾斜
    MINUTE = "minute"            # 分钟级TWAP


class EmergencyLevel(Enum):
    NORMAL = "normal"           # 正常状态 
    WARNING = "warning"         # 警告状态（±10-15%）
    EMERGENCY = "emergency"     # 紧急状态（±15-25%）


@dataclass
class InventorySnapshot:
    """库存快照"""
    timestamp: float
    doge_balance: Decimal
    usdt_balance: Decimal
    total_equity_usdt: Decimal
    doge_ratio: float              # DOGE占比 (0-1)
    inventory_skew: float          # 库存偏斜 (-1 to 1, 0为平衡)
    emergency_level: EmergencyLevel
    target_ratio: float = 0.5     # 目标比例
    deviation_pct: float = 0.0    # 偏离百分比


@dataclass
class FillEvent:
    """成交事件"""
    order_id: str
    side: str                     # BUY/SELL
    filled_qty: Decimal
    filled_price: Decimal
    timestamp: float
    remaining_qty: Decimal = Decimal('0')


@dataclass
class RebalanceAction:
    """再平衡动作"""
    domain: InventoryDomain
    side: str
    qty: Decimal
    price: Decimal
    action_type: str             # "instant_fill", "skew_adjust", "twap_order"
    urgency: EmergencyLevel
    rationale: str


class MillisecondDomain:
    """毫秒级：FILL触发瞬时补位"""
    
    def __init__(self):
        self.fill_response_history: List[float] = []  # 响应时间历史
        self.instant_repost_enabled = True
        
        # 补位配置
        self.repost_ratio = {
            'opposite_side': 0.7,    # 对侧优先70%
            'same_side': 0.3         # 同侧次之30%
        }
        
        logger.info("[MillisecondDomain] 毫秒级补位系统初始化完成")
    
    def calculate_instant_repost(self, fill_event: FillEvent, 
                               inventory_snapshot: InventorySnapshot) -> List[RebalanceAction]:
        """计算瞬时补位订单"""
        start_time = time.time()
        actions = []
        
        try:
            # 根据库存偏斜调整补位策略
            skew = inventory_snapshot.inventory_skew
            filled_side = fill_event.side
            filled_qty = fill_event.filled_qty
            filled_price = fill_event.filled_price
            
            # 对侧补位（优先）
            opposite_side = 'SELL' if filled_side == 'BUY' else 'BUY'
            opposite_qty = filled_qty * Decimal(str(self.repost_ratio['opposite_side']))
            
            # 根据库存偏斜调整对侧价格和数量
            if skew > 0.1:  # DOGE过多
                if opposite_side == 'SELL':
                    # 卖单更积极：价格更近，数量更大
                    price_adjustment = Decimal('0.9998')  # 稍微降价
                    qty_multiplier = Decimal('1.2')       # 数量增大20%
                else:
                    # 买单保守：价格更远，数量更小
                    price_adjustment = Decimal('1.0002')
                    qty_multiplier = Decimal('0.8')
            elif skew < -0.1:  # USDT过多
                if opposite_side == 'BUY':
                    # 买单更积极：价格更近，数量更大
                    price_adjustment = Decimal('1.0002')
                    qty_multiplier = Decimal('1.2')
                else:
                    # 卖单保守：价格更远，数量更小
                    price_adjustment = Decimal('0.9998')
                    qty_multiplier = Decimal('0.8')
            else:
                # 平衡状态
                price_adjustment = Decimal('1.0')
                qty_multiplier = Decimal('1.0')
            
            opposite_price = filled_price * price_adjustment
            opposite_qty = opposite_qty * qty_multiplier
            
            if opposite_qty >= Decimal('20'):  # 最小单位检查
                actions.append(RebalanceAction(
                    domain=InventoryDomain.MILLISECOND,
                    side=opposite_side,
                    qty=opposite_qty,
                    price=opposite_price,
                    action_type="instant_fill",
                    urgency=inventory_snapshot.emergency_level,
                    rationale=f"对侧补位 skew={skew:.2f} 价格调整={price_adjustment} 数量调整={qty_multiplier}"
                ))
            
            # 同侧补位（次之）- 仅在库存严重偏斜时
            if abs(skew) > 0.15:  # 严重偏斜才同侧补位
                same_qty = filled_qty * Decimal(str(self.repost_ratio['same_side']))
                # 同侧补位价格需要更保守
                if filled_side == 'BUY':
                    same_price = filled_price * Decimal('0.9995')  # 买单价格稍低
                else:
                    same_price = filled_price * Decimal('1.0005')  # 卖单价格稍高
                
                if same_qty >= Decimal('20'):
                    actions.append(RebalanceAction(
                        domain=InventoryDomain.MILLISECOND,
                        side=filled_side,
                        qty=same_qty,
                        price=same_price,
                        action_type="instant_fill",
                        urgency=inventory_snapshot.emergency_level,
                        rationale=f"同侧补位 严重偏斜={skew:.2f}"
                    ))
            
            # 记录响应时间
            response_time = (time.time() - start_time) * 1000  # ms
            self.fill_response_history.append(response_time)
            if len(self.fill_response_history) > 100:
                self.fill_response_history.pop(0)
            
            logger.debug(
                "[MillisecondDomain] ⚡ 瞬时补位: %s %s@%s -> %d个补位订单 响应=%.1fms",
                fill_event.side, fill_event.filled_qty, fill_event.filled_price,
                len(actions), response_time
            )
            
            return actions
            
        except Exception as e:
            logger.error("[MillisecondDomain] 瞬时补位失败: %s", str(e))
            return []
    
    def get_response_metrics(self) -> Dict[str, float]:
        """获取毫秒级响应指标"""
        if not self.fill_response_history:
            return {'p50': 0.0, 'p95': 0.0, 'p99': 0.0}
        
        sorted_times = sorted(self.fill_response_history)
        return {
            'p50': sorted_times[len(sorted_times) // 2],
            'p95': sorted_times[int(len(sorted_times) * 0.95)],
            'p99': sorted_times[int(len(sorted_times) * 0.99)]
        }


class SecondDomain:
    """秒级：ISQ（Inventory-Skew Quoter）倾斜策略"""
    
    def __init__(self):
        self.update_interval = 1.0  # 1秒更新间隔
        self.last_update = 0.0
        
        # 倾斜配置
        self.skew_sensitivity = 0.2   # 倾斜敏感度
        self.max_spread_adjustment = 0.15  # 最大价差调整15%
        self.max_size_adjustment = 0.3     # 最大尺寸调整30%
        
        logger.info("[SecondDomain] 秒级ISQ倾斜系统初始化完成")
    
    def calculate_skew_adjustments(self, inventory_snapshot: InventorySnapshot,
                                 base_spread: Decimal, base_size: Decimal) -> Dict[str, Any]:
        """计算库存倾斜调整"""
        current_time = time.time()
        
        if current_time - self.last_update < self.update_interval:
            return {'should_update': False}
        
        self.last_update = current_time
        skew = inventory_snapshot.inventory_skew
        
        # 价差倾斜调整
        spread_adjustment = min(abs(skew) * self.skew_sensitivity, self.max_spread_adjustment)
        
        if skew > 0.05:  # DOGE过多，卖方更积极
            buy_spread_multiplier = 1 + spread_adjustment      # 买单价差扩大
            sell_spread_multiplier = 1 - spread_adjustment     # 卖单价差缩小
            buy_size_multiplier = 1 - abs(skew) * self.skew_sensitivity  # 买单尺寸减小
            sell_size_multiplier = 1 + abs(skew) * self.skew_sensitivity  # 卖单尺寸增大
        elif skew < -0.05:  # USDT过多，买方更积极
            buy_spread_multiplier = 1 - spread_adjustment      # 买单价差缩小
            sell_spread_multiplier = 1 + spread_adjustment     # 卖单价差扩大
            buy_size_multiplier = 1 + abs(skew) * self.skew_sensitivity  # 买单尺寸增大
            sell_size_multiplier = 1 - abs(skew) * self.skew_sensitivity  # 卖单尺寸减小
        else:
            # 平衡状态，无调整
            buy_spread_multiplier = sell_spread_multiplier = 1.0
            buy_size_multiplier = sell_size_multiplier = 1.0
        
        # 限制调整幅度
        buy_size_multiplier = max(0.7, min(1.3, buy_size_multiplier))
        sell_size_multiplier = max(0.7, min(1.3, sell_size_multiplier))
        
        adjustments = {
            'should_update': True,
            'skew': skew,
            'spread_adjustments': {
                'buy_multiplier': buy_spread_multiplier,
                'sell_multiplier': sell_spread_multiplier
            },
            'size_adjustments': {
                'buy_multiplier': buy_size_multiplier,
                'sell_multiplier': sell_size_multiplier
            },
            'rationale': f"ISQ倾斜: skew={skew:.3f} spread_adj={spread_adjustment:.3f}"
        }
        
        logger.debug(
            "[SecondDomain] 📐 ISQ倾斜调整: skew=%.3f buy_spread=%.3f sell_spread=%.3f buy_size=%.3f sell_size=%.3f",
            skew, buy_spread_multiplier, sell_spread_multiplier, 
            buy_size_multiplier, sell_size_multiplier
        )
        
        return adjustments


class MinuteDomain:
    """分钟级：纯Maker积极TWAP（紧急时更贴近市价，但始终保持maker角色）"""
    
    def __init__(self):
        self.update_interval = 60.0  # 1分钟更新间隔
        self.last_update = 0.0
        
        # TWAP配置（纯maker模式）
        self.target_bands = {
            'soft': 0.10,      # 软带宽±10%
            'hard': 0.15,      # 硬带宽±15%
            'emergency': 0.25  # 紧急带宽±25%
        }
        
        self.maker_only = True       # 纯maker模式，不使用taker
        self.twap_duration = 300     # TWAP持续时间5分钟
        self.aggressive_maker_factor = 1.5  # 紧急时提高maker积极性
        
        # 历史记录
        self.twap_history: List[Dict] = []
        self.pov_usage: List[float] = []
        
        logger.info("[MinuteDomain] 分钟级TWAP/POV系统初始化完成")
    
    def calculate_twap_orders(self, inventory_snapshot: InventorySnapshot) -> List[RebalanceAction]:
        """计算TWAP再平衡订单"""
        current_time = time.time()
        
        if current_time - self.last_update < self.update_interval:
            return []
        
        self.last_update = current_time
        actions = []
        
        try:
            deviation_pct = abs(inventory_snapshot.deviation_pct)
            skew = inventory_snapshot.inventory_skew
            emergency_level = inventory_snapshot.emergency_level
            
            # 确定需要再平衡的方向和数量
            if abs(skew) < 0.05:  # 平衡状态，无需TWAP
                return []
            
            # 计算目标再平衡量
            total_equity = inventory_snapshot.total_equity_usdt
            target_rebalance_usd = total_equity * Decimal(str(abs(skew))) * Decimal('0.5')  # 减少50%偏斜
            
            if skew > 0:  # DOGE过多，需要卖DOGE
                rebalance_side = 'SELL'
                # 计算需要卖出的DOGE数量
                doge_price_estimate = total_equity / (inventory_snapshot.doge_balance + 
                                                    inventory_snapshot.usdt_balance / Decimal('0.26'))  # 估算DOGE价格
                rebalance_qty = target_rebalance_usd / doge_price_estimate
            else:  # USDT过多，需要买DOGE
                rebalance_side = 'BUY'
                doge_price_estimate = total_equity / (inventory_snapshot.doge_balance + 
                                                    inventory_snapshot.usdt_balance / Decimal('0.26'))
                rebalance_qty = target_rebalance_usd / doge_price_estimate
            
            # 根据紧急程度确定TWAP参数（纯maker模式）
            if emergency_level == EmergencyLevel.EMERGENCY:
                # 紧急状态：更积极的maker价格，加速再平衡
                twap_slices = 3  # 减少切片，加速执行
                price_aggression = self.aggressive_maker_factor  # 更积极的价格
            elif emergency_level == EmergencyLevel.WARNING:
                twap_slices = 5
                price_aggression = 1.2  # 适度积极
            else:
                twap_slices = 10  # 正常状态慢慢再平衡
                price_aggression = 1.0   # 正常价格
            
            # 生成TWAP切片订单
            slice_qty = rebalance_qty / twap_slices
            
            for i in range(twap_slices):
                if slice_qty >= Decimal('20'):  # 最小单位检查
                    # 根据积极性调整价格偏移（更积极=更贴近市价）
                    base_offset = Decimal('0.9995')  # 基础价格偏移
                    aggression_offset = (price_aggression - 1.0) * 0.0003  # 积极性偏移
                    price_offset = base_offset + Decimal(str(aggression_offset))
                    
                    # 每个切片稍微随机化价格，避免被识别
                    price_random = price_offset + Decimal(str((i % 3) * 0.0001))
                    
                    action = RebalanceAction(
                        domain=InventoryDomain.MINUTE,
                        side=rebalance_side,
                        qty=slice_qty,
                        price=price_random,  # 积极的maker价格
                        action_type="aggressive_maker_twap",
                        urgency=emergency_level,
                        rationale=f"纯Maker TWAP {i+1}/{twap_slices} 偏斜={skew:.3f} 积极度={price_aggression:.1f}"
                    )
                    actions.append(action)
            
            # 记录TWAP历史
            self.twap_history.append({
                'timestamp': current_time,
                'skew': float(skew),
                'actions_count': len(actions),
                'total_qty': float(rebalance_qty),
                'emergency_level': emergency_level.value
            })
            if len(self.twap_history) > 100:
                self.twap_history.pop(0)
            
            if actions:
                logger.info(
                    "[MinuteDomain] 📈 纯Maker TWAP再平衡: %s skew=%.3f 切片=%d 紧急=%s 积极度=%.1f",
                    rebalance_side, skew, len(actions), emergency_level.value, price_aggression
                )
            
            return actions
            
        except Exception as e:
            logger.error("[MinuteDomain] TWAP计算失败: %s", str(e))
            return []
    
    def get_twap_metrics(self) -> Dict[str, Any]:
        """获取TWAP指标"""
        if not self.twap_history:
            return {'total_twap_sessions': 0, 'avg_skew': 0.0}
        
        recent_sessions = [h for h in self.twap_history if time.time() - h['timestamp'] < 3600]
        avg_skew = statistics.mean([abs(h['skew']) for h in recent_sessions]) if recent_sessions else 0.0
        
        return {
            'total_twap_sessions': len(self.twap_history),
            'recent_sessions_1h': len(recent_sessions),
            'avg_skew': avg_skew,
            'emergency_sessions': len([h for h in recent_sessions if h['emergency_level'] == 'emergency'])
        }


class ThreeDomainInventorySystem:
    """库存管理三时域系统集成器"""
    
    def __init__(self):
        self.millisecond_domain = MillisecondDomain()
        self.second_domain = SecondDomain()
        self.minute_domain = MinuteDomain()
        
        # 系统状态
        self.enabled = True
        self.last_inventory_update = 0.0
        
        # 指标
        self.metrics = {
            'millisecond_responses': 0,
            'second_adjustments': 0,
            'minute_twaps': 0,
            'emergency_interventions': 0
        }
        
        logger.info("[ThreeDomainInventory] 库存管理三时域系统初始化完成")
    
    def calculate_inventory_snapshot(self, doge_balance: Decimal, 
                                   usdt_balance: Decimal) -> InventorySnapshot:
        """计算库存快照"""
        # 估算DOGE价格 (简化版，实际应从市价获取)
        estimated_doge_price = Decimal('0.26')  # 假设DOGE价格
        
        doge_value_usdt = doge_balance * estimated_doge_price
        total_equity = doge_value_usdt + usdt_balance
        
        if total_equity > 0:
            doge_ratio = float(doge_value_usdt / total_equity)
        else:
            doge_ratio = 0.5
        
        # 计算库存偏斜 (-1 to 1)
        inventory_skew = (doge_ratio - 0.5) * 2
        deviation_pct = abs(doge_ratio - 0.5) * 2
        
        # 确定紧急程度
        if deviation_pct >= 0.25:
            emergency_level = EmergencyLevel.EMERGENCY
        elif deviation_pct >= 0.15:
            emergency_level = EmergencyLevel.WARNING
        else:
            emergency_level = EmergencyLevel.NORMAL
        
        return InventorySnapshot(
            timestamp=time.time(),
            doge_balance=doge_balance,
            usdt_balance=usdt_balance,
            total_equity_usdt=total_equity,
            doge_ratio=doge_ratio,
            inventory_skew=inventory_skew,
            emergency_level=emergency_level,
            deviation_pct=deviation_pct
        )
    
    def handle_fill_event(self, fill_event: FillEvent, 
                         doge_balance: Decimal, usdt_balance: Decimal) -> List[RebalanceAction]:
        """处理成交事件（毫秒级响应）"""
        if not self.enabled:
            return []
        
        inventory_snapshot = self.calculate_inventory_snapshot(doge_balance, usdt_balance)
        
        # 毫秒级：瞬时补位
        actions = self.millisecond_domain.calculate_instant_repost(fill_event, inventory_snapshot)
        
        if actions:
            self.metrics['millisecond_responses'] += 1
            logger.info(
                "[ThreeDomainInventory] ⚡ 毫秒级响应: %s成交触发%d个补位订单",
                fill_event.side, len(actions)
            )
        
        return actions
    
    def get_skew_adjustments(self, doge_balance: Decimal, usdt_balance: Decimal,
                           base_spread: Decimal, base_size: Decimal) -> Dict[str, Any]:
        """获取库存倾斜调整（秒级）"""
        if not self.enabled:
            return {'should_update': False}
        
        inventory_snapshot = self.calculate_inventory_snapshot(doge_balance, usdt_balance)
        
        adjustments = self.second_domain.calculate_skew_adjustments(
            inventory_snapshot, base_spread, base_size
        )
        
        if adjustments.get('should_update'):
            self.metrics['second_adjustments'] += 1
        
        return adjustments
    
    def get_twap_orders(self, doge_balance: Decimal, usdt_balance: Decimal) -> List[RebalanceAction]:
        """获取TWAP再平衡订单（分钟级）"""
        if not self.enabled:
            return []
        
        inventory_snapshot = self.calculate_inventory_snapshot(doge_balance, usdt_balance)
        
        actions = self.minute_domain.calculate_twap_orders(inventory_snapshot)
        
        if actions:
            self.metrics['minute_twaps'] += 1
            if inventory_snapshot.emergency_level == EmergencyLevel.EMERGENCY:
                self.metrics['emergency_interventions'] += 1
        
        return actions
    
    def get_system_metrics(self) -> Dict[str, Any]:
        """获取系统综合指标"""
        millisecond_metrics = self.millisecond_domain.get_response_metrics()
        twap_metrics = self.minute_domain.get_twap_metrics()
        
        return {
            'enabled': self.enabled,
            'domain_responses': self.metrics.copy(),
            'millisecond_response': millisecond_metrics,
            'twap_metrics': twap_metrics,
            'system_health': 'optimal' if self.metrics['emergency_interventions'] < 5 else 'stressed'
        }


# 全局实例
_three_domain_inventory_system = None


def get_three_domain_inventory_system() -> ThreeDomainInventorySystem:
    """获取库存管理三时域系统单例"""
    global _three_domain_inventory_system
    if _three_domain_inventory_system is None:
        _three_domain_inventory_system = ThreeDomainInventorySystem()
    return _three_domain_inventory_system