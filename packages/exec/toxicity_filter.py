"""
Phase 9: 毒性过滤器 (Order-flow toxicity)
基于spread压缩 + 成交突增 + L2倾斜 + 短期动量的毒性评分
"""
import time
import math
import logging
from typing import Dict, Any, List, Tuple, Optional
from collections import deque

logger = logging.getLogger(__name__)


class ToxicityFilter:
    """订单流毒性过滤器 - 对标世界级做市商防御机制"""
    
    def __init__(self):
        # 参数配置
        self.window_size = 30.0      # 30秒观测窗口
        self.momentum_window = 5.0   # 5秒短期动量窗口
        self.min_samples = 3         # 最小样本数
        
        # 历史数据
        self.spread_history = deque()        # (timestamp, spread_bps)
        self.trade_history = deque()         # (timestamp, qty, side, price)
        self.depth_history = deque()         # (timestamp, bid_qty, ask_qty, imbalance)
        self.price_history = deque()         # (timestamp, mid_price)
        
        # 基准值（用于检测异常）
        self.baseline_spread = 50.0          # 基准spread (bp)
        self.baseline_trade_intensity = 0.0  # 基准成交强度
        self.baseline_depth_ratio = 1.0      # 基准深度比例
        
        # 权重配置
        self.spread_weight = 0.3
        self.intensity_weight = 0.25
        self.imbalance_weight = 0.25
        self.momentum_weight = 0.2
        
        # 防御参数
        self.defense_threshold = 0.6         # 高毒性阈值
        self.max_widen_bps = 15.0           # 最大加宽幅度
        self.min_size_scale = 0.3           # 最小订单量比例
        self.min_ttl_scale = 0.5            # 最小TTL比例
        
        logger.info(f"[Toxic] ToxicityFilter initialized: threshold={self.defense_threshold}")
    
    def update_spread(self, spread_bps: float):
        """更新spread历史"""
        now = time.time()
        self.spread_history.append((now, spread_bps))
        self._cleanup_old_data()
        
        # 更新基准值
        if len(self.spread_history) >= 10:
            recent_spreads = [s for _, s in list(self.spread_history)[-10:]]
            self.baseline_spread = sum(recent_spreads) / len(recent_spreads)
    
    def update_trade(self, qty: float, side: str, price: float):
        """更新成交历史"""
        now = time.time()
        self.trade_history.append((now, qty, side, price))
        self._cleanup_old_data()
        
        # 更新成交强度基准
        self._update_trade_intensity_baseline()
    
    def update_depth(self, bid_qty: float, ask_qty: float):
        """更新深度历史"""
        now = time.time()
        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / max(1.0, total_qty)  # [-1, 1]
        self.depth_history.append((now, bid_qty, ask_qty, imbalance))
        self._cleanup_old_data()
    
    def update_price(self, mid_price: float):
        """更新价格历史"""
        now = time.time()
        self.price_history.append((now, mid_price))
        self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """清理过期数据"""
        now = time.time()
        cutoff = now - self.window_size
        
        while self.spread_history and self.spread_history[0][0] < cutoff:
            self.spread_history.popleft()
        while self.trade_history and self.trade_history[0][0] < cutoff:
            self.trade_history.popleft()
        while self.depth_history and self.depth_history[0][0] < cutoff:
            self.depth_history.popleft()
        while self.price_history and self.price_history[0][0] < cutoff:
            self.price_history.popleft()
    
    def _update_trade_intensity_baseline(self):
        """更新成交强度基准"""
        if len(self.trade_history) < 10:
            return
            
        now = time.time()
        recent_window = 60.0  # 1分钟窗口计算基准
        cutoff = now - recent_window
        
        recent_trades = [(ts, qty) for ts, qty, side, price in self.trade_history if ts > cutoff]
        if len(recent_trades) >= 5:
            total_qty = sum(qty for ts, qty in recent_trades)
            time_span = max(1.0, recent_trades[-1][0] - recent_trades[0][0])
            self.baseline_trade_intensity = total_qty / time_span
    
    def calculate_spread_compression_score(self) -> float:
        """计算spread压缩评分 [0,1]"""
        if len(self.spread_history) < self.min_samples:
            return 0.0
            
        recent_spreads = [s for _, s in list(self.spread_history)[-5:]]
        avg_recent_spread = sum(recent_spreads) / len(recent_spreads)
        
        # spread压缩程度：spread越小于基准，毒性越高
        compression_ratio = avg_recent_spread / max(1.0, self.baseline_spread)
        compression_score = max(0.0, 1.0 - compression_ratio)
        
        return min(1.0, compression_score)
    
    def calculate_trade_intensity_score(self) -> float:
        """计算成交突增评分 [0,1]"""
        if len(self.trade_history) < self.min_samples:
            return 0.0
            
        now = time.time()
        short_window = 10.0  # 10秒短期窗口
        cutoff = now - short_window
        
        recent_trades = [(ts, qty) for ts, qty, side, price in self.trade_history if ts > cutoff]
        if len(recent_trades) < 2:
            return 0.0
            
        total_qty = sum(qty for ts, qty in recent_trades)
        time_span = max(1.0, recent_trades[-1][0] - recent_trades[0][0])
        current_intensity = total_qty / time_span
        
        # 相对于基准的强度倍数
        if self.baseline_trade_intensity <= 0:
            return 0.0
            
        intensity_ratio = current_intensity / self.baseline_trade_intensity
        intensity_score = min(1.0, max(0.0, (intensity_ratio - 1.0) / 3.0))  # 超过基准3倍为满分
        
        return intensity_score
    
    def calculate_depth_imbalance_score(self) -> float:
        """计算深度失衡评分 [0,1]"""
        if len(self.depth_history) < self.min_samples:
            return 0.0
            
        recent_imbalances = [imb for _, _, _, imb in list(self.depth_history)[-5:]]
        avg_imbalance = sum(abs(imb) for imb in recent_imbalances) / len(recent_imbalances)
        
        # 失衡程度评分：失衡越大，毒性越高
        imbalance_score = min(1.0, avg_imbalance * 2.0)  # 50%失衡为满分
        
        return imbalance_score
    
    def calculate_momentum_score(self) -> float:
        """计算短期动量评分 [0,1]"""
        if len(self.price_history) < 3:
            return 0.0
            
        now = time.time()
        cutoff = now - self.momentum_window
        
        recent_prices = [(ts, price) for ts, price in self.price_history if ts > cutoff]
        if len(recent_prices) < 3:
            return 0.0
            
        # 计算价格变化率
        start_price = recent_prices[0][1]
        end_price = recent_prices[-1][1]
        price_change_pct = abs(end_price - start_price) / max(1e-8, start_price)
        
        # 动量评分：短期变化越大，毒性越高
        momentum_score = min(1.0, price_change_pct * 100.0)  # 1%变化为满分
        
        return momentum_score
    
    def calculate_toxicity_score(self) -> float:
        """计算综合毒性评分 [0,1]"""
        spread_score = self.calculate_spread_compression_score()
        intensity_score = self.calculate_trade_intensity_score()
        imbalance_score = self.calculate_depth_imbalance_score()
        momentum_score = self.calculate_momentum_score()
        
        # 加权平均
        tox_score = (
            spread_score * self.spread_weight +
            intensity_score * self.intensity_weight +
            imbalance_score * self.imbalance_weight +
            momentum_score * self.momentum_weight
        )
        
        return min(1.0, max(0.0, tox_score))
    
    def calculate_defense_adjustments(self, tox_score: float) -> Dict[str, float]:
        """
        B5: 计算反毒性防御调整参数
        使用指数衰减公式: size_scale = clip(exp(-k * toxicity), s_min, s_max)
        """
        if tox_score < self.defense_threshold:
            # 低毒性：正常或略微激进
            widen_bps = 0.0
            size_scale = 1.0 + (self.defense_threshold - tox_score) * 0.2  # 最多+20%
            ttl_scale = 1.0 + (self.defense_threshold - tox_score) * 0.3   # 最多+30%
        else:
            # B5: 高毒性防御模式 - 使用指数衰减
            tox_excess = tox_score - self.defense_threshold
            widen_bps = tox_excess * self.max_widen_bps / (1.0 - self.defense_threshold)
            
            # B5: 反毒性缩放 size_scale = clip(exp(-k * toxicity), s_min, s_max)
            k = 2.0  # 衰减系数，控制敏感度
            s_min = self.min_size_scale  # 0.3
            s_max = 1.0
            
            # 指数衰减：毒性越高，订单量越小
            raw_size_scale = math.exp(-k * tox_score)
            size_scale = max(s_min, min(s_max, raw_size_scale))
            
            # TTL也使用类似逻辑
            ttl_scale = max(self.min_ttl_scale, 1.0 - tox_excess * 0.5)
            
            logger.debug(f"[B5] tox_score={tox_score:.3f} k={k} raw_scale={raw_size_scale:.3f} "
                        f"clipped_scale={size_scale:.3f} (range=[{s_min}, {s_max}])")
        
        return {
            'widen_bps': widen_bps,
            'size_scale': size_scale,
            'ttl_scale': ttl_scale
        }
    
    def analyze_toxicity(self) -> Dict[str, Any]:
        """分析当前毒性并给出调整建议"""
        tox_score = self.calculate_toxicity_score()
        adjustments = self.calculate_defense_adjustments(tox_score)
        
        return {
            'tox_score': tox_score,
            'spread_widen_bps': adjustments['widen_bps'],
            'size_scale': adjustments['size_scale'],
            'ttl_scale': adjustments['ttl_scale'],
            'defense_mode': tox_score >= self.defense_threshold
        }
    
    def log_analysis(self, analysis: Dict[str, Any]):
        """按照Phase 9.3模板输出Toxic状态线"""
        logger.info(
            f"[Toxic] score={analysis['tox_score']:.2f} "
            f"spread_widen_bps={analysis['spread_widen_bps']:.1f} "
            f"size_scale={analysis['size_scale']:.2f} "
            f"ttl_scale={analysis['ttl_scale']:.2f}"
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取毒性过滤器统计信息"""
        return {
            'samples': {
                'spread': len(self.spread_history),
                'trade': len(self.trade_history),
                'depth': len(self.depth_history),
                'price': len(self.price_history)
            },
            'baselines': {
                'spread_bps': self.baseline_spread,
                'trade_intensity': self.baseline_trade_intensity,
                'depth_ratio': self.baseline_depth_ratio
            },
            'current_scores': {
                'spread_compression': self.calculate_spread_compression_score(),
                'trade_intensity': self.calculate_trade_intensity_score(),
                'depth_imbalance': self.calculate_depth_imbalance_score(),
                'momentum': self.calculate_momentum_score()
            }
        }