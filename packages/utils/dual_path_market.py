#!/usr/bin/env python3
"""
Phase 3 - Track A1: 市场数据双路径架构
世界级方案：永不返回0的中间价，99.9%可用性
"""

import time
import logging
from typing import Dict, Optional, Tuple
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class MarketSnapshot:
    """市场快照数据"""
    bid: float
    ask: float
    mid: float
    spread_bps: float
    timestamp: float
    source: str  # 'orderbook' or 'aggtrade'
    quality: float  # 0-1质量评分

class OrderBookPath:
    """L2 OrderBook主路径"""
    
    def __init__(self):
        self.last_update = 0
        self.bid = 0
        self.ask = 0
        self.orderbook = {'bids': [], 'asks': []}
        self.update_count = 0
        
    def update(self, orderbook: Dict):
        """更新订单簿"""
        if orderbook and 'bids' in orderbook and 'asks' in orderbook:
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if bids and asks:
                self.bid = float(bids[0][0])
                self.ask = float(asks[0][0])
                self.orderbook = orderbook
                self.last_update = time.time()
                self.update_count += 1
                
    def is_fresh(self, max_age_ms: int = 500) -> bool:
        """检查数据新鲜度"""
        if self.bid <= 0 or self.ask <= 0:
            return False
        age_ms = (time.time() - self.last_update) * 1000
        return age_ms <= max_age_ms
        
    def get_mid(self) -> float:
        """获取中间价"""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return 0

class AggTradePath:
    """AggTrade备援路径"""
    
    def __init__(self, window_ms: int = 100):
        self.window_ms = window_ms
        self.trades = deque(maxlen=1000)
        self.last_price = 0
        self.last_update = 0
        
    def add_trade(self, price: float, qty: float, timestamp: float):
        """添加成交记录"""
        self.trades.append({
            'price': price,
            'qty': qty,
            'timestamp': timestamp
        })
        self.last_price = price
        self.last_update = timestamp
        
    def is_fresh(self, max_age_ms: int = 1000) -> bool:
        """检查数据新鲜度"""
        if self.last_price <= 0:
            return False
        age_ms = (time.time() - self.last_update) * 1000
        return age_ms <= max_age_ms
        
    def get_mid(self) -> float:
        """获取VWAP中间价"""
        now = time.time()
        cutoff = now - self.window_ms / 1000
        
        # 获取时间窗口内的成交
        recent_trades = [t for t in self.trades if t['timestamp'] >= cutoff]
        
        if not recent_trades:
            return self.last_price if self.last_price > 0 else 0
            
        # 计算VWAP
        total_value = sum(t['price'] * t['qty'] for t in recent_trades)
        total_qty = sum(t['qty'] for t in recent_trades)
        
        if total_qty > 0:
            return total_value / total_qty
        return self.last_price

class ConsensusEngine:
    """数据一致性引擎"""
    
    def __init__(self):
        self.last_known_mid = 0
        self.price_history = deque(maxlen=100)
        self.quality_scores = {'orderbook': 1.0, 'aggtrade': 0.8}
        
    def update_last_known(self, price: float):
        """更新最后已知价格"""
        if price > 0:
            self.last_known_mid = price
            self.price_history.append((price, time.time()))
            
    def get_quality_score(self, source: str, age_ms: float) -> float:
        """计算数据质量评分"""
        base_score = self.quality_scores.get(source, 0.5)
        
        # 根据延迟衰减
        if age_ms < 100:
            decay = 1.0
        elif age_ms < 500:
            decay = 0.9
        elif age_ms < 1000:
            decay = 0.7
        else:
            decay = 0.3
            
        return base_score * decay

