#!/usr/bin/env python3
"""
Dynamic Cushion - 动态资金垫
基于市场波动率和成交频率自适应调整资金预留
"""

import time
import math
import logging
from typing import Dict, List, Tuple, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class VolatilityCalculator:
    """波动率计算器"""
    
    def __init__(self, window_size: int = 20, sample_interval: float = 1.0):
        self.window_size = window_size
        self.sample_interval = sample_interval
        self.price_samples = deque(maxlen=window_size)
        self.last_sample_time = 0
        
    def add_price(self, price: float):
        """添加价格样本"""
        now = time.time()
        if now - self.last_sample_time >= self.sample_interval:
            self.price_samples.append({
                'price': price,
                'timestamp': now
            })
            self.last_sample_time = now
            
    def calculate_volatility(self) -> float:
        """计算历史波动率"""
        if len(self.price_samples) < 2:
            return 0.001  # 默认波动率
            
        prices = [s['price'] for s in self.price_samples]
        
        # 计算对数收益率
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                log_return = math.log(prices[i] / prices[i-1])
                log_returns.append(log_return)
                
        if not log_returns:
            return 0.001
            
        # 标准差作为波动率
        mean_return = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_return) ** 2 for r in log_returns) / len(log_returns)
        volatility = math.sqrt(variance)
        
        # 年化波动率（假设1分钟采样）
        annualized_vol = volatility * math.sqrt(525600)  # 分钟/年
        
        return min(annualized_vol, 1.0)  # 限制最大1.0


class FillRateTracker:
    """成交频率跟踪器"""
    
    def __init__(self, window_minutes: int = 10):
        self.window_seconds = window_minutes * 60
        self.fill_events = deque()
        
    def add_fill(self, quantity: float, price: float):
        """添加成交事件"""
        now = time.time()
        self.fill_events.append({
            'timestamp': now,
            'quantity': quantity,
            'price': price,
            'notional': quantity * price
        })
        
        # 清理过期事件
        cutoff = now - self.window_seconds
        while self.fill_events and self.fill_events[0]['timestamp'] < cutoff:
            self.fill_events.popleft()
            
    def get_fills_per_minute(self) -> float:
        """获取每分钟成交次数"""
        if not self.fill_events:
            return 0.0
            
        # 计算时间窗口内的成交频率
        window_minutes = self.window_seconds / 60
        return len(self.fill_events) / window_minutes
        
    def get_notional_per_minute(self) -> float:
        """获取每分钟成交金额"""
        if not self.fill_events:
            return 0.0
            
        total_notional = sum(event['notional'] for event in self.fill_events)
        window_minutes = self.window_seconds / 60
        return total_notional / window_minutes


