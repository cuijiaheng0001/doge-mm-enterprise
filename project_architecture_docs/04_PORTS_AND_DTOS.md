# 🔌 Enterprise架构 - 接口与数据契约（Ports & Data Contracts）

> 统一接口定义：5个核心DTO + 3个补充DTO，覆盖全部交易流程

## 📐 设计原则

1. **最小化接口**：只定义必要的数据传输对象
2. **向后兼容**：新增字段用Optional，不改现有字段含义
3. **类型安全**：所有字段强类型定义
4. **不可变性**：DTO创建后不可修改，保证数据一致性

---

## 🎯 五类核心DTO

### 1️⃣ MarketSnapshot（市场快照）

**来源**：MarketDataManager
**消费者**：PricingManager

```python
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
    bid_depth: Optional[List[PriceLevel]] = None
    ask_depth: Optional[List[PriceLevel]] = None

    # 高级特征（Phase 2扩展）
    microprice: Optional[Decimal] = None          # 微观价格
    queue_info: Optional[QueuePosition] = None    # L3队列信息
    imbalance: Optional[Decimal] = None          # 买卖失衡度

    # 质量标记（Phase 3扩展）
    is_stale: bool = False
    confidence: float = 1.0
    source: str = "primary"
```

### 2️⃣ QuoteSet（报价集合）

**来源**：PricingManager
**消费者**：ExecutionManager, RiskManager

```python
@dataclass(frozen=True)
class QuoteSet:
    """定价输出 - 报价决策"""
    # 核心报价
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal

    # 定价元数据
    confidence: float                    # 0.0-1.0 置信度
    skew_meta: Optional[SkewInfo] = None # 偏斜信息

    # 分层报价（可选）
    layers: Optional[List[QuoteLayer]] = None

    # 定价参数（用于审计）
    spread_bps: Optional[float] = None
    target_position: Optional[Decimal] = None

@dataclass(frozen=True)
class SkewInfo:
    """库存偏斜元信息"""
    inventory_ratio: float
    skew_direction: str  # "buy_heavy" | "sell_heavy" | "neutral"
    adjustment_factor: float
```

### 3️⃣ OrderPlan（订单计划）

**来源**：ExecutionManager
**消费者**：RiskManager, CoreTradeConnector

```python
@dataclass(frozen=True)
class OrderPlan:
    """订单执行计划"""
    orders: List[PlannedOrder]
    strategy_id: str
    generation_time: int

@dataclass(frozen=True)
class PlannedOrder:
    """单个计划订单"""
    # 核心字段
    side: str          # "BUY" | "SELL"
    price: Decimal
    quantity: Decimal
    symbol: str

    # 执行控制
    time_in_force: str = "GTC"    # GTC | IOC | FOK | GTX
    order_type: str = "LIMIT"     # LIMIT | LIMIT_MAKER
    layer: int = 0                 # 0=L0, 1=L1, etc

    # 追踪标识
    client_order_id: str
    strategy_tag: Optional[str] = None

    # TTL管理
    ttl_ms: Optional[int] = None
    post_only: bool = True
```

### 4️⃣ ExecutionReport（执行报告）

**来源**：ExecutionManager/CoreTradeConnector
**消费者**：AccountStateManager, HedgingManager

```python
@dataclass(frozen=True)
class ExecutionReport:
    """执行结果报告"""
    # 执行结果
    status: str                    # "ACK" | "FILLED" | "PARTIAL" | "CANCELLED" | "REJECTED"
    order_id: str
    client_order_id: str

    # 成交信息（如果有）
    fill: Optional[FillInfo] = None

    # 撤单/拒绝信息
    cancel_reason: Optional[str] = None
    reject_reason: Optional[str] = None

    # 性能指标
    latencies: Optional[LatencyBreakdown] = None

    # 时间戳
    exchange_time: Optional[int] = None
    local_time: int

@dataclass(frozen=True)
class FillInfo:
    """成交详情"""
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    is_maker: bool
    trade_id: str

@dataclass(frozen=True)
class LatencyBreakdown:
    """延迟分解"""
    total_ms: float
    network_ms: Optional[float] = None
    exchange_ms: Optional[float] = None
    internal_ms: Optional[float] = None
```

### 5️⃣ RiskVerdict（风控裁决）

**来源**：RiskManager
**消费者**：ExecutionManager

```python
@dataclass(frozen=True)
class RiskVerdict:
    """风控审批结果"""
    # 核心裁决
    approved: bool

    # 拒绝原因（如果拒绝）
    rejection_reason: Optional[str] = None
    rejection_code: Optional[str] = None

    # 限额状态
    limits_state: Optional[LimitsSnapshot] = None

    # 建议调整（软限制）
    suggested_adjustment: Optional[RiskAdjustment] = None

    # 审批耗时
    check_latency_us: Optional[int] = None

@dataclass(frozen=True)
class LimitsSnapshot:
    """限额快照"""
    position_used: Decimal
    position_limit: Decimal
    notional_used: Decimal
    notional_limit: Decimal
    order_count_used: int
    order_count_limit: int

@dataclass(frozen=True)
class RiskAdjustment:
    """风控建议调整"""
    reduce_size_pct: Optional[float] = None
    widen_spread_bps: Optional[float] = None
    pause_side: Optional[str] = None  # "BUY" | "SELL"
```

