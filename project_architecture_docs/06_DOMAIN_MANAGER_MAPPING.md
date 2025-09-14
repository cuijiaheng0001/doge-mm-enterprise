# 🎯 Domain Manager与CONCISE架构模块映射

## 📊 8个Domain Manager职责分配

### 1️⃣ ReferenceManager (品种主数据域)
**负责Layer 0品种主数据**

包含模块：
- `0.0 InstrumentMaster` - 品种主数据服务
- 交易规则管理
- 价格精度管理
- 费率层级管理
- 交易状态管理

---

### 2️⃣ MarketDataManager (市场数据域)
**负责Layer 1市场数据子系统**

包含模块：
- `1.0.1 MultiSourceDataGovernance` - 多源数据治理
- `1.0.2 IncrementalStreamProcessor` - 增量流处理
- `1.0.3 FeedArbiter` - 数据源仲裁
- `1.0.4 Sequencer` - 序列化器
- `1.0.5 GapFillEngine` - 缺口填充
- `1.0.6 StaleTick Gate` - 陈旧数据过滤
- `1.0.7 L3 BookBuilder` - L3订单簿构建
- `1.1 DualActiveMarketData` - 双活市场数据

---

### 3️⃣ AccountStateManager (账户状态域)
**负责Layer 1账户数据与状态管理**

包含模块：
- `1.2 UserDataStream` - 用户数据流
- `1.3 DropCopyIngestor` - 独立抄送引擎
- `1.4 StateReconciler` - 状态和解协调器
- `0.4 SessionStateManager` - 会话状态管理

---

### 4️⃣ RiskManager (风险管理域)
**负责Layer 2风控系统**

包含模块：
- `2.0.1 CentralizedRiskServer` - 集中式风控服务器
- `2.0.2 TokenBucketLimiter` - 令牌桶限流
- `2.0.3 SelfTradePreventionEngine` - 自成交防控
- `2.0.4 PreTradeGuardrail` - 前置合规栅格
- `2.1 PessimisticReservationModel` - 悲观预扣模型
- `2.2 SSOTReservationClosedLoop` - SSOT预留闭环
- `2.3 InstitutionalEventLedger` - 机构级事件账本

---

### 5️⃣ PricingManager (定价域)
**负责Layer 3定价决策**

包含模块：
- `3.1 LiquidityEnvelope` - 流动性包络
- `3.2 QuotePricingService` - 智能定价引擎
- `3.4 ThreeDomainInventorySystem` - 三域库存系统
- `5.1 ToxicityMonitor` - 毒性监控器
- `5.2 QuoteQualityService` - 报价质量服务

---

### 6️⃣ ExecutionManager (执行域)
**负责Layer 3订单编排与Layer 4执行**

包含模块：
- `3.3 OrderOrchestrator` - 订单协调引擎
- `4.1 IBE` - 智能批量执行器
- `4.2 EmergencyKillSwitch` - 紧急停止开关
- `4.3 MillisecondResponseSystem` - 毫秒响应系统
- `4.4 APIRateLimiter` - API限流管理器
- `4.5 CoreTradeConnector` - 核心交易连接器

---

### 7️⃣ HedgingManager (对冲域)
**负责Layer 7完整对冲引擎**

包含模块：
- `7.1 DeltaBus` - Delta事件总线
- `7.2 PositionBook` - 仓位账本
- `7.3 ModeController` - 模式控制器
- `7.4 PassivePlanner` - 被动腿计划器
- `7.5 ActivePlanner` - 主动腿计划器
- `7.6 HedgeRouter` - 对冲路由器
- `7.7 HedgeGovernor` - 对冲治理器
- `7.8 HedgeService` - 对冲服务主控

---

### 8️⃣ QualityOpsManager (质量运维域)
**负责Layer 5质量分析、Layer 6监控、Layer 8生产工程化**

包含模块：

**质量分析 (Layer 5):**
- `5.3 MarketQualityDashboard` - 做市质量仪表板

**监控 (Layer 6):**
- `6.1 ObservabilityDashboard` - 可观测性仪表板

**生产工程化 (Layer 8):**
- `8.1 ParameterServer` - 参数服务器
- `8.2 FeatureConsistency` - 特征一致性
- `8.3 ReplaySimulator` - 重放仿真器
- `8.4 ShadowTrading` - 影子交易
- `8.5 CanaryDeployment` - 金丝雀部署
- `8.6 EventSourcingEngine` - 事件溯源引擎

---

## 🔧 基础设施层模块（不属于Domain Manager）

这些模块作为基础设施，被所有Domain Manager共享：

### Layer -1: 系统调优基线层
- `-1.1 NetworkHostTuningBaseline` - 网络主机调优
- `-1.2 PTSyncService` - 精密时间同步

### Layer 0: 基础设施服务
- `0.1 SigningService` - API签名服务
- `0.2 ChangeGuard` - 双人复核服务
- `0.3 LightweightFailoverManager` - 故障切换管理
- `0.5 TimeAuthority` - 统一时间权威
- `0.6 LatencyTracker` - 延迟追踪器

---

## ✅ 完整性检查

### CONCISE架构54个模块分配情况：

| Domain Manager | 模块数量 | 占比 |
|---------------|---------|------|
| ReferenceManager | 1 | 1.9% |
| MarketDataManager | 8 | 14.8% |
| AccountStateManager | 4 | 7.4% |
| RiskManager | 7 | 13.0% |
| PricingManager | 5 | 9.3% |
| ExecutionManager | 6 | 11.1% |
| HedgingManager | 8 | 14.8% |
| QualityOpsManager | 8 | 14.8% |
| 基础设施层 | 7 | 13.0% |
| **总计** | **54** | **100%** |

### 验证结果：
- ✅ 所有54个CONCISE模块都已分配
- ✅ 没有遗漏的模块
- ✅ 每个模块都有明确的归属
- ✅ 8个Domain Manager职责清晰
- ✅ 基础设施层独立支撑

---

## 🎯 Domain Manager接口示例

```python
# 极薄主循环调用Domain Manager
class Engine:
    def __init__(self, refs, mkt, acct, risk, pricing, execu, hedge, ops):
        """初始化8个域管理器"""
        self.refs = refs      # ReferenceManager
        self.mkt = mkt        # MarketDataManager
        self.acct = acct      # AccountStateManager
        self.risk = risk      # RiskManager
        self.pricing = pricing # PricingManager
        self.execu = execu    # ExecutionManager
        self.hedge = hedge    # HedgingManager
        self.ops = ops        # QualityOpsManager
```

---

*映射版本: 1.0.0*
*更新时间: 2025-01-19*