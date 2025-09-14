# 🏢 DOGE Market Maker Enterprise - 世界级做市商架构

> 8域分离 + 极薄主循环 + 事件驱动 = 无限扩展性

## 🏗️ 仓库结构

```
doge-mm-enterprise/
├── engine/                      # 核心引擎
│   ├── domains/                 # 8个域管理器
│   │   ├── reference/          # 主数据与时间管理
│   │   ├── market_data/        # 市场数据域
│   │   ├── account_state/      # 账户状态域
│   │   ├── risk/               # 风控域
│   │   ├── pricing/            # 定价域
│   │   ├── execution/          # 执行域
│   │   ├── hedging/            # 对冲域
│   │   └── ops/                # 运维质量域
│   ├── engine_core/            # 极薄主循环（20行）
│   ├── dto/                    # 数据传输对象
│   ├── infra/                  # 基础设施
│   ├── events/                 # 事件架构
│   │   ├── bus/               # 事件总线实现
│   │   ├── schemas/           # 事件模式定义
│   │   └── replay/            # 事件重放引擎
│   ├── adapters/              # 交易所适配器
│   │   ├── binance/          # 币安实现
│   │   ├── okx/              # OKX实现
│   │   └── common/           # 通用接口
│   └── monitoring/            # 监控可观测性
│       ├── metrics/          # Prometheus指标
│       ├── tracing/          # 分布式追踪
│       └── alerting/         # 告警规则
├── tests/                     # 测试套件
│   ├── unit/                 # 单元测试
│   ├── integration/          # 集成测试
│   ├── e2e/                  # 端到端测试
│   ├── stress/               # 压力测试
│   └── chaos/                # 混沌工程
├── configs/                   # 配置管理
│   ├── dev/                  # 开发环境
│   ├── staging/              # 预发环境
│   ├── prod/                 # 生产环境
│   └── markets/              # 市场配置
├── scripts/                   # 工具脚本
│   ├── deploy/               # 部署脚本
│   ├── backtest/             # 回测工具
│   ├── benchmark/            # 性能基准
│   └── replay/               # 事件重放
└── docs/                      # 文档
    ├── architecture/          # 架构文档
    ├── api/                   # API文档
    └── operations/            # 运维手册
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

- [架构设计](docs/ARCHITECTURE.md)
- [域管理器](docs/DOMAIN_MANAGERS.md)
- [接口契约](docs/PORTS.md)
- [运维手册](docs/OPERATIONS.md)

## 📄 License

Proprietary - All Rights Reserved