---

## 🔧 三个补充DTO（建议添加）

### 6️⃣ PositionState（仓位状态）

**来源**：AccountStateManager
**消费者**：HedgingManager, PricingManager

```python
@dataclass(frozen=True)
class PositionState:
    """统一仓位状态"""
    symbol: str

    # 现货仓位
    spot_position: Decimal
    spot_notional: Decimal

    # 期货仓位（如果有）
    futures_position: Optional[Decimal] = None
    futures_notional: Optional[Decimal] = None

    # 净Delta
    net_delta: Decimal
    net_delta_usd: Decimal

    # 仓位指标
    inventory_ratio: float
    pnl_unrealized: Optional[Decimal] = None
    pnl_realized: Optional[Decimal] = None

    # 更新时间
    last_update: int
    reconciled: bool = True
```

### 7️⃣ HedgeCommand（对冲指令）

**来源**：HedgingManager
**消费者**：ExecutionManager（期货执行）

```python
@dataclass(frozen=True)
class HedgeCommand:
    """对冲执行指令"""
    # 对冲目标
    target_delta: Decimal
    current_delta: Decimal
    hedge_size: Decimal

    # 执行计划
    legs: List[HedgeLeg]
    urgency: str  # "PASSIVE" | "NORMAL" | "AGGRESSIVE" | "PANIC"

    # 成本预算
    max_cost_bps: Optional[float] = None
    estimated_cost_bps: Optional[float] = None

@dataclass(frozen=True)
class HedgeLeg:
    """对冲腿"""
    venue: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str  # "LIMIT" | "MARKET"
    limit_price: Optional[Decimal] = None
```

### 8️⃣ SystemHealth（系统健康度）

**来源**：QualityOpsManager
**消费者**：所有Manager（用于降级决策）

```python
@dataclass(frozen=True)
class SystemHealth:
    """系统健康状态"""
    # 整体状态
    overall_status: str  # "HEALTHY" | "DEGRADED" | "CRITICAL"

    # 分项指标
    latency_p99_ms: float
    maker_rate: float
    error_rate: float
    stale_data_rate: float

    # 各域健康度
    domain_health: Dict[str, DomainHealth]

    # 建议动作
    should_reduce_rate: bool = False
    should_widen_spread: bool = False
    should_kill_switch: bool = False

    # 采样信息
    sample_period_ms: int
    sample_time: int

@dataclass(frozen=True)
class DomainHealth:
    """单域健康度"""
    domain: str
    status: str  # "UP" | "DEGRADED" | "DOWN"
    latency_ms: Optional[float] = None
    error_count: int = 0
    last_success: Optional[int] = None
```

---

## 📊 数据流矩阵

| DTO | 生产者 | 消费者 | 触发时机 |
|-----|--------|--------|----------|
| MarketSnapshot | MarketDataManager | PricingManager | 每个tick |
| QuoteSet | PricingManager | ExecutionManager, RiskManager | 计算完成后 |
| OrderPlan | ExecutionManager | RiskManager, Connector | 订单生成后 |
| ExecutionReport | Connector/Exchange | AccountStateManager, HedgingManager | 订单状态变化 |
| RiskVerdict | RiskManager | ExecutionManager | 风控检查后 |
| PositionState | AccountStateManager | HedgingManager, PricingManager | 仓位变化后 |
| HedgeCommand | HedgingManager | ExecutionManager | Delta超阈值 |
| SystemHealth | QualityOpsManager | All Managers | 定时/事件触发 |

---

## 🔒 版本控制策略

### 字段扩展规则
1. **只增不改**：新字段必须是Optional
2. **语义不变**：已有字段含义永不改变
3. **版本标记**：在注释中标注添加版本

### 示例：扩展MarketSnapshot
```python
class MarketSnapshot:
    # v1.0 - 基础字段
    bid: Decimal
    ask: Decimal

    # v1.1 - 添加深度
    bid_depth: Optional[List[PriceLevel]] = None

    # v1.2 - 添加L3特征
    queue_info: Optional[QueuePosition] = None

    # v2.0 - 添加毒性评分（不影响v1用户）
    toxicity_score: Optional[float] = None
```

---

## 🎯 实施建议

### Phase 1: MVP（核心5个DTO）
- MarketSnapshot（最简版）
- QuoteSet（基础报价）
- OrderPlan（单层订单）
- ExecutionReport（基础执行）
- RiskVerdict（是/否判定）

### Phase 2: 增强（添加可选字段）
- MarketSnapshot + L3信息
- QuoteSet + 多层报价
- ExecutionReport + 延迟分解

### Phase 3: 完整（添加补充DTO）
- PositionState（仓位管理）
- HedgeCommand（对冲协调）
- SystemHealth（健康监控）

这套接口设计可以支撑从MVP到生产级系统的平滑演进！