"""
Active-IOC Planner - 主动腿计划器
负责生成快速清风险的IOC订单计划
"""

import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ExecutionType(Enum):
    """执行类型"""
    IOC = "IOC"  # Immediate Or Cancel
    FOK = "FOK"  # Fill Or Kill
    MARKET = "MARKET"  # 市价单


@dataclass
class ActiveLeg:
    """主动腿订单"""
    venue: str  # 交易场所
    side: str  # BUY/SELL
    qty: float  # 数量（DOGE）
    execution_type: ExecutionType
    price_limit: Optional[float]  # 限价（IOC时使用）
    max_slippage_bps: float  # 最大滑点（基点）
    tag: str  # 标签
    priority: int  # 优先级（1最高）
    metadata: Dict[str, Any] = None
    
    @property
    def is_aggressive(self) -> bool:
        """是否为激进订单"""
        return self.execution_type in [ExecutionType.MARKET, ExecutionType.FOK]


class ActivePlanner:
    """
    主动腿计划器 - FAHE组件
    生成快速清风险的IOC/市价订单计划
    """
    
    def __init__(self,
                 single_order_limit: float = 5000,  # 单笔订单上限（USDT）
                 min_order_size: float = 500,  # 最小订单（USDT）
                 max_slippage_bps: float = 5,  # 最大滑点（5bp）
                 split_threshold: float = 10000,  # 拆单阈值（USDT）
                 emergency_mode_threshold: float = 300):  # 紧急模式Delta阈值
        """
        初始化主动腿计划器
        
        Args:
            single_order_limit: 单笔订单上限
            min_order_size: 最小订单大小
            max_slippage_bps: 最大允许滑点
            split_threshold: 自动拆单阈值
            emergency_mode_threshold: 紧急模式阈值
        """
        self.single_order_limit = single_order_limit
        self.min_order_size = min_order_size
        self.max_slippage_bps = max_slippage_bps
        self.split_threshold = split_threshold
        self.emergency_mode_threshold = emergency_mode_threshold
        
        # 深度分析参数
        self.depth_levels = 3  # 分析L1-L3深度
        self.impact_model_k = 0.5  # 市场冲击系数
        
        # 统计信息
        self.stats = {
            'orders_planned': 0,
            'total_qty_planned': 0.0,
            'avg_slippage_estimate': 0.0,
            'emergency_triggers': 0
        }
        
        logger.info(f"[ActivePlanner] 初始化完成: limit={single_order_limit}, max_slip={max_slippage_bps}bp")
    
    def plan(self, side: str, qty: float, market_data: Dict[str, Any], urgent: bool = False) -> List[ActiveLeg]:
        """
        生成主动腿订单计划
        
        Args:
            side: 买卖方向（BUY/SELL）
            qty: 数量（DOGE）
            market_data: 市场数据
            urgent: 是否紧急
        
        Returns:
            主动腿订单列表
        """
        legs = []
        
        # 检查是否需要紧急模式
        is_emergency = urgent or qty > self.emergency_mode_threshold
        if is_emergency:
            self.stats['emergency_triggers'] += 1
            logger.warning(f"[ActivePlanner] 紧急模式: qty={qty:.2f}")
        
        # 分析市场深度
        depth_analysis = self._analyze_depth(side, qty, market_data)
        
        # 计算拆单策略
        split_strategy = self._calculate_split_strategy(qty, depth_analysis, is_emergency)
        
        # 生成订单腿
        for i, (venue, order_qty, execution_type, price_limit) in enumerate(split_strategy):
            # 估算滑点
            estimated_slippage = self._estimate_slippage(order_qty, depth_analysis)
            
            # 如果滑点过大，进一步拆分或调整
            if estimated_slippage > self.max_slippage_bps and not is_emergency:
                # 降级为更小的订单
                sub_legs = self._create_safer_legs(side, order_qty, market_data, depth_analysis)
                legs.extend(sub_legs)
            else:
                leg = ActiveLeg(
                    venue=venue,
                    side=side,
                    qty=order_qty,
                    execution_type=execution_type,
                    price_limit=price_limit,
                    max_slippage_bps=min(estimated_slippage * 1.2, self.max_slippage_bps * 2),
                    tag=f"active_hedge_{i}",
                    priority=1 if is_emergency else 2,
                    metadata={
                        'estimated_slippage_bps': estimated_slippage,
                        'depth_available': depth_analysis.get('total_depth', 0),
                        'impact_estimate': self._calculate_impact(order_qty, depth_analysis),
                        'is_emergency': is_emergency
                    }
                )
                legs.append(leg)
        
        # 更新统计
        self._update_stats(legs)
        
        logger.info(f"[ActivePlanner] 计划{len(legs)}个主动腿订单: "
                   f"side={side}, total_qty={qty:.2f}, emergency={is_emergency}")
        
        return legs
    
    def _analyze_depth(self, side: str, qty: float, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析市场深度
        
        Args:
            side: 买卖方向
            qty: 数量
            market_data: 市场数据
        
        Returns:
            深度分析结果
        """
        # 获取订单簿数据
        if side == 'BUY':
            # 买单看卖盘
            levels = market_data.get('asks', [])
        else:
            # 卖单看买盘
            levels = market_data.get('bids', [])
        
        # 分析各层深度
        depth_info = {
            'levels': [],
            'total_depth': 0,
            'total_notional': 0,
            'avg_price': 0,
            'can_fill_qty': 0
        }
        
        cumulative_qty = 0
        cumulative_cost = 0
        
        for i in range(min(self.depth_levels, len(levels))):
            level_price = levels[i].get('price', 0)
            level_qty = levels[i].get('qty', 0)
            
            depth_info['levels'].append({
                'price': level_price,
                'qty': level_qty,
                'cumulative_qty': cumulative_qty + level_qty
            })
            
            cumulative_qty += level_qty
            cumulative_cost += level_price * level_qty
            
            if cumulative_qty >= qty:
                depth_info['can_fill_qty'] = qty
                break
        else:
            depth_info['can_fill_qty'] = cumulative_qty
        
        depth_info['total_depth'] = cumulative_qty
        depth_info['total_notional'] = cumulative_cost
        depth_info['avg_price'] = cumulative_cost / cumulative_qty if cumulative_qty > 0 else 0
        
        return depth_info
    
    def _calculate_split_strategy(self, qty: float, depth_analysis: Dict[str, Any], 
                                 is_emergency: bool) -> List[Tuple[str, float, ExecutionType, Optional[float]]]:
        """
        计算拆单策略
        
        Args:
            qty: 总数量
            depth_analysis: 深度分析
            is_emergency: 是否紧急
        
        Returns:
            [(场所, 数量, 执行类型, 限价)]列表
        """
        strategy = []
        remaining_qty = qty
        
        # 获取当前价格
        current_price = depth_analysis.get('avg_price', 0.25)
        
        # 计算单笔上限（DOGE）
        max_qty_per_order = self.single_order_limit / current_price
        
        if is_emergency:
            # 紧急模式：使用更激进的策略
            while remaining_qty > 0:
                order_qty = min(remaining_qty, max_qty_per_order * 2)  # 紧急时可以翻倍
                
                # 紧急订单使用市价或FOK
                if order_qty >= max_qty_per_order:
                    execution_type = ExecutionType.FOK
                else:
                    execution_type = ExecutionType.IOC
                
                # 限价设置为当前价格的102%（买）或98%（卖）作为保护
                price_limit = current_price * 1.02 if remaining_qty > 0 else current_price * 0.98
                
                strategy.append(("BINANCE_USDT", order_qty, execution_type, price_limit))
                remaining_qty -= order_qty
        else:
            # 正常模式：分层执行
            levels = depth_analysis.get('levels', [])
            
            for level in levels:
                if remaining_qty <= 0:
                    break
                
                level_qty = min(level['qty'], remaining_qty, max_qty_per_order)
                
                if level_qty * current_price >= self.min_order_size:
                    # 使用IOC订单，限价为该层价格
                    strategy.append(("BINANCE_USDT", level_qty, ExecutionType.IOC, level['price']))
                    remaining_qty -= level_qty
            
            # 如果还有剩余，使用IOC清理
            if remaining_qty > 0:
                strategy.append(("BINANCE_USDT", remaining_qty, ExecutionType.IOC, 
                               current_price * 1.01))  # 稍微让价
        
        return strategy
    
    def _estimate_slippage(self, qty: float, depth_analysis: Dict[str, Any]) -> float:
        """
        估算滑点
        
        Args:
            qty: 订单数量
            depth_analysis: 深度分析
        
        Returns:
            预期滑点（基点）
        """
        total_depth = depth_analysis.get('total_depth', 0)
        avg_price = depth_analysis.get('avg_price', 0.25)
        
        if total_depth == 0:
            # 没有深度信息，返回最大滑点
            return self.max_slippage_bps
        
        # 计算订单占深度的比例
        depth_ratio = qty / total_depth if total_depth > 0 else 1.0
        
        # 基础滑点模型
        base_slippage = 1.0  # 1bp基础滑点
        
        # 深度影响
        if depth_ratio < 0.1:
            # 小单，滑点小
            depth_impact = 0.5
        elif depth_ratio < 0.3:
            # 中等，线性增长
            depth_impact = 1.0 + (depth_ratio - 0.1) * 5
        else:
            # 大单，指数增长
            depth_impact = 2.0 * (1 + depth_ratio)
        
        # 计算总滑点
        estimated_slippage = base_slippage * depth_impact
        
        return min(estimated_slippage, self.max_slippage_bps * 2)
    
    def _calculate_impact(self, qty: float, depth_analysis: Dict[str, Any]) -> float:
        """
        计算市场冲击
        
        Args:
            qty: 订单数量
            depth_analysis: 深度分析
        
        Returns:
            市场冲击估计（价格变化百分比）
        """
        total_depth = depth_analysis.get('total_depth', 0)
        
        if total_depth == 0:
            return 0.01  # 1%默认冲击
        
        # 平方根市场冲击模型
        impact = self.impact_model_k * (qty / total_depth) ** 0.5
        
        return min(impact, 0.05)  # 最大5%冲击
    
    def _create_safer_legs(self, side: str, qty: float, market_data: Dict[str, Any], 
                          depth_analysis: Dict[str, Any]) -> List[ActiveLeg]:
        """
        创建更安全的订单腿（滑点过大时）
        
        Args:
            side: 买卖方向
            qty: 数量
            market_data: 市场数据
            depth_analysis: 深度分析
        
        Returns:
            安全的订单腿列表
        """
        safer_legs = []
        
        # 将订单拆成更小的块
        current_price = depth_analysis.get('avg_price', 0.25)
        safe_size = self.min_order_size / current_price
        
        num_orders = int(qty / safe_size) + (1 if qty % safe_size > 0 else 0)
        
        for i in range(num_orders):
            order_qty = min(safe_size, qty - i * safe_size)
            
            leg = ActiveLeg(
                venue="BINANCE_USDT",
                side=side,
                qty=order_qty,
                execution_type=ExecutionType.IOC,
                price_limit=current_price * (1.005 if side == 'BUY' else 0.995),
                max_slippage_bps=self.max_slippage_bps,
                tag=f"active_safe_{i}",
                priority=3,
                metadata={
                    'is_safer_split': True,
                    'original_qty': qty
                }
            )
            safer_legs.append(leg)
        
        return safer_legs
    
    def _update_stats(self, legs: List[ActiveLeg]) -> None:
        """
        更新统计信息
        
        Args:
            legs: 订单腿列表
        """
        self.stats['orders_planned'] += len(legs)
        self.stats['total_qty_planned'] += sum(leg.qty for leg in legs)
        
        # 更新平均滑点估计
        if legs:
            slippages = [leg.metadata.get('estimated_slippage_bps', 0) for leg in legs if leg.metadata]
            if slippages:
                avg_slippage = sum(slippages) / len(slippages)
                alpha = 0.1  # EWMA系数
                self.stats['avg_slippage_estimate'] = \
                    (1 - alpha) * self.stats['avg_slippage_estimate'] + alpha * avg_slippage
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        return {
            **self.stats,
            'single_order_limit': self.single_order_limit,
            'max_slippage_bps': self.max_slippage_bps
        }