class DynamicCushion:
    """动态资金垫管理器"""
    
    def __init__(self, config: Dict = None):
        """
        初始化动态Cushion
        
        Args:
            config: 配置字典
        """
        self.cfg = config or self._load_config()
        
        # 组件
        self.vol_calc = VolatilityCalculator(
            window_size=self.cfg['volatility_window'],
            sample_interval=self.cfg['volatility_sample_interval']
        )
        self.fill_tracker = FillRateTracker(self.cfg['fill_rate_window'])
        
        # 状态
        self.current_cushion = {
            'USDT': self.cfg['base_usdt'],
            'DOGE': self.cfg['base_doge']
        }
        self.last_update = 0
        self.update_interval = self.cfg['update_interval']
        
        # 历史记录
        self.cushion_history = deque(maxlen=100)
        
        # 统计
        self.stats = {
            'updates': 0,
            'vol_triggers': 0,
            'fill_triggers': 0,
            'equity_limits': 0,
            'max_cushion_usdt': self.cfg['base_usdt'],
            'max_cushion_doge': self.cfg['base_doge'],
            'avg_volatility': 0.0,
            'avg_fill_rate': 0.0
        }
        
    def _load_config(self) -> Dict:
        """加载配置"""
        import os
        return {
            'base_usdt': float(os.getenv('CUSHION_BASE_USDT', '10')),
            'base_doge': float(os.getenv('CUSHION_BASE_DOGE', '30')),
            'max_pct': float(os.getenv('CUSHION_MAX_PCT', '0.05')),
            'volatility_factor': float(os.getenv('CUSHION_VOLATILITY_FACTOR', '2')),
            'fill_rate_factor': float(os.getenv('CUSHION_FILL_RATE_FACTOR', '0.1')),
            'volatility_window': int(os.getenv('CUSHION_VOLATILITY_WINDOW', '20')),
            'volatility_sample_interval': float(os.getenv('CUSHION_VOL_SAMPLE_INTERVAL', '1.0')),
            'fill_rate_window': int(os.getenv('CUSHION_FILL_RATE_WINDOW', '10')),
            'update_interval': float(os.getenv('CUSHION_UPDATE_INTERVAL', '30')),
            'smooth_factor': float(os.getenv('CUSHION_SMOOTH_FACTOR', '0.3')),
            'vol_threshold': float(os.getenv('CUSHION_VOL_THRESHOLD', '0.01')),
            'fill_threshold': float(os.getenv('CUSHION_FILL_THRESHOLD', '5.0'))
        }
        
    def add_market_data(self, price: float):
        """添加市场数据"""
        self.vol_calc.add_price(price)
        
    def add_fill_event(self, quantity: float, price: float):
        """添加成交事件"""
        self.fill_tracker.add_fill(quantity, price)
        
    def calculate_cushion(self, equity: float, mid_price: float, 
                         force_update: bool = False) -> Dict[str, float]:
        """
        计算动态Cushion
        
        Args:
            equity: 总权益
            mid_price: 中间价
            force_update: 强制更新
            
        Returns:
            {asset: cushion_amount}
        """
        now = time.time()
        
        # 检查是否需要更新
        if not force_update and now - self.last_update < self.update_interval:
            return self.current_cushion.copy()
            
        # 计算市场指标
        volatility = self.vol_calc.calculate_volatility()
        fills_per_min = self.fill_tracker.get_fills_per_minute()
        
        # 基础Cushion
        base_usdt = self.cfg['base_usdt']
        base_doge = self.cfg['base_doge']
        
        # 波动率调整因子
        vol_factor = 1.0
        if volatility > self.cfg['vol_threshold']:
            vol_factor = 1 + volatility * self.cfg['volatility_factor']
            self.stats['vol_triggers'] += 1
            
        # 成交频率调整因子
        fill_factor = 1.0
        if fills_per_min > self.cfg['fill_threshold']:
            fill_factor = 1 + fills_per_min * self.cfg['fill_rate_factor']
            self.stats['fill_triggers'] += 1
            
        # 计算新Cushion
        new_cushion_usdt = base_usdt * vol_factor * fill_factor
        new_cushion_doge = base_doge * vol_factor * fill_factor
        
        # 权益比例限制
        max_cushion_by_equity = equity * self.cfg['max_pct']
        if new_cushion_usdt > max_cushion_by_equity:
            new_cushion_usdt = max_cushion_by_equity
            self.stats['equity_limits'] += 1
            
        if new_cushion_doge * mid_price > max_cushion_by_equity:
            new_cushion_doge = max_cushion_by_equity / mid_price
            self.stats['equity_limits'] += 1
            
        # 平滑过渡
        smooth_factor = self.cfg['smooth_factor']
        smoothed_usdt = (
            self.current_cushion['USDT'] * (1 - smooth_factor) + 
            new_cushion_usdt * smooth_factor
        )
        smoothed_doge = (
            self.current_cushion['DOGE'] * (1 - smooth_factor) +
            new_cushion_doge * smooth_factor
        )
        
        # 更新状态
        self.current_cushion = {
            'USDT': smoothed_usdt,
            'DOGE': smoothed_doge
        }
        
        self.last_update = now
        self.stats['updates'] += 1
        
        # 更新统计
        self.stats['max_cushion_usdt'] = max(
            self.stats['max_cushion_usdt'], 
            smoothed_usdt
        )
        self.stats['max_cushion_doge'] = max(
            self.stats['max_cushion_doge'],
            smoothed_doge
        )
        self.stats['avg_volatility'] = (
            self.stats['avg_volatility'] * 0.9 + volatility * 0.1
        )
        self.stats['avg_fill_rate'] = (
            self.stats['avg_fill_rate'] * 0.9 + fills_per_min * 0.1
        )
        
        # 记录历史
        self.cushion_history.append({
            'timestamp': now,
            'usdt': smoothed_usdt,
            'doge': smoothed_doge,
            'volatility': volatility,
            'fills_per_min': fills_per_min,
            'vol_factor': vol_factor,
            'fill_factor': fill_factor
        })
        
        logger.debug(
            f"[DynamicCushion] 更新: USDT={smoothed_usdt:.2f} DOGE={smoothed_doge:.1f} "
            f"vol={volatility:.4f} fills={fills_per_min:.1f}/min "
            f"vol_factor={vol_factor:.2f} fill_factor={fill_factor:.2f}"
        )
        
        return self.current_cushion.copy()
        
    def get_current_cushion(self) -> Dict[str, float]:
        """获取当前Cushion"""
        return self.current_cushion.copy()
        
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()
        
    def get_status(self) -> Dict:
        """获取详细状态"""
        volatility = self.vol_calc.calculate_volatility()
        fills_per_min = self.fill_tracker.get_fills_per_minute()
        
        return {
            'current_cushion': self.current_cushion.copy(),
            'volatility': volatility,
            'fills_per_min': fills_per_min,
            'last_update': self.last_update,
            'time_since_update': time.time() - self.last_update,
            'stats': self.stats.copy(),
            'history_size': len(self.cushion_history)
        }
        
    def get_summary(self) -> str:
        """获取状态摘要"""
        status = self.get_status()
        return (
            f"cushion(USDT={self.current_cushion['USDT']:.1f} "
            f"DOGE={self.current_cushion['DOGE']:.0f}) "
            f"vol={status['volatility']:.3f} "
            f"fills={status['fills_per_min']:.1f}/min"
        )
        
    def reset_stats(self):
        """重置统计"""
        self.stats = {
            'updates': 0,
            'vol_triggers': 0,
            'fill_triggers': 0,
            'equity_limits': 0,
            'max_cushion_usdt': self.cfg['base_usdt'],
            'max_cushion_doge': self.cfg['base_doge'],
            'avg_volatility': 0.0,
            'avg_fill_rate': 0.0
        }


# 全局实例
_cushion_instance = None


def get_dynamic_cushion(config: Dict = None) -> DynamicCushion:
    """获取全局动态Cushion实例"""
    global _cushion_instance
    
    if _cushion_instance is None:
        _cushion_instance = DynamicCushion(config)
        
    return _cushion_instance


def reset_dynamic_cushion():
    """重置全局实例"""
    global _cushion_instance
    _cushion_instance = None


if __name__ == "__main__":
    # 简单测试
    dc = DynamicCushion()
    
    # 模拟市场数据
    base_price = 0.24
    for i in range(50):
        # 添加带波动的价格
        price = base_price + 0.001 * math.sin(i * 0.1) + 0.0005 * (i % 3 - 1)
        dc.add_market_data(price)
        
        # 模拟成交
        if i % 5 == 0:
            dc.add_fill_event(100, price)
            
        time.sleep(0.1)
        
    # 计算Cushion
    equity = 1000
    mid_price = 0.24
    
    cushion = dc.calculate_cushion(equity, mid_price, force_update=True)
    print(f"动态Cushion: {cushion}")
    print(f"详细状态: {dc.get_status()}")
    print(f"摘要: {dc.get_summary()}")