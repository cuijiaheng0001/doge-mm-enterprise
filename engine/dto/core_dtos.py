"""
核心数据传输对象（DTOs）
所有域之间的通信契约
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List, Dict


@dataclass(frozen=True)
class MarketSnapshot:
    """市场数据快照 - 定价依据"""
    # 必需字段（MVP版本）
    symbol: str
    bid: Decimal
    ask: Decimal
    spread: Decimal
    timestamp: int

    # 深度信息（Phase 1扩展）
    bid_depth: Optional[List['PriceLevel']] = None
    ask_depth: Optional[List['PriceLevel']] = None

    # 高级特征（Phase 2扩展）
    microprice: Optional[Decimal] = None
    queue_info: Optional['QueuePosition'] = None
    imbalance: Optional[Decimal] = None

    # 质量标记
    is_stale: bool = False
    confidence: float = 1.0
    source: str = "primary"


@dataclass(frozen=True)
class QuoteSet:
    """定价输出 - 报价决策"""
    # 核心报价
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal

    # 定价元数据
    confidence: float
    skew_meta: Optional['SkewInfo'] = None

    # 分层报价（可选）
    layers: Optional[List['QuoteLayer']] = None


@dataclass(frozen=True)
class OrderPlan:
    """订单执行计划"""
    orders: List['PlannedOrder']
    strategy_id: str
    generation_time: int


@dataclass(frozen=True)
class PlannedOrder:
    """单个计划订单"""
    # 核心字段
    side: str  # "BUY" | "SELL"
    price: Decimal
    quantity: Decimal
    symbol: str

    # 执行控制
    time_in_force: str = "GTC"
    order_type: str = "LIMIT"
    layer: int = 0

    # 追踪标识
    client_order_id: str
    strategy_tag: Optional[str] = None

    # TTL管理
    ttl_ms: Optional[int] = None
    post_only: bool = True


@dataclass(frozen=True)
class ExecutionReport:
    """执行结果报告"""
    # 执行结果
    status: str  # "ACK" | "FILLED" | "PARTIAL" | "CANCELLED" | "REJECTED"
    order_id: str
    client_order_id: str

    # 成交信息（如果有）
    fill: Optional['FillInfo'] = None

    # 撤单/拒绝信息
    cancel_reason: Optional[str] = None
    reject_reason: Optional[str] = None

    # 时间戳
    exchange_time: Optional[int] = None
    local_time: int


@dataclass(frozen=True)
class RiskVerdict:
    """风控审批结果"""
    # 核心裁决
    approved: bool

    # 拒绝原因（如果拒绝）
    rejection_reason: Optional[str] = None
    rejection_code: Optional[str] = None

    # 限额状态
    limits_state: Optional['LimitsSnapshot'] = None