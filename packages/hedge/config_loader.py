"""
配置加载器 - 从环境文件加载FAHE配置
"""

import os
from typing import Optional
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class FuturesAPIConfig:
    """永续合约API配置"""
    api_key: str
    api_secret: str
    testnet: bool = False
    

@dataclass
class FAHEConfig:
    """FAHE对冲系统配置"""
    # API配置
    api: FuturesAPIConfig
    
    # 对冲参数
    bandwidth: float = 150
    deadband: float = 40
    max_delta_error: float = 30
    
    # 预算参数
    fill_budget: int = 12
    reprice_budget: int = 12
    cancel_budget: int = 40
    target_usage_pct: float = 0.07
    safe_usage_pct: float = 0.15
    
    # 执行参数
    single_order_limit: float = 5000
    max_slippage_bps: float = 5
    
    # 模式控制
    mode_a0: float = 0.6
    mode_a1: float = 0.5
    mode_a2: float = 0.4
    mode_a3: float = 0.3
    mode_a4: float = 0.2
    
    # 监控参数
    heartbeat_interval: int = 5
    stats_interval: int = 30
    
    # 功能开关
    enabled: bool = False
    passive_enabled: bool = False
    active_enabled: bool = True


def load_futures_config(env_file: str = '/home/ubuntu/.env.futures') -> Optional[FAHEConfig]:
    """
    加载永续合约配置
    
    Args:
        env_file: 配置文件路径
    
    Returns:
        FAHE配置对象，如果未启用则返回None
    """
    # 加载环境文件
    if os.path.exists(env_file):
        load_dotenv(env_file, override=True)
    else:
        print(f"警告: 配置文件不存在 {env_file}")
        print("请运行: /home/ubuntu/setup_futures_api.sh 进行配置")
        return None
    
    # 检查是否启用
    if os.getenv('HEDGE_ENABLED', 'false').lower() != 'true':
        return None
    
    # 加载API配置
    api_key = os.getenv('FUTURES_API_KEY', '')
    api_secret = os.getenv('FUTURES_API_SECRET', '')
    
    if not api_key or not api_secret or api_key == 'YOUR_FUTURES_API_KEY_HERE':
        print("错误: 请先配置永续合约API密钥")
        print("运行: /home/ubuntu/setup_futures_api.sh")
        return None
    
    api_config = FuturesAPIConfig(
        api_key=api_key,
        api_secret=api_secret,
        testnet=os.getenv('FUTURES_TESTNET', 'false').lower() == 'true'
    )
    
    # 加载FAHE配置
    config = FAHEConfig(
        api=api_config,
        
        # 对冲参数
        bandwidth=float(os.getenv('HEDGE_BANDWIDTH', '150')),
        deadband=float(os.getenv('HEDGE_DEADBAND', '40')),
        max_delta_error=float(os.getenv('HEDGE_MAX_ERROR', '30')),
        
        # 预算参数
        fill_budget=int(os.getenv('HEDGE_FILL_BUDGET', '12')),
        reprice_budget=int(os.getenv('HEDGE_REPRICE_BUDGET', '12')),
        cancel_budget=int(os.getenv('HEDGE_CANCEL_BUDGET', '40')),
        target_usage_pct=float(os.getenv('HEDGE_TARGET_USAGE', '0.07')),
        safe_usage_pct=float(os.getenv('HEDGE_SAFE_USAGE', '0.15')),
        
        # 执行参数
        single_order_limit=float(os.getenv('HEDGE_SINGLE_ORDER_LIMIT', '5000')),
        max_slippage_bps=float(os.getenv('HEDGE_MAX_SLIPPAGE_BPS', '5')),
        
        # 模式控制
        mode_a0=float(os.getenv('HEDGE_MODE_A0', '0.6')),
        mode_a1=float(os.getenv('HEDGE_MODE_A1', '0.5')),
        mode_a2=float(os.getenv('HEDGE_MODE_A2', '0.4')),
        mode_a3=float(os.getenv('HEDGE_MODE_A3', '0.3')),
        mode_a4=float(os.getenv('HEDGE_MODE_A4', '0.2')),
        
        # 监控参数
        heartbeat_interval=int(os.getenv('HEDGE_HEARTBEAT_INTERVAL', '5')),
        stats_interval=int(os.getenv('HEDGE_STATS_INTERVAL', '30')),
        
        # 功能开关
        enabled=True,  # 已经通过HEDGE_ENABLED检查
        passive_enabled=os.getenv('HEDGE_PASSIVE_ENABLED', 'false').lower() == 'true',
        active_enabled=os.getenv('HEDGE_ACTIVE_ENABLED', 'true').lower() == 'true'
    )
    
    # 输出配置摘要
    print(f"✅ FAHE配置加载成功:")
    print(f"   - 网络: {'测试网' if config.api.testnet else '主网'}")
    print(f"   - 带宽/死区: {config.bandwidth}/{config.deadband} DOGE")
    print(f"   - 模式: {'Passive+Active' if config.passive_enabled else 'Active only'}")
    
    return config


def get_hedge_config_for_main() -> Optional[FAHEConfig]:
    """
    供main.py调用的配置加载函数
    
    Returns:
        FAHE配置对象，如果未启用则返回None
    """
    # 先尝试加载futures配置
    config = load_futures_config()
    
    # 如果没有futures配置，检查主.env文件
    if config is None:
        load_dotenv('/home/ubuntu/.env')
        if os.getenv('HEDGE_ENABLED', 'false').lower() == 'true':
            print("提示: 检测到HEDGE_ENABLED=true，但缺少永续合约配置")
            print("请运行: /home/ubuntu/setup_futures_api.sh 配置API")
    
    return config