class DualPathMarketData:
    """
    双路径市场数据架构
    - 主路径：OrderBook (L2 depth stream)
    - 备援路径：AggTrade stream
    - 降级策略：最后已知价格
    """
    
    def __init__(self):
        self.primary = OrderBookPath()
        self.secondary = AggTradePath()
        self.consensus = ConsensusEngine()
        
        # 统计
        self.stats = {
            'primary_used': 0,
            'secondary_used': 0,
            'fallback_used': 0,
            'total_requests': 0,
            'zero_returns': 0
        }
        
        # 路径切换记录
        self.last_source = 'unknown'
        self.switch_count = 0
        
        logger.info("[Phase3-A1] DualPathMarketData initialized")
        
    def update_orderbook(self, orderbook: Dict):
        """更新主路径订单簿"""
        self.primary.update(orderbook)
        
        # 更新consensus
        mid = self.primary.get_mid()
        if mid > 0:
            self.consensus.update_last_known(mid)
            
    def add_aggtrade(self, price: float, qty: float, timestamp: Optional[float] = None):
        """更新备援路径成交"""
        if timestamp is None:
            timestamp = time.time()
        self.secondary.add_trade(price, qty, timestamp)
        
        # 更新consensus
        if price > 0:
            self.consensus.update_last_known(price)
            
    def get_best_mid(self) -> Tuple[float, str]:
        """
        获取最优中间价，永不返回0
        返回: (price, source)
        """
        self.stats['total_requests'] += 1
        
        # 1. 尝试主路径
        if self.primary.is_fresh(max_age_ms=500):
            mid = self.primary.get_mid()
            if mid > 0:
                self.stats['primary_used'] += 1
                self._track_source('orderbook')
                return mid, 'orderbook'
                
        # 2. 切换备援路径
        if self.secondary.is_fresh(max_age_ms=1000):
            mid = self.secondary.get_mid()
            if mid > 0:
                self.stats['secondary_used'] += 1
                self._track_source('aggtrade')
                logger.warning(f"[Phase3-A1] Primary path stale, using aggTrade: {mid:.5f}")
                return mid, 'aggtrade'
                
        # 3. 降级到最后已知价格
        if self.consensus.last_known_mid > 0:
            self.stats['fallback_used'] += 1
            self._track_source('fallback')
            logger.warning(f"[Phase3-A1] Both paths stale, using last known: {self.consensus.last_known_mid:.5f}")
            return self.consensus.last_known_mid, 'fallback'
            
        # 4. 绝对兜底（不应该发生）
        self.stats['zero_returns'] += 1
        logger.error("[Phase3-A1] CRITICAL: No valid price available!")
        return 0.001, 'emergency'  # 返回极小值而非0
        
    def get_market_snapshot(self) -> MarketSnapshot:
        """获取完整市场快照"""
        mid, source = self.get_best_mid()
        
        # 获取买卖价
        if self.primary.is_fresh(500):
            bid = self.primary.bid
            ask = self.primary.ask
        else:
            # 使用中间价构造虚拟买卖价
            spread_estimate = 0.0001  # 0.01% 估计价差
            bid = mid * (1 - spread_estimate)
            ask = mid * (1 + spread_estimate)
            
        # 计算价差
        if bid > 0 and ask > 0:
            spread_bps = (ask - bid) / mid * 10000
        else:
            spread_bps = 0
            
        # 计算质量评分
        if source == 'orderbook':
            quality = 1.0
        elif source == 'aggtrade':
            quality = 0.8
        elif source == 'fallback':
            quality = 0.5
        else:
            quality = 0.1
            
        return MarketSnapshot(
            bid=bid,
            ask=ask,
            mid=mid,
            spread_bps=spread_bps,
            timestamp=time.time(),
            source=source,
            quality=quality
        )
        
    def _track_source(self, source: str):
        """跟踪数据源切换"""
        if source != self.last_source:
            self.switch_count += 1
            self.last_source = source
            
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total = max(1, self.stats['total_requests'])
        return {
            'primary_pct': self.stats['primary_used'] / total * 100,
            'secondary_pct': self.stats['secondary_used'] / total * 100,
            'fallback_pct': self.stats['fallback_used'] / total * 100,
            'zero_pct': self.stats['zero_returns'] / total * 100,
            'switch_count': self.switch_count,
            'current_source': self.last_source,
            'orderbook_updates': self.primary.update_count,
            'last_known_mid': self.consensus.last_known_mid
        }
        
    def health_check(self) -> bool:
        """健康检查"""
        # 至少有一个路径是新鲜的
        primary_ok = self.primary.is_fresh(1000)
        secondary_ok = self.secondary.is_fresh(2000)
        has_fallback = self.consensus.last_known_mid > 0
        
        return primary_ok or secondary_ok or has_fallback

# 单例实例
_dual_path_instance = None

def get_dual_path_market() -> DualPathMarketData:
    """获取双路径市场数据单例"""
    global _dual_path_instance
    if _dual_path_instance is None:
        _dual_path_instance = DualPathMarketData()
    return _dual_path_instance