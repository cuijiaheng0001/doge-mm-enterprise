# 🏢 DOGE Market Maker Enterprise - 世界级做市商架构

> 8域分离 + 极薄主循环 + 事件驱动 = 无限扩展性

## 🏗️ 仓库结构

```
doge-mm-enterprise/
├── engine/                      # 核心引擎
│   ├── engine_core/
│   │   └── orchestrator.py      # 极薄主循环 (20行)
│   ├── domains/                 # 8个域管理器
│   │   ├── reference/           # 品种主数据域
│   │   ├── market_data/         # 市场数据域
│   │   ├── account_state/       # 账户状态域
│   │   ├── risk/                # 风险管理域
│   │   ├── pricing/             # 定价域
│   │   ├── execution/           # 执行域
│   │   ├── hedging/             # 对冲域
│   │   └── ops/                 # 运维质量域
│   └── dto/
│       └── core_dtos.py         # 核心数据传输对象
│
├── packages/                    # 功能包
│   ├── connectors/              # 连接器
│   │   ├── core_trade_connector.py  # 精简交易连接器(5个方法)
│   │   ├── user_stream.py          # 用户数据流
│   │   └── perp_binance.py         # 永续合约连接
│   ├── risk/                    # 风控组件
│   │   ├── pessimistic_reservation_model.py
│   │   ├── institutional_event_ledger.py
│   │   └── budget_governor.py
│   ├── exec/                    # 执行组件
│   │   ├── intelligent_batch_executor.py  # IBE批量执行
│   │   ├── millisecond_response_system.py # 毫秒响应
│   │   └── three_domain_inventory_system.py
│   ├── hedge/                   # 对冲组件
│   │   ├── delta_bus.py        # Delta事件总线
│   │   ├── position_book.py    # 仓位账本
│   │   └── hedge_service.py    # 对冲主控
│   └── utils/                   # 工具组件
│       ├── dual_active_market_data.py
│       └── observability_dashboard.py
│
├── project_architecture_docs/   # 架构文档
│   ├── 01_MAIN_ARCHITECTURE_V10.md
│   ├── 02_CONCISE_ARCHITECTURE.md
│   ├── 03_DOMAIN_MANAGERS.md
│   ├── 04_PORTS_AND_DTOS.md
│   ├── 05_DEVELOP_PROGRESS_PLAN.md
│   ├── 06_DOMAIN_MANAGER_MAPPING.md
│   └── 07_DIRECTORY_STRUCTURE.md
│
├── configs/                     # 配置文件
├── scripts/                     # 脚本工具
├── tests/                       # 测试套件
└── docs/                        # 文档
```

## 🚀 快速开始

### 1. 环境准备
```bash
# Python虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置检查
python scripts/check_config.py
```

### 2. 开发模式启动
```bash
# 单进程模式（开发测试）
python -m engine.engine_core.main --config configs/dev/config.yaml

# 多进程模式（模拟生产）
python scripts/launch_multiprocess.py --env dev
```

### 3. 运行测试
```bash
# 单元测试
pytest tests/unit/

# 集成测试
pytest tests/integration/

# 端到端测试
pytest tests/e2e/
```

## 📊 进程架构

### 核心交易进程组（同机部署）
- **engine_core**: 主循环协调器
- **pricing**: 定价引擎
- **execution**: 执行引擎

### 数据层进程（独立部署）
- **market_data**: 市场数据处理
- **account_state**: 账户状态管理

### 风控进程（严格隔离）
- **risk**: 集中式风控服务

### 运维进程组（独立机器）
- **ops_dashboard**: 监控仪表板
- **param_server**: 参数服务
- **quality_monitor**: 质量监控

## 🔌 核心接口（Ports）

### 5个核心DTO
1. **MarketSnapshot**: 市场数据快照
2. **QuoteSet**: 报价集合
3. **OrderPlan**: 订单计划
4. **ExecutionReport**: 执行报告
5. **RiskVerdict**: 风控裁决

### 3个补充DTO
6. **PositionState**: 仓位状态
7. **HedgeCommand**: 对冲指令
8. **SystemHealth**: 系统健康度

## 📈 性能指标

- **主循环延迟**: < 50μs
- **端到端延迟**: < 1ms (p99)
- **吞吐量**: > 100K events/sec
- **Maker率**: > 95%

## 🔧 技术栈

- **核心语言**: Python 3.10+
- **高性能组件**: Rust (执行关键路径)
- **事件总线**: Redis Streams / Disruptor
- **时间同步**: PTP + GPS
- **监控**: Prometheus + Grafana
- **部署**: Docker + Kubernetes

## 📝 开发规范

1. **域边界**: 每个域独立开发，通过DTO通信
2. **事件驱动**: 异步事件，无阻塞调用
3. **配置外部化**: 所有配置通过YAML管理
4. **测试覆盖**: 单元测试 > 90%

## 🚨 生产部署

```bash
# 构建镜像
docker build -t doge-mm-enterprise:latest .

# Kubernetes部署
kubectl apply -f k8s/

# 健康检查
curl http://localhost:8080/health
```

## 📚 相关文档

- [完整架构文档](project_architecture_docs/README.md)
- [主架构设计](project_architecture_docs/01_MAIN_ARCHITECTURE_V10.md)
- [域管理器](project_architecture_docs/03_DOMAIN_MANAGERS.md)
- [开发计划](project_architecture_docs/05_DEVELOP_PROGRESS_PLAN.md)

## 📄 License

Proprietary - All Rights Reserved