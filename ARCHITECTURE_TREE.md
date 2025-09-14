# 🌳 Enterprise世界级架构树

## 📂 完整项目结构

```
doge-mm-enterprise/
│
├── 📚 project_architecture_docs/          # 架构文档中心
│   ├── README.md                          # 文档导航
│   ├── 01_MAIN_ARCHITECTURE_V10.md        # 主架构(58KB, 1934行)
│   ├── 02_CONCISE_ARCHITECTURE.md         # 简洁版(7KB, 54模块)
│   ├── 03_DOMAIN_MANAGERS.md              # 域管理器设计
│   ├── 04_PORTS_AND_DTOS.md               # 数据契约
│   ├── 05_DEVELOP_PROGRESS_PLAN.md        # S0-S8开发计划
│   ├── 06_DOMAIN_MANAGER_MAPPING.md       # 模块映射关系
│   ├── 07_ARCHITECTURE_SUMMARY.md         # 架构总结
│   ├── 08_CLEANED_REPORT.md               # 清理报告
│   └── 09_OLD_ARCHITECTURE.md             # 历史版本
│
├── 🎯 engine/                             # 核心引擎
│   ├── engine_core/
│   │   └── orchestrator.py                # 🔥 极薄主循环(20行)
│   │
│   ├── domains/                           # 8个域管理器
│   │   ├── reference/                     # 品种主数据域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # ReferenceManager
│   │   ├── market_data/                   # 市场数据域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # MarketDataManager
│   │   ├── account_state/                 # 账户状态域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # AccountStateManager
│   │   ├── risk/                          # 风险管理域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # RiskManager
│   │   ├── pricing/                       # 定价域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # PricingManager
│   │   ├── execution/                     # 执行域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # ExecutionManager
│   │   ├── hedging/                       # 对冲域
│   │   │   ├── __init__.py
│   │   │   └── manager.py                 # HedgingManager
│   │   └── ops/                           # 质量运维域
│   │       ├── __init__.py
│   │       └── manager.py                 # QualityOpsManager
│   │
│   ├── dto/
│   │   └── core_dtos.py                   # 5个核心DTO
│   │
│   ├── events/                            # 事件架构
│   ├── adapters/                          # 交易所适配器
│   ├── infra/                             # 基础设施
│   └── monitoring/                        # 监控系统
│
├── 🔧 layer_minus1_hardware/              # Layer -1: 系统调优基线层
│   ├── network_host_tuning_baseline.py    # -1.1 网络主机调优
│   └── ptp_sync_service.py                # -1.2 精密时间同步
│
├── 📋 layer0_reference/                   # Layer 0: 品种主数据层
│   └── instrument_master.py               # 0.0 品种主数据服务
│
├── 🔐 layer0_infrastructure/              # Layer 0.1-0.6: 基础设施层
│   ├── security/
│   │   ├── signing_service.py             # 0.1 API签名服务
│   │   └── change_guard.py                # 0.2 双人复核服务
│   ├── failover/
│   │   ├── lightweight_failover_manager.py # 0.3 故障切换管理
│   │   └── session_state_manager.py       # 0.4 会话状态管理
│   └── time/
│       ├── time_authority.py              # 0.5 统一时间权威
│       └── latency_tracker.py             # 0.6 延迟追踪器
│
├── 📊 layer1_data/                        # Layer 1: 数据层
│   ├── market/
│   │   └── dual_active_market_data.py     # 1.1 双活市场数据
│   ├── account/
│   │   └── user_data_stream.py            # 1.2 用户数据流
│   └── reconciliation/
│       ├── drop_copy_ingestor.py          # 1.3 独立抄送引擎
│       └── state_reconciler.py            # 1.4 状态和解协调器
│
├── 🛡️ layer2_risk/                       # Layer 2: 风控层
│   ├── server/
│   │   └── centralized_risk_server.py     # 2.0.1 集中式风控服务器
│   ├── reservation/
│   │   ├── pessimistic_reservation_model.py # 2.1 悲观预扣模型
│   │   └── ssot_reservation_closed_loop.py  # 2.2 SSOT预留闭环
│   └── ledger/
│       └── institutional_event_ledger.py  # 2.3 机构级事件账本
│
├── 🧠 layer3_decision/                    # Layer 3: 决策层
│   ├── pricing/
│   │   └── quote_pricing_service.py       # 3.2 智能定价引擎
│   ├── orchestration/
│   │   └── order_orchestrator.py          # 3.3 订单协调引擎
│   └── inventory/
│       ├── liquidity_envelope.py          # 3.1 流动性包络
│       └── three_domain_inventory_system.py # 3.4 三域库存系统
│
├── ⚡ layer4_execution/                   # Layer 4: 执行层
│   ├── batch/
│   │   └── ibe.py                         # 4.1 智能批量执行器
│   ├── response/
│   │   └── millisecond_response_system.py # 4.3 毫秒响应系统
│   ├── control/
│   │   ├── emergency_kill_switch.py       # 4.2 紧急停止开关
│   │   └── api_rate_limiter.py            # 4.4 API限流管理器
│   └── core_trade_connector.py            # 4.5 核心交易连接器
│
├── 📈 layer5_quality/                     # Layer 5: 做市质量分析层
│   ├── toxicity_monitor.py                # 5.1 毒性监控器
│   ├── quote_quality_service.py           # 5.2 报价质量服务
│   └── market_quality_dashboard.py        # 5.3 做市质量仪表板
│
├── 🔄 layer6_hedge/                       # Layer 6: 对冲引擎层
│   ├── delta_bus.py                       # 7.1 Delta事件总线
│   ├── position_book.py                   # 7.2 仓位账本
│   ├── mode_controller.py                 # 7.3 模式控制器
│   ├── planner_passive.py                 # 7.4 被动腿计划器
│   ├── planner_active.py                  # 7.5 主动腿计划器
│   ├── router.py                          # 7.6 对冲路由器
│   ├── governor.py                        # 7.7 对冲治理器
│   ├── hedge_service.py                   # 7.8 对冲服务主控
│   └── config_loader.py                   # 配置加载器
│
├── 🚀 layer7_production/                  # Layer 7: 生产工程化层
│   ├── parameter_server.py                # 8.1 参数服务器
│   ├── feature_consistency.py             # 8.2 特征一致性引擎
│   ├── replay_simulator.py                # 8.3 重放仿真器
│   ├── shadow_trading.py                  # 8.4 影子交易系统
│   ├── canary_deployment.py               # 8.5 金丝雀放量系统
│   └── event_sourcing_engine.py           # 8.6 事件溯源引擎
│
├── 📊 layer8_monitoring/                  # Layer 8: 监控层
│   └── observability_dashboard.py         # 6.1 可观测性仪表板
│
├── 🧪 tests/                              # 测试套件
│   ├── unit/                              # 单元测试
│   ├── integration/                       # 集成测试
│   ├── performance/                       # 性能测试
│   ├── market_scenarios/                  # 市场场景测试
│   ├── failure_modes/                     # 故障模式测试
│   └── compliance/                        # 合规测试
│
├── ⚙️ configs/                            # 配置管理
│   ├── dev/                               # 开发环境配置
│   ├── test/                              # 测试环境配置
│   └── prod/                              # 生产环境配置
│
├── 📜 scripts/                            # 工具脚本
│   ├── deploy/                            # 部署脚本
│   ├── monitor/                           # 监控脚本
│   ├── tools/                             # 工具脚本
│   └── stress_test/                       # 压力测试脚本
│
├── 📝 docs/                               # 技术文档
│
├── 🧹 cleanup_to_concise.sh              # 清理脚本
├── 🔄 reorganize_to_v10.sh               # 重组脚本
├── 🔨 create_missing_modules.sh          # 创建模块脚本
├── 📄 README.md                           # 项目说明
└── 🌳 ARCHITECTURE_TREE.md               # 本文档

```

