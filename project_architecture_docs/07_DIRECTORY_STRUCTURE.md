# 📁 Enterprise目录结构

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
├── configs/                     # 配置文件
├── scripts/                     # 脚本工具
├── tests/                       # 测试套件
└── docs/                        # 文档

```

---

*更新时间: 2025-01-19*
*架构版本: 1.0.0*