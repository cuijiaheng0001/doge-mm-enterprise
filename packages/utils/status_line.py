#!/usr/bin/env python3
"""
Status Line Monitor - 单行实时状态监控
每5秒输出关键指标的单行状态
"""

import time
import logging
import asyncio
from typing import Dict, List, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class StatusLineMonitor:
    """单行状态监控器"""
    
    def __init__(self, interval: float = 5.0, log_to_console: bool = True):
        """
        初始化状态监控器
        
        Args:
            interval: 输出间隔（秒）
            log_to_console: 是否输出到控制台
        """
        self.interval = interval
        self.log_to_console = log_to_console
        self.running = False
        
        # 状态提供者
        self.providers = {}  # name -> callable
        
        # 历史状态
        self.status_history = []
        self.max_history = 100
        
    def register_provider(self, name: str, provider: Callable[[], str]):
        """
        注册状态提供者
        
        Args:
            name: 提供者名称
            provider: 返回状态字符串的函数
        """
        self.providers[name] = provider
        
    def format_status_line(self) -> str:
        """格式化单行状态"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # 收集所有状态
        status_parts = [f"[{timestamp}]"]
        
        for name, provider in self.providers.items():
            try:
                status = provider()
                if status:
                    status_parts.append(f"{name}={status}")
            except Exception as e:
                status_parts.append(f"{name}=ERROR")
                logger.debug(f"状态提供者 {name} 异常: {e}")
                
        return " ".join(status_parts)
        
    def output_status(self):
        """输出状态行"""
        status_line = self.format_status_line()
        
        # 记录历史
        self.status_history.append({
            'timestamp': time.time(),
            'status': status_line
        })
        
        # 限制历史长度
        if len(self.status_history) > self.max_history:
            self.status_history.pop(0)
            
        # 输出
        if self.log_to_console:
            print(f"\r{status_line}", end="", flush=True)
        else:
            logger.info(f"[STATUS] {status_line}")
            
    async def run(self):
        """运行监控循环"""
        self.running = True
        logger.info(f"[StatusMonitor] 启动，间隔 {self.interval}s")
        
        try:
            while self.running:
                self.output_status()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass
        finally:
            if self.log_to_console:
                print()  # 换行
            logger.info("[StatusMonitor] 停止")
            
    def stop(self):
        """停止监控"""
        self.running = False
        
    def get_history(self, limit: int = 10) -> List[Dict]:
        """获取状态历史"""
        return self.status_history[-limit:]


class StandardStatusProviders:
    """标准状态提供者集合"""
    
    @staticmethod
    def create_trading_status(strategy_instance) -> Dict[str, Callable]:
        """创建交易状态提供者"""
        providers = {}
        
        # 利用率状态
        def util_status():
            try:
                util = getattr(strategy_instance, 'util_onbook', 0)
                return f"{util:.2%}"
            except:
                return "N/A"
        providers['util'] = util_status
        
        # 有效仓位比例
        def frac_status():
            try:
                frac = getattr(strategy_instance, 'usdt_frac_eff', 0.5)
                return f"{frac:.2%}"
            except:
                return "N/A"
        providers['frac'] = frac_status
        
        # 买卖挂单金额
        def positions_status():
            try:
                n_buy = getattr(strategy_instance, 'N_buy', 0)
                n_sell = getattr(strategy_instance, 'N_sell', 0)
                return f"${n_buy:.0f}/${n_sell:.0f}"
            except:
                return "N/A"
        providers['pos'] = positions_status
        
        # 成交频率
        def fills_status():
            try:
                fills = getattr(strategy_instance, 'fills_1m', 0)
                return f"{fills}/min"
            except:
                return "0/min"
        providers['fills'] = fills_status
        
        return providers
        
    @staticmethod
    def create_system_status(awg_instance, shadow_instance, mirror_instance) -> Dict[str, Callable]:
        """创建系统状态提供者"""
        providers = {}
        
        # AWG状态
        def awg_status():
            try:
                if awg_instance:
                    return awg_instance.get_usage_stats()
                return "OFF"
            except:
                return "ERR"
        providers['AWG'] = awg_status
        
        # Shadow Balance状态
        def shadow_status():
            try:
                if shadow_instance:
                    return shadow_instance.get_summary()
                return "OFF"
            except:
                return "ERR"
        providers['Shadow'] = shadow_status
        
        # Order Mirror状态
        def mirror_status():
            try:
                if mirror_instance:
                    return mirror_instance.get_summary()
                return "OFF"
            except:
                return "ERR"
        providers['Mirror'] = mirror_status
        
        return providers


# 全局状态监控器实例
_status_monitor = None


def get_status_monitor(interval: float = 5.0, log_to_console: bool = True) -> StatusLineMonitor:
    """获取全局状态监控器"""
    global _status_monitor
    
    if _status_monitor is None:
        _status_monitor = StatusLineMonitor(interval, log_to_console)
        
    return _status_monitor


def setup_standard_monitoring(strategy_instance, awg_instance=None, 
                            shadow_instance=None, mirror_instance=None,
                            interval: float = 5.0) -> StatusLineMonitor:
    """
    设置标准监控
    
    Args:
        strategy_instance: 策略实例
        awg_instance: AWG实例
        shadow_instance: Shadow Balance实例
        mirror_instance: Order Mirror实例
        interval: 监控间隔
        
    Returns:
        StatusLineMonitor实例
    """
    monitor = get_status_monitor(interval)
    
    # 注册交易状态提供者
    trading_providers = StandardStatusProviders.create_trading_status(strategy_instance)
    for name, provider in trading_providers.items():
        monitor.register_provider(name, provider)
        
    # 注册系统状态提供者
    system_providers = StandardStatusProviders.create_system_status(
        awg_instance, shadow_instance, mirror_instance
    )
    for name, provider in system_providers.items():
        monitor.register_provider(name, provider)
        
    return monitor


if __name__ == "__main__":
    # 测试
    class MockStrategy:
        def __init__(self):
            self.util_onbook = 0.85
            self.usdt_frac_eff = 0.52
            self.N_buy = 250.5
            self.N_sell = 320.8
            self.fills_1m = 3
            
    async def test():
        # 创建模拟策略
        strategy = MockStrategy()
        
        # 创建状态监控器
        monitor = StatusLineMonitor(interval=1.0)
        
        # 注册状态提供者
        providers = StandardStatusProviders.create_trading_status(strategy)
        for name, provider in providers.items():
            monitor.register_provider(name, provider)
            
        # 运行5秒
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(5)
        monitor.stop()
        await task
        
        print("\n历史状态:")
        for record in monitor.get_history(3):
            print(f"  {record['status']}")
            
# 全局实例
_global_status_line_monitor = None

def get_status_line_monitor(**kwargs):
    """获取Status Line Monitor实例（单例）"""
    global _global_status_line_monitor
    
    if _global_status_line_monitor is None:
        update_interval = kwargs.get('update_interval', 1.0)
        
        _global_status_line_monitor = StatusLineMonitor(
            interval=update_interval
        )
        logger.info(f"[StatusLineMonitor] 创建新实例: update_interval={update_interval}")
    
    return _global_status_line_monitor

def reset_status_line_monitor():
    """重置Status Line Monitor实例（测试用）"""
    global _global_status_line_monitor
    _global_status_line_monitor = None

if __name__ == "__main__":
    asyncio.run(test())