## 📊 架构层级统计

| 层级 | 名称 | 模块数 | 主要职责 |
|------|------|--------|----------|
| **Layer -1** | 系统调优基线层 | 2 | 硬件与网络基础优化 |
| **Layer 0** | 基础设施层 | 7 | 品种主数据、安全、容灾、时间 |
| **Layer 1** | 数据层 | 11 | 市场数据、账户数据、状态和解 |
| **Layer 2** | 风控层 | 7 | 集中式风控、预扣、事件账本 |
| **Layer 3** | 决策层 | 4 | 定价、订单编排、库存管理 |
| **Layer 4** | 执行层 | 5 | IBE批量执行、限流、连接器 |
| **Layer 5** | 质量分析层 | 3 | 毒性监控、质量评估 |
| **Layer 6** | 监控层 | 1 | 系统健康监控 |
| **Layer 7** | 对冲引擎层 | 8 | Delta管理、对冲执行 |
| **Layer 8** | 生产工程化层 | 6 | 参数管理、影子交易、金丝雀 |

### 🎯 核心组件
- **极薄主循环**: 20行代码
- **域管理器**: 8个
- **核心模块**: 54个
- **核心DTO**: 5个

## 🔑 关键特性

### 1. 极薄主循环架构
```python
# engine_core/orchestrator.py - 仅20行核心代码
class Engine:
    def on_market_tick(self, tick)
    def on_fill(self, fill)
    def on_timer(self)
```

### 2. 八大域管理器
- ReferenceManager - 品种主数据
- MarketDataManager - 市场数据
- AccountStateManager - 账户状态
- RiskManager - 风险管理
- PricingManager - 定价决策
- ExecutionManager - 执行管理
- HedgingManager - 对冲管理
- QualityOpsManager - 质量运维

### 3. 清晰的层级架构
从Layer -1（硬件优化）到Layer 8（生产工程化），每层职责明确，无功能重叠。

---

*架构版本: V10*
*模块总数: 54个*
*更新时间: 2025-01-19*