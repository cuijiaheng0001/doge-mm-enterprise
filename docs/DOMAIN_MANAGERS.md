# 🏛️ Enterprise架构 - 域管理器模式（Domain Managers Pattern）

> 世界级做市商架构：8域分离 + 极薄主循环 + 事件驱动

## 📐 核心设计理念

1. **域边界固定**：永远保持8个域，避免架构膨胀
2. **接口驱动**：主循环只依赖Ports，不知道实现细节
3. **垂直切片**：每个域可独立开发、测试、部署
4. **事件解耦**：域之间通过事件总线通信，无直接依赖

---

## 🎯 域控制器（Domain Managers）划分

> 每个管理器对外只暴露 Ports（接口）；对内可组合多个子组件。括号内为架构文件中的对应组件。

### 1️⃣ ReferenceManager（主数据与时间）

**职责**：统一品种/规则/费率与精度（InstrumentMaster）、统一时间与延迟度量（TimeAuthority/LatencyTracker/PTSync）。

**关键 Ports**：
```python
class ReferenceManagerPorts:
    def get_symbol_rules(symbol: str) -> SymbolRules
    def get_tick_size(symbol: str) -> Decimal
    def get_min_notional(symbol: str) -> Decimal
    def get_fee_schedule(symbol: str, user_level: str) -> FeeInfo
    def now() -> Timestamp  # 统一时间源
    def latency_metrics() -> LatencyStats
    def validate_trading_hours(symbol: str) -> bool
```

**内部组件**：
- InstrumentMaster：品种主数据管理
- TimeAuthority：软件层时间治理
- PTSyncService：硬件时间同步
- LatencyTracker：延迟度量

---

### 2️⃣ MarketDataManager（市场数据域）

**职责**：多源治理、序列化、缺口填充、陈旧门控、L3构簿、双活快照（MultiSource/Sequencer/GapFill/StaleTick/L3/DualActive）。

**关键 Ports**：
```python
class MarketDataManagerPorts:
    def get_snapshot(symbol: str) -> MarketSnapshot
    def subscribe_trades(symbol: str, callback: Callable) -> Subscription
    def get_l3_book(symbol: str) -> OrderBook
    def get_market_status() -> MarketStatus
    def validate_data_quality() -> QualityMetrics
```

**内部组件**：
- MultiSourceGovernor：多源数据治理
- Sequencer：序列化保证
- GapFillService：缺口填充
- L3BookBuilder：订单簿构建
- DualActiveSnapshot：双活快照

---

### 3️⃣ AccountStateManager（账户与状态一致性）

**职责**：UDS主通道、独立Drop-Copy、状态和解、事件账本（UserDataStream/DropCopy/StateReconciler/EventLedger）。

**关键 Ports**：
```python
class AccountStateManagerPorts:
    def balances() -> Dict[str, Balance]
    def open_orders() -> List[Order]
    def on_fill(fill: Fill) -> None
    def reconcile_now() -> ReconcileResult
    def get_position(symbol: str) -> Position
    def get_pnl() -> PnLReport
```

**内部组件**：
- UserDataStream：主账户数据流
- DropCopyService：独立验证通道
- StateReconciler：状态和解器
- EventLedger：事件账本

---

### 4️⃣ RiskManager（集中风控与预扣闭环）

**职责**：集中式风控（四维限额、STP、TokenBucket、Pre-Trade Guardrail）、悲观预扣与SSOT状态机。

**关键 Ports**：
```python
class RiskManagerPorts:
    def pretrade_check(order: Order) -> CheckResult
    def reserve(amount: Decimal, symbol: str) -> ReservationToken
    def confirm(token: ReservationToken) -> None
    def release(token: ReservationToken) -> None
    def get_risk_metrics() -> RiskMetrics
    def emergency_stop() -> None
```

**内部组件**：
- CentralizedRiskServer：集中风控服务
- SSOTReservationSystem：预扣闭环系统
- PreTradeGuardrail：交易前守护
- TokenBucketLimiter：流量控制
- STPEngine：自成交预防

---

### 5️⃣ PricingManager（定价与库存）

**职责**：定价（QuotePricingService）、库存三时域、资金信封（LiquidityEnvelope/Inventory）。

**关键 Ports**：
```python
class PricingManagerPorts:
    def calculate_quotes(snapshot: MarketSnapshot) -> QuoteSet
    def skew_params() -> SkewParameters
    def allocation() -> FundAllocation
    def get_inventory_ratio() -> InventoryRatio
    def suggest_rebalance() -> RebalanceHint
```

**内部组件**：
- QuotePricingService：智能定价引擎
- ThreeDomainInventory：三时域库存管理
- LiquidityEnvelope：资金包络管理
- VolatilityEstimator：波动率估算

---

### 6️⃣ ExecutionManager（下单编排与执行）

**职责**：订单生成与拆单/撤单策略（OrderOrchestrator）、批量执行与动态TTL（IBE）、配额与杀开关（APIRateLimiter/KillSwitch）、交易通道（CoreTradeConnector）。

**关键 Ports**：
```python
class ExecutionManagerPorts:
    def generate_orders(quotes: QuoteSet) -> List[Order]
    def execute_batch(orders: List[Order]) -> ExecutionResult
    def cancel_batch(order_ids: List[str]) -> CancelResult
    def get_active_orders() -> List[Order]
    def trigger_kill_switch() -> None
```

**内部组件**：
- OrderOrchestrator：订单协调器
- IBE：智能批量执行器
- APIRateLimiter：限流管理
- EmergencyKillSwitch：紧急停止
- CoreTradeConnector：交易连接器

---

### 7️⃣ HedgingManager（对冲域）

**职责**：Delta事件、模式控制、被动/主动腿计划、路由与执行、预算/成本治理（FAHE套件）。

**关键 Ports**：
```python
class HedgingManagerPorts:
    def on_delta(delta: Decimal) -> HedgePlan
    def plan_legs(imbalance: Imbalance) -> List[Leg]
    def route_leg(leg: Leg) -> RoutingDecision
    def get_hedge_status() -> HedgeStatus
    def get_hedge_cost() -> HedgeCost
```

**内部组件**：
- FAHECore：快速激进对冲引擎
- PassiveAggressive：被动/主动腿策略
- MultiVenueRouter：多市场路由
- HedgeBudgetGovernor：对冲预算管理

---

### 8️⃣ QualityOpsManager（做市质量与工程化）

**职责**：毒性/质量评估、可视化、参数服务、影子/金丝雀、事件溯源/回放、可观测性（Toxicity/QuoteQuality/Dashboard/ParameterServer/Shadow/Canary/EventSourcing/Replay/Observability）。

**关键 Ports**：
```python
class QualityOpsManagerPorts:
    def quality_report() -> QualityMetrics
    def suggest_params() -> OptimalParameters
    def set_traffic_ratio(shadow: float, canary: float) -> None
    def replay(events: List[Event]) -> ReplayResult
    def get_toxicity_score() -> ToxicityScore
    def dashboard_metrics() -> DashboardData
```

**内部组件**：
- ToxicityMonitor：毒性监控
- QuoteQualityService：报价质量评估
- ParameterServer：参数优化服务
- ShadowTrading：影子交易
- CanaryDeployment：金丝雀部署
- EventSourcingEngine：事件溯源
- ObservabilityDashboard：可观测性仪表板

---

## 🎭 主循环（Orchestrator）最小形态

> 极简设计：20行代码实现完整交易循环，主程序永远不会"越写越厚"

```python
class Engine:
    def __init__(self, refs, mkt, acct, risk, pricing, execu, hedge, ops):
        """初始化8个域管理器"""
        self.refs, self.mkt, self.acct = refs, mkt, acct
        self.risk, self.pricing, self.execu = risk, pricing, execu
        self.hedge, self.ops = hedge, ops

    def on_market_tick(self, tick):
        """市场数据事件驱动"""
        snap = self.mkt.get_snapshot()
        quotes = self.pricing.calculate_quotes(snap)          # 只定价
        orders = self.execu.generate_orders(quotes)           # 只编排生成
        approved = [o for o in orders if self.risk.pretrade_check(o).approved]
        self.execu.execute_batch(approved)                    # 只执行

    def on_fill(self, fill):
        """成交事件驱动"""
        self.acct.reconcile_now()
        delta = self.hedge.calc_delta()                      # PositionBook/DeltaBus
        self.hedge.on_delta(delta)

    def on_timer(self):
        """定时器事件驱动"""
        self.ops.quality_report()
        if self.ops.should_kill():
            self.execu.kill_switch()
```

### 🌟 架构优势

1. **无环状依赖**：
   ```
   MarketData → Pricing → Execution → AccountState → Hedging → Quality
   ```
   数据单向流动，无循环依赖

2. **可替换性**：
   - 任何管理器都可独立回滚/替换
   - 例如：把定价从规则改为ML，不影响其他域
   - 支持灰度发布和A/B测试

3. **异常处置**：
   - `ops.should_kill()` 整合多维度熔断条件
   - P99延迟 > 10ms
   - 陈旧行情 > 1秒
   - 余额异常 > 5%
   - 统一通过EmergencyKillSwitch处理

### 📊 数据流示意

```
市场Tick → [MarketDataManager]
    ↓
快照Snapshot → [PricingManager]
    ↓
报价Quotes → [ExecutionManager + RiskManager]
    ↓
订单Orders → [交易所]
    ↓
成交Fill → [AccountStateManager]
    ↓
Delta偏差 → [HedgingManager]
    ↓
质量报告 → [QualityOpsManager]
```

### 🚀 生产级扩展

虽然核心只有20行，但可以无缝扩展：

```python
class ProductionEngine(Engine):
    """生产环境增强版"""

    def on_market_tick(self, tick):
        # 增加数据质量检查
        if not self.mkt.validate_tick(tick):
            return

        # 增加参数动态调整
        params = self.ops.get_dynamic_params()
        self.pricing.update_params(params)

        # 调用父类核心逻辑
        super().on_market_tick(tick)

    def on_error(self, error):
        """错误处理扩展"""
        self.ops.log_error(error)
        if error.is_critical():
            self.execu.kill_switch()
```

### ⚡ 性能特征

- **延迟**: 主循环开销 < 50μs
- **内存**: 主循环本身 < 1MB
- **CPU**: 单核即可运行主循环
- **扩展性**: 域内优化不影响主循环

这个极简主循环设计已经包含了世界级做市商的核心架构思想！

