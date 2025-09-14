"""
Passive-Maker Planner - 被动腿计划器
负责生成返佣优先的Maker订单计划
"""

import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """订单类型"""
    POST_ONLY = "POST_ONLY"
    GTX = "GTX"  # Good Till Cross
    LIMIT = "LIMIT"


class Venue(Enum):
    """交易场所"""
    BINANCE_USDT = "BINANCE_USDT"
    BINANCE_USDC = "BINANCE_USDC"
    OKX_USDT = "OKX_USDT"


@dataclass
class PassiveLeg:
    """被动腿订单"""
    venue: Venue
    side: str  # BUY/SELL
    qty: float  # 数量（DOGE）
    price_offset: int  # 价格偏移（tick）
    order_type: OrderType
    ttl_ms: int  # Time To Live（毫秒）
    tag: str  # 标签
    metadata: Dict[str, Any] = None
    
    @property
    def notional_estimate(self) -> float:
        """估算名义价值"""
        # 简化计算，实际需要根据市场价格
        return self.qty * 0.25  # 假设DOGE价格0.25 USDT


class PassivePlanner:
    """
    被动腿计划器 - FAHE组件
    生成返佣优先的Maker订单计划
    """
    
    def __init__(self,
                 default_ttl_ms: int = 800,
                 min_ttl_ms: int = 300,
                 max_ttl_ms: int = 1200,
                 target_fill_prob: float = 0.7,
                 rebate_usdt: float = -0.0003,  # -0.03%
                 rebate_usdc: float = -0.0006):  # -0.06%
        """
        初始化被动腿计划器
        
        Args:
            default_ttl_ms: 默认TTL
            min_ttl_ms: 最小TTL
            max_ttl_ms: 最大TTL
            target_fill_prob: 目标成交概率
            rebate_usdt: USDT返佣率
            rebate_usdc: USDC返佣率
        """
        self.default_ttl_ms = default_ttl_ms
        self.min_ttl_ms = min_ttl_ms
        self.max_ttl_ms = max_ttl_ms
        self.target_fill_prob = target_fill_prob
        self.rebate_usdt = rebate_usdt
        self.rebate_usdc = rebate_usdc
        
        # 队列位置估计参数
        self.queue_decay_rate = 0.1  # 队列衰减率
        self.fill_hazard_base = 0.5  # 基础成交危险率
        
        # 价格偏移策略
        self.initial_offset = 0  # 初始在touch
        self.retry_offset = 1  # 重试时让1tick
        
        # 统计信息
        self.stats = {
            'orders_planned': 0,
            'total_qty_planned': 0.0,
            'avg_ttl_ms': default_ttl_ms,
            'venue_distribution': {}
        }
        
        logger.info(f"[PassivePlanner] 初始化完成: ttl={default_ttl_ms}ms, fill_prob={target_fill_prob}")
    
    def plan(self, side: str, qty: float, market_data: Dict[str, Any]) -> List[PassiveLeg]:
        """
        生成被动腿订单计划
        
        Args:
            side: 买卖方向（BUY/SELL）
            qty: 数量（DOGE）
            market_data: 市场数据
        
        Returns:
            被动腿订单列表
        """
        legs = []
        
        # 选择最优场所
        venue = self._select_venue(market_data)
        
        # 计算TTL
        ttl_ms = self._calculate_ttl(market_data)
        
        # 计算价格偏移
        price_offset = self._calculate_price_offset(side, market_data)
        
        # 拆分订单（如果太大）
        order_sizes = self._split_order(qty, market_data)
        
        for i, size in enumerate(order_sizes):
            leg = PassiveLeg(
                venue=venue,
                side=side,
                qty=size,
                price_offset=price_offset,
                order_type=OrderType.POST_ONLY,
                ttl_ms=ttl_ms,
                tag=f"passive_hedge_{i}",
                metadata={
                    'expected_rebate_bps': self._get_rebate_bps(venue),
                    'fill_prob_estimate': self._estimate_fill_prob(price_offset, ttl_ms, market_data),
                    'queue_position_estimate': self._estimate_queue_position(price_offset, market_data)
                }
            )
            legs.append(leg)
        
        # 更新统计
        self._update_stats(legs)
        
        logger.info(f"[PassivePlanner] 计划{len(legs)}个被动腿订单: "
                   f"side={side}, total_qty={qty:.2f}, venue={venue.value}")
        
        return legs
    
    def _select_venue(self, market_data: Dict[str, Any]) -> Venue:
        """
        选择最优交易场所
        
        Args:
            market_data: 市场数据
        
        Returns:
            选择的场所
        """
        # 获取各场所的状态
        usdc_available = market_data.get('usdc_available', True)
        usdc_queue_friendly = market_data.get('usdc_queue_depth', 1000) > 500
        usdt_congested = market_data.get('usdt_congestion', False)
        
        # USDC返佣更高，优先选择
        if usdc_available and usdc_queue_friendly and not usdt_congested:
            return Venue.BINANCE_USDC
        
        # 默认USDT
        return Venue.BINANCE_USDT
    
    def _calculate_ttl(self, market_data: Dict[str, Any]) -> int:
        """
        计算动态TTL
        
        Args:
            market_data: 市场数据
        
        Returns:
            TTL（毫秒）
        """
        # 获取市场参数
        sigma = market_data.get('volatility_30s', 0.001)
        queue_depth = market_data.get('queue_depth', 1000)
        toxicity = market_data.get('queue_toxicity', 0.3)
        
        # 波动率越低，TTL越长
        volatility_factor = max(0.5, 1.0 - sigma * 100)
        
        # 队列越深，TTL越长
        queue_factor = min(1.5, queue_depth / 1000)
        
        # 毒性越高，TTL越短
        toxicity_factor = max(0.5, 1.0 - toxicity)
        
        # 计算TTL
        ttl_ms = int(self.default_ttl_ms * volatility_factor * queue_factor * toxicity_factor)
        
        # 限制范围
        ttl_ms = max(self.min_ttl_ms, min(self.max_ttl_ms, ttl_ms))
        
        return ttl_ms
    
    def _calculate_price_offset(self, side: str, market_data: Dict[str, Any]) -> int:
        """
        计算价格偏移
        
        Args:
            side: 买卖方向
            market_data: 市场数据
        
        Returns:
            价格偏移（tick数）
        """
        # 获取市场参数
        toxicity = market_data.get('queue_toxicity', 0.3)
        microprice_bias = market_data.get('microprice_bias', 0)  # 正=买压，负=卖压
        
        # 基础偏移（touch）
        offset = self.initial_offset
        
        # 根据毒性调整
        if toxicity > 0.6:
            # 高毒性，让1tick
            offset = self.retry_offset
        
        # 根据微价格偏置调整
        if side == 'BUY' and microprice_bias < -0.0001:
            # 卖压大，买单可以更激进
            offset = -1  # 越过touch
        elif side == 'SELL' and microprice_bias > 0.0001:
            # 买压大，卖单可以更激进
            offset = -1  # 越过touch
        
        return offset
    
    def _split_order(self, qty: float, market_data: Dict[str, Any]) -> List[float]:
        """
        拆分大订单
        
        Args:
            qty: 总数量
            market_data: 市场数据
        
        Returns:
            拆分后的数量列表
        """
        # 单笔上限（USDT）
        max_notional = 5000  # 5k USDT
        price = market_data.get('mid_price', 0.25)
        max_qty = max_notional / price
        
        if qty <= max_qty:
            return [qty]
        
        # 拆分成多笔
        orders = []
        remaining = qty
        while remaining > 0:
            size = min(remaining, max_qty)
            orders.append(size)
            remaining -= size
        
        return orders
    
    def _get_rebate_bps(self, venue: Venue) -> float:
        """
        获取返佣率（基点）
        
        Args:
            venue: 交易场所
        
        Returns:
            返佣率（基点）
        """
        if venue == Venue.BINANCE_USDC:
            return self.rebate_usdc * 10000  # -6bp
        elif venue == Venue.BINANCE_USDT:
            return self.rebate_usdt * 10000  # -3bp
        else:
            return 0
    
    def _estimate_fill_prob(self, price_offset: int, ttl_ms: int, market_data: Dict[str, Any]) -> float:
        """
        估算成交概率
        
        Args:
            price_offset: 价格偏移
            ttl_ms: TTL
            market_data: 市场数据
        
        Returns:
            成交概率 [0,1]
        """
        # 获取队列参数
        queue_depth = market_data.get('queue_depth', 1000)
        arrival_rate = market_data.get('arrival_rate', 1.0)  # 订单到达率
        
        # 价格偏移影响
        if price_offset < 0:
            # 越过touch，成交概率高
            base_prob = 0.9
        elif price_offset == 0:
            # 在touch，看队列
            base_prob = 0.5
        else:
            # 让价，成交概率低
            base_prob = 0.3 / (1 + price_offset)
        
        # TTL影响
        ttl_factor = min(1.0, ttl_ms / 1000)  # TTL越长，概率越高
        
        # 队列深度影响
        queue_factor = max(0.3, 1.0 - queue_depth / 5000)
        
        # 综合概率
        fill_prob = base_prob * ttl_factor * queue_factor
        
        return min(1.0, max(0.0, fill_prob))
    
    def _estimate_queue_position(self, price_offset: int, market_data: Dict[str, Any]) -> int:
        """
        估算队列位置
        
        Args:
            price_offset: 价格偏移
            market_data: 市场数据
        
        Returns:
            预期队列位置
        """
        if price_offset < 0:
            # 越过touch，队列前面
            return 0
        elif price_offset == 0:
            # 在touch，看当前队列
            return market_data.get('queue_depth', 1000)
        else:
            # 让价，新队列
            return 0
    
    def _update_stats(self, legs: List[PassiveLeg]) -> None:
        """
        更新统计信息
        
        Args:
            legs: 订单腿列表
        """
        self.stats['orders_planned'] += len(legs)
        self.stats['total_qty_planned'] += sum(leg.qty for leg in legs)
        
        # 更新TTL平均值
        if legs:
            avg_ttl = sum(leg.ttl_ms for leg in legs) / len(legs)
            alpha = 0.1  # EWMA系数
            self.stats['avg_ttl_ms'] = (1 - alpha) * self.stats['avg_ttl_ms'] + alpha * avg_ttl
        
        # 更新场所分布
        for leg in legs:
            venue_name = leg.venue.value
            self.stats['venue_distribution'][venue_name] = \
                self.stats['venue_distribution'].get(venue_name, 0) + 1
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'default_ttl_ms': self.default_ttl_ms,
            'target_fill_prob': self.target_fill_prob
        }