# 🏢 Enterprise世界级架构 V10 - 拆分优化版

> 每个组件只做一件事，并把它做到极致

## 📐 架构设计原则

1. **单一职责**：一个组件只负责一个领域
2. **清晰边界**：组件之间通过接口通信
3. **数据单向流**：避免循环依赖
4. **无功能重叠**：不同组件不做相同的事

---

## 🏗️ 十层架构总览

```
┌─────────────────────────────────────────────────┐
│               监控层（第8层）                      │
│            ObservabilityDashboard                │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│            实验与生产工程化层（第7层）              │
│    ShadowTrading + CanaryDeployment + ...       │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│               对冲引擎层（第6层）                   │
│             FAHE + MultiVenue Router             │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│                执行层（第4层）                     │
│    IBE (批量执行) + MillisecondResponse         │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│                决策层（第3层）                     │
│  QuotePricingService + OrderOrchestrator + InventorySystem  │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│                风控层（第2层）                     │
│ CentralizedRiskServer + SSOT + StateReconciler  │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│                数据层（第1层）                     │
│ MultiSource + DropCopy + UserDataStream + L3   │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│            时间治理层（第0.1层）                   │
│     TimeAuthority + LatencyTracker               │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│           品种主数据层（第0层）                     │
│        InstrumentMaster / ReferenceData          │
└─────────────────────────────────────────────────┘
                        ↑
┌─────────────────────────────────────────────────┐
│          系统调优基线层（第-1层）                   │
│      NetworkHostTuningBaseline + PTSync         │
└─────────────────────────────────────────────────┘
```

---

## 🏗️ 第-1层：系统调优基线层（硬件基础设施）🆕

### -1.1 NetworkHostTuningBaseline（网络主机调优基线）🆕
**唯一职责**：底层硬件与网络性能优化的固化基线
```python
职责：
✅ CPU 亲和性绑定（避免进程跳核）
✅ NIC 多队列绑定与中断亲和
✅ 忙轮询(busy polling) 减少内核延迟
✅ HugePages 内存优化（减少TLB miss）
✅ NUMA 拓扑优化（就近内存访问）
✅ 内核绕过网络栈（DPDK/用户态）
✅ 时钟源固定化（TSC/HPET选择）
✅ 系统调用优化（减少上下文切换）
❌ 不管业务逻辑
❌ 不管应用层配置

CPU调优配置：
- 隔离CPU核心：专用于交易线程
- 亲和性绑定：核心线程固定CPU
- 禁用CPU频率调节：锁定最高频率
- 禁用超线程：避免资源竞争
- 设置进程优先级：实时调度优先级

网络调优配置：
- 多队列NIC：每核专用队列
- 中断亲和：中断处理绑核
- 忙轮询：绕过中断机制
- 内核绕过：DPDK/用户态网络
- TCP/UDP优化：缓冲区大小调优
- 网卡Offload：硬件加速启用

内存调优配置：
- HugePages：大页内存减少TLB miss
- NUMA绑定：内存就近分配
- 内存预分配：避免运行时分配
- Swap禁用：防止内存换出

输出接口：
- validate_system_baseline() → SystemHealth
- get_cpu_affinity_config() → CPUConfig
- get_network_optimization() → NetworkConfig
- get_memory_layout() → MemoryConfig
- check_realtime_capability() → RTStatus
- benchmark_latency() → LatencyProfile
- one_click_health_check() → BaselineStatus  # SRE一键体检
```

### -1.2 PTSyncService（精密时间同步服务）🆕
**唯一职责**：硬件级时间同步与TimeAuthority协同
```python
职责：
✅ PTP/IEEE1588 硬件时间戳
✅ GPS/原子钟时间源对接
✅ Grandmaster/Boundary Clock配置
✅ 网络时间戳硬件卸载
✅ 与TimeAuthority软件协同
❌ 不管软件层时间逻辑

PTP配置：
- Grandmaster Clock: GPS/原子钟作为主时钟源
- Boundary Clock: 多网段时间中继
- 硬件时间戳: NIC硬件打时间戳
- 时钟精度: 亚微秒级同步精度

与TimeAuthority协同：
- PTSyncService: 提供硬件级时间基准
- TimeAuthority: 基于硬件时间做软件层治理
- 双重验证: 硬件时间戳 vs 软件时间戳
- 漂移检测: 硬件与软件时间差异监控

输出接口：
- get_hardware_timestamp() → HardwareTime
- validate_ptp_sync() → SyncStatus
- get_clock_drift() → DriftMetrics
- coordinate_with_time_authority() → bool
```

---

## 📋 第0层：品种主数据层（基础设施）

### 0.0 InstrumentMaster / ReferenceDataService（品种主数据服务）🆕
**唯一职责**：集中管理所有交易品种的完整信息
```python
职责：
✅ 撮合规则管理（Limit/Market/Stop等限制）
✅ 价格步长与数量精度管理
✅ 费率层级管理（Maker/Taker/VIP等级）
✅ 交易状态管理（Normal/Halt/Close-Only/Delist）
✅ 交易时段管理（开市/收市/维护时间）
✅ 合约乘数管理（期货/期权）
✅ 返佣层级管理（不同用户等级）
✅ 版本化与热更新
✅ 统一依赖注入给所有组件
❌ 不执行交易逻辑

品种信息模型：
{
  "symbol": "DOGEUSDT",
  "base_asset": "DOGE",
  "quote_asset": "USDT",
  "status": "TRADING",  # TRADING/HALT/CLOSE_ONLY/DELISTED
  "trading_hours": {
    "start": "00:00:00Z",
    "end": "23:59:59Z",
    "maintenance": ["02:00-02:30"]
  },
  "price_rules": {
    "tick_size": "0.00001",
    "min_price": "0.00001",
    "max_price": "10000"
  },
  "quantity_rules": {
    "step_size": "1",
    "min_qty": "1",
    "max_qty": "10000000",
    "min_notional": "5"
  },
  "trading_rules": {
    "order_types": ["LIMIT", "MARKET", "LIMIT_MAKER"],
    "time_in_force": ["GTC", "IOC", "FOK"],
    "max_orders_per_symbol": 200,
    "max_algo_orders": 5
  },
  "fee_schedule": {
    "maker_fee": "0.001",
    "taker_fee": "0.001",
    "vip_levels": {
      "VIP1": {"maker": "0.0009", "taker": "0.001"},
      "VIP2": {"maker": "0.0008", "taker": "0.001"}
    }
  },
  "rebate_schedule": {
    "maker_rebate": "0.0001",
    "high_volume_rebate": "0.0002"
  },
  "version": "v1.2.3",
  "last_updated": "2024-01-15T10:30:00Z"
}

热更新机制：
- 版本控制：每次更新递增版本号
- 推送通知：主动通知所有依赖组件
- 平滑切换：旧版本兼容缓冲期
- 回滚支持：出问题立即回滚到上一版本

输出接口：
- get_instrument(symbol) → InstrumentInfo
- get_trading_status(symbol) → TRADING/HALT/CLOSE_ONLY
- get_fee_schedule(symbol, user_level) → fee_info
- get_price_precision(symbol) → tick_size
- get_quantity_precision(symbol) → step_size
- is_trading_allowed(symbol, order_type) → bool
- subscribe_updates(callback) → 订阅更新通知
- update_instrument(symbol, new_data) → 热更新
- rollback_version(symbol, version) → 版本回滚

统一依赖注入：
- QuotePricingService + OrderOrchestrator: 获取价格精度、交易规则
- SSOTReservationClosedLoop: 获取最小交易金额
- IBE: 获取订单类型限制、速率限制
- HedgeRouter: 获取费率信息、交易状态
- PricingEngine: 获取tick_size进行报价对齐
```

## 🔐 第0.1层：安全服务层（基础设施）

### 0.1.1 SigningService（API签名服务）🆕
**唯一职责**：集中化API密钥管理与签名服务
```python
职责：
✅ API密钥安全存储（KMS/HSM）
✅ 签名请求代理服务
✅ 短期访问票据管理
✅ 签名审计日志
✅ 密钥轮转管理
❌ 不存储明文密钥在业务系统

安全架构：
- 密钥存储：AWS KMS / Azure Key Vault / 硬件HSM
- 访问控制：基于短期token的访问
- 网络隔离：独立安全网络段
- 审计追踪：所有签名操作记录

签名流程：
1. CoreTradeConnector请求签名
2. 提供短期token验证身份
3. SigningService从KMS获取密钥
4. 执行签名并返回结果
5. 记录审计日志

输出接口：
- request_signature(params, token) → signature
- refresh_access_token() → new_token
- rotate_api_keys() → key_rotation_status
- get_signing_audit() → audit_logs

解决的问题：
- ✅ 消除"签名失败"问题
- ✅ API密钥不再存储在业务系统
- ✅ 开发人员无法接触真实密钥
- ✅ 集中化密钥管理和轮转
```

### 0.1.2 ChangeGuard（双人复核服务）🆕
**唯一职责**：重大变更的双人复核与变更窗口管理
```python
职责：
✅ 参数变更双人复核
✅ 版本发布审批流程
✅ 金丝雀放量审批
✅ 变更冻结窗口管理
✅ 变更回滚机制
❌ 不管日常运维操作

双人复核流程：
1. 发起人提交变更请求
2. 系统生成变更单
3. 指定审批人员（不能是同一人）
4. 第一人审核并批准
5. 第二人独立审核并确认
6. 系统执行变更
7. 记录完整审计链

变更类型：
- PARAMETER：参数调整（如spread、size限制）
- VERSION：策略版本发布
- CANARY：金丝雀流量调整
- EMERGENCY：紧急变更（单人特批）

冻结窗口：
- 交易高峰期：禁止非紧急变更
- 周末维护窗：允许所有变更
- 节假日：仅允许紧急变更

输出接口：
- submit_change_request(change) → request_id
- approve_change(request_id, approver) → approval_status
- execute_approved_change(request_id) → execution_result
- rollback_change(request_id) → rollback_status
- get_change_history() → change_audit

与现有组件集成：
- ParameterServer: 参数变更需要复核
- CanaryDeployment: 放量调整需要审批
- InstrumentMaster: 品种配置变更需要复核
```

## 🔄 第0.2层：容灾服务层（基础设施）

### 0.2.1 LightweightFailoverManager（轻量级故障切换管理器）🆕
**唯一职责**：单机房内的高可用和简单容灾
```python
职责：
✅ 关键服务健康检查
✅ 服务自动重启和故障切换
✅ 状态快照和恢复
✅ 简单的跨服务器备份
❌ 不做跨区域复杂容灾

适合您的轻量级容灾：
- 双服务器部署（主+备）
- 本地状态同步
- 自动故障检测
- 快速服务重启

核心组件容灾：
1. IBE执行层：主备双实例，状态共享
2. CentralizedRiskServer：双机热备
3. SigningService：主备切换
4. 关键数据库：实时备份

检测机制：
- 心跳检测：每5秒检查服务状态
- 业务检测：监控订单执行成功率
- 网络检测：监控与交易所连接
- 资源检测：CPU/内存/磁盘

切换策略：
- 自动切换：明确的服务挂掉
- 手动切换：可疑但不确定的问题
- 快速恢复：30秒内完成切换

输出接口：
- monitor_service_health() → 健康状态
- trigger_failover(service) → 执行切换
- backup_critical_state() → 状态备份
- restore_from_backup() → 状态恢复
```

### 0.2.2 SessionStateManager（会话状态管理器）🆕
**唯一职责**：交易会话的状态保持和恢复
```python
职责：
✅ 交易会话状态持久化
✅ 订单状态快照
✅ 用户连接状态维护
✅ 会话热恢复
❌ 不做跨区域复杂同步

会话状态包含：
- 活跃订单列表
- 预扣资金状态
- WebSocket连接状态
- 最后处理的序列号

快照机制：
- 每分钟自动快照
- 关键操作前快照
- 异常情况紧急快照

恢复流程：
1. 从最新快照恢复状态
2. 通过EventSourcingEngine重放未处理事件
3. 重建WebSocket连接
4. 恢复订单监控

与现有组件集成：
- EventSourcingEngine：提供事件重放
- StateReconciler：确保状态一致性
- UserDataStream：重建连接状态
- SSOTClosedLoop：恢复预扣状态

输出接口：
- capture_session_snapshot() → 会话快照
- restore_session_from_snapshot() → 会话恢复
- sync_active_orders() → 同步活跃订单
- rebuild_connections() → 重建连接
```

## ⏰ 第0.3层：时间治理层（基础设施）

### 0.3.1 TimeAuthority（统一时间权威）
**唯一职责**：提供纳秒级精确时间戳
```python
职责：
✅ NTP/PTP时间同步
✅ 硬件时间戳获取
✅ Event Time vs Processing Time分离
✅ 时间漂移检测与校正
❌ 不管业务逻辑

核心功能：
- get_hardware_timestamp() → nanoseconds  # 网卡硬件时间
- get_event_time(event) → timestamp       # 事件发生时间
- get_processing_time() → timestamp       # 处理时间
- detect_clock_drift() → drift_ms         # 时钟漂移检测

时间戳精度要求：
- 订单创建: 微秒级（用于QPE）
- 市场数据: 纳秒级（用于延迟分析）
- 成交回报: 微秒级（用于对账）
```

### 0.3.2 LatencyTracker（延迟追踪器）
**唯一职责**：全链路延迟监控
```python
职责：
✅ 记录每个环节的时间戳
✅ 计算组件间延迟
✅ 识别性能瓶颈
❌ 不干预执行

关键测量点：
1. 市场数据接收 → 决策生成（决策延迟）
2. 决策生成 → 订单发送（执行延迟）
3. 订单发送 → 交易所ACK（网络延迟）
4. 成交通知 → 本地更新（处理延迟）

输出指标：
- get_p50_latency() → 0.5ms
- get_p99_latency() → 2ms
- get_bottleneck() → "QuotePricingService: 0.6ms + OrderOrchestrator: 0.6ms"
```

## 📊 第1层：数据层（信息源）

### 🎯 专业化市场数据子系统（交易所级别）

#### 1.0.1 MultiSourceDataGovernance（多路数据源治理）🆕
**唯一职责**：多路独立行情源的统一管理
```python
职责：
✅ 主所直连（WebSocket + 备用REST）
✅ 辅路聚合源（第三方数据商）
✅ 录播回放源（历史数据重放）
✅ 源间质量对比与切换
✅ "行情陈旧即全撤"硬规则执行
❌ 不处理具体业务逻辑

数据源配置：
- PRIMARY: 币安WebSocket直连（延迟<10ms）
- SECONDARY: 币安REST轮询（延迟~50ms）
- AGGREGATE: 第三方聚合源（如CoinAPI）
- REPLAY: 历史数据录播

硬规则：
- 陈旧检测阈值：>100ms
- 触发动作：立即全撤所有订单
- 降级策略：切换到备用源

输出接口：
- get_best_source() → source_info
- detect_stale_data() → bool
- trigger_emergency_cancel() → 执行全撤
- switch_to_fallback() → 切换数据源
```

#### 1.0.2 IncrementalStreamProcessor（增量流处理器）🆕
**唯一职责**：提供严格序列一致性的增量数据流
```python
职责：
✅ 增量订单簿构建与维护
✅ 序列号严格校验
✅ 增量流接口（非快照）
✅ 流式数据推送
❌ 不做数据存储

流式接口：
- subscribe_depth_stream(callback) → 增量深度流
- subscribe_trade_stream(callback) → 增量成交流
- get_sequence_number() → 当前序列号
- validate_sequence_integrity() → 序列完整性

数据格式：
{
  "stream": "depth_incremental",
  "sequence": 12345,
  "timestamp": 1694678901234,
  "changes": [
    {"side": "bid", "price": "0.4000", "qty": "100", "action": "update"}
  ]
}
```

#### 1.0.3 FeedArbiter（数据源仲裁器）🆕
**唯一职责**：多源数据质量评估与最优选择
```python
职责：
✅ 比较多个数据源的延迟和质量
✅ 选择最优质量的数据源
✅ 检测数据源异常并隔离
❌ 不处理数据内容

输出接口：
- select_best_feed() → feed_id
- get_feed_quality(feed_id) → {latency_ns, success_rate, score}
- isolate_bad_feed(feed_id)
```

#### 1.0.2 Sequencer（序列化器）🆕
**唯一职责**：确保市场事件严格有序
```python
职责：
✅ 按交易所时间戳排序
✅ 处理乱序数据（100ms窗口）
✅ 序列号连续性检查
❌ 不修改数据内容

输出接口：
- sequence_events(events) → ordered_events
- check_sequence_gap() → missing_sequences
```

#### 1.0.3 GapFillEngine（缺口填充引擎）🆕
**唯一职责**：检测并填充数据缺失
```python
职责：
✅ 实时检测数据缺口
✅ 从历史或备份源回补数据
✅ 使用插值算法填充小缺口
❌ 不改变正常数据

输出接口：
- detect_gaps() → gap_list
- fill_gap(gap_info) → filled_data
- get_gap_statistics() → {total_gaps, filled_count}
```

#### 1.0.4 StaleTick Gate（陈旧数据过滤门）🆕
**唯一职责**：过滤陈旧无效数据
```python
职责：
✅ 检查时间戳新鲜度（<100ms）
✅ 验证价格合理性（3σ范围）
✅ 检测异常成交量
❌ 不生成新数据

输出接口：
- is_fresh(tick) → bool
- validate_price(price, symbol) → bool
- filter_stale_ticks(ticks) → fresh_ticks
```

#### 1.0.5 L3 BookBuilder（三级订单簿构建器）🆕
**唯一职责**：构建完整订单簿模型
```python
职责：
✅ 处理增量深度更新
✅ 维护订单ID级别信息
✅ 合并快照与增量更新
✅ 验证订单簿一致性
❌ 不执行交易决策

输出接口：
- build_book(snapshot, deltas) → L3_book
- get_order_position(order_id) → queue_position
- validate_book_integrity() → bool
```

### 1.1 DualActiveMarketData
**唯一职责**：提供经过专业处理的市场数据
```python
职责：
✅ 接收专业子系统处理后的数据
✅ 提供订单簿深度（从L3 BookBuilder）
✅ 提供最新成交价（经过StaleTick过滤）
✅ 双活备份防故障
❌ 不管账户数据
❌ 不管交易执行

数据流：
FeedArbiter → Sequencer → GapFillEngine → StaleTick Gate → L3 BookBuilder → DualActiveMarketData

输出接口：
- get_snapshot() → {bid, ask, spread, depth, queue_info}
- get_trades() → [{price, qty, time}...]
- get_l3_book() → OrderBook  # 包含队列位置信息
```

### 1.2 UserDataStream (UDS)
**唯一职责**：提供账户私有数据（主通道）
```python
职责：
✅ 实时推送余额变化
✅ 实时推送订单状态
✅ 管理listenKey生命周期
❌ 不管市场数据
❌ 不管交易执行

输出接口：
- get_balance() → {DOGE: 1000, USDT: 500}
- get_open_orders() → [order1, order2...]
- on_balance_update(callback)
- on_order_update(callback)
```

### 1.3 DropCopyIngestor（独立成交抄送引擎）🆕
**唯一职责**：接入交易所独立成交/状态抄送流
```python
职责：
✅ 独立成交流接入（与主通道分离）
✅ 订单状态抄送流接入
✅ 时间锚点校验
✅ 订单键匹配验证
✅ 数据完整性检查
❌ 不做状态修正（StateReconciler负责）

独立通道架构：
- 主通道：UserDataStream（交易请求响应）
- 抄送通道：DropCopy（独立成交/状态推送）
- 避免单点故障：两路数据互相验证

数据源：
- 交易所Drop-Copy API（如Binance的executionReport）
- 独立WebSocket连接
- 与主通道物理分离

输出接口：
- ingest_trade_copy(trade_data) → 成交抄送数据
- ingest_order_status_copy(status_data) → 状态抄送数据
- validate_timestamp_anchor(event) → 时间锚校验
- match_order_key(local_order, copy_order) → 订单匹配
```

### 1.4 StateReconciler（状态和解协调器）🆕
**唯一职责**：以交易所为准纠正本地状态
```python
职责：
✅ 本地状态 vs 交易所状态对比
✅ 发现状态不一致并纠正
✅ 丢ACK/断连/重试混杂场景处理
✅ 定期对账 + 事件驱动对账
✅ 触发SSOT/Reservation/Position修正
❌ 不记录事件（InstitutionalEventLedger负责）

对账机制：
1. 定期对账：每30秒全量对账
2. 事件驱动：检测到异常立即对账
3. 断连恢复：重连后强制对账
4. 交易所优先：以交易所状态为准

纠正流程：
发现不一致 → 以交易所为准 → 修正本地状态 → 记录修正事件 → 通知相关组件

输出接口：
- reconcile_orders() → 订单状态对账
- reconcile_balances() → 余额状态对账
- reconcile_positions() → 仓位状态对账
- trigger_correction(discrepancy) → 触发状态修正
- schedule_periodic_reconcile() → 定期对账

与现有组件集成：
- 读取：DropCopyIngestor + UserDataStream
- 写入：InstitutionalEventLedger（记录修正事件）
- 触发：SSOTClosedLoop.correction_branch()（修正分支）
```

---

## 🛡️ 第2层：风控层（资金守护）

### 🏦 集中式风控服务器（独立进程）🆕

#### 2.0.1 CentralizedRiskServer（集中式风控服务器）🆕
**唯一职责**：四维限额与前置风控检查（独立进程）
```python
职责：
✅ 四维限额管理（账户/品种/交易所/策略）
✅ 自成交防控（STP - Self Trade Prevention）
✅ Token Bucket订单速率限流
✅ 前置风控白名单检查
✅ 独立进程部署（独立于策略/执行）
❌ 不执行具体交易

四维限额体系：
1. 账户维度：单账户总敞口限制
2. 品种维度：单品种最大持仓限制
3. 交易所维度：单交易所风险敞口限制
4. 策略维度：单策略资金使用限制

前置检查流程：
QuotePricingService → OrderOrchestrator → CentralizedRiskServer → PreTradeGuardrail → IBE执行

完整Pre-Trade检查链：
1. 四维限额检查（账户/品种/交易所/策略）
2. STP自成交防控
3. Token Bucket速率限流
4. 动态价格围栏
5. Fat-Finger异常检测
6. OTR/取消率治理
7. 最终白名单检查

部署架构：
- 独立主机/独立进程
- 高可用双机热备
- 毫秒级响应时间
- 所有订单必过风控

输出接口：
- pre_check_order(order) → {approved: bool, reason: str}
- update_limits(dimension, limits) → bool
- check_stp_violation(order) → bool
- consume_rate_limit(strategy) → bool
- emergency_freeze_account() → 冻结账户
```

#### 2.0.2 TokenBucketLimiter（令牌桶限流器）🆕
**唯一职责**：多维度订单速率限制
```python
职责：
✅ 策略级别速率限制
✅ 账户级别速率限制
✅ 交易所级别速率限制
✅ 品种级别速率限制
❌ 不管具体订单内容

限流维度：
- 策略限流：单策略10单/秒
- 账户限流：单账户50单/秒
- 交易所限流：币安100单/秒
- 品种限流：DOGEUSDT 20单/秒

令牌桶配置：
- 桶容量：允许的突发流量
- 补充速率：令牌恢复速度
- 泄露算法：平滑流量控制

输出接口：
- acquire_token(dimension, count=1) → bool
- get_remaining_tokens(dimension) → int
- set_rate_limit(dimension, rate) → bool
```

#### 2.0.3 SelfTradePreventionEngine（自成交防控引擎）🆕
**唯一职责**：防止账户内部自成交
```python
职责：
✅ 检测同账户对敲风险
✅ 阻止自成交订单
✅ 多策略协调（同账户不同策略）
✅ 历史自成交分析
❌ 不管其他风控

检测逻辑：
1. 同账户、同品种、相反方向
2. 价格重叠区间检测
3. 时间窗口内的订单关联
4. 多策略间的协调

防控策略：
- REJECT：直接拒绝自成交订单
- CANCEL_OLDEST：取消最老的对冲订单
- CANCEL_SMALLEST：取消数量较小的订单
- WARNING：只告警不阻止

输出接口：
- detect_self_trade(new_order, open_orders) → STPResult
- set_stp_policy(policy) → bool
- get_stp_statistics() → 自成交统计
```

### 2.0.4 PreTradeGuardrail（撮合前合规栅格）🆕
**唯一职责**：补充细化的撮合前风控检查
```python
职责：
✅ 动态价格围栏（基于实时波动率）
✅ Fat-Finger异常检测（大单/错价）
✅ OTR(Order-to-Trade Ratio)治理
✅ 取消率监控与限制
✅ 名义敞口实时检查
❌ 不替代现有风控（作为补充）

细化检查项：
1. 价格围栏：
   - 动态波动带：当前价格±2σ范围
   - 撮合带检查：订单价格不超出合理范围
   - 实时调整：基于市场波动率动态更新

2. Fat-Finger检测：
   - 异常大单：超过正常订单10倍
   - 价格偏离：偏离市场价>5%立即拒绝
   - 数量异常：单笔超过日均成交量1%

3. OTR治理：
   - 订单成交比：Order-to-Trade Ratio<10:1
   - 取消率限制：取消率<30%
   - 频率限制：防止恶意刷单

检查级别：
- BLOCK：直接拒绝订单
- WARN：记录警告但通过
- THROTTLE：临时降速处理

输出接口：
- validate_price_fence(price, symbol) → bool
- detect_fat_finger(order) → FatFingerResult
- check_otr_compliance() → OTRStatus
- monitor_cancel_ratio() → CancelRatioMetrics

与现有组件协作：
- 在CentralizedRiskServer之后执行
- 在IBE之前最后检查
- 补充而非替代现有风控
```

### 2.1 PessimisticReservationModel
**唯一职责**：预扣资金管理
```python
职责：
✅ 下单前预扣资金
✅ 成交后确认扣除
✅ 失败后释放资金
❌ 不管订单执行
❌ 不决定订单大小

输出接口：
- reserve(order_id, amount) → bool
- confirm(order_id)
- release(order_id)
- get_available() → {DOGE: 800, USDT: 400}
```

### 2.2 SSOTReservationClosedLoop
**唯一职责**：确保资金一致性 + 订单状态机管理 + 状态修正
```python
职责：
✅ 维护预扣闭环
✅ 订单状态机管理（防止状态错乱）
✅ 幂等性保证（防止重复处理）
✅ 30秒超时自动释放
✅ 对账资金状态
✅ 状态修正分支（StateReconciler触发）🆕
❌ 不管交易策略
❌ 不管订单执行

订单状态机：
NEW → PARTIALLY_FILLED → FILLED
  ↓         ↓              ↓
CANCELED  CANCELED     (终态)
  ↓
REJECTED/EXPIRED

幂等性保护：
1. ClientOrderId去重：
   - 维护已处理订单ID集合
   - 拒绝重复的ClientOrderId

2. 状态转换幂等：
   - 同一状态转换只执行一次
   - 重复的ACK/FILL只处理一次

3. 资金操作幂等：
   - 预扣只执行一次
   - 释放只执行一次

输出接口：
- register_order(order, client_order_id) → bool
- is_duplicate(client_order_id) → bool
- on_order_ack(order_id) → success
- on_order_filled(order_id, amount) → success
- get_order_state(order_id) → OrderState
- correction_branch(discrepancy) → 状态修正分支 🆕
```

### 🔄 状态和解数据流 🆕

```
交易请求通道（主通道）
UserDataStream → 订单状态/余额更新
        ↓
SSOTReservationClosedLoop → 本地状态维护

Drop-Copy通道（独立验证）
DropCopyIngestor → 独立成交/状态抄送
        ↓
StateReconciler → 状态对比
        ↓
    发现不一致？
    ├─ 否 → 正常流程
    └─ 是 → 以交易所为准修正
            ↓
    SSOTClosedLoop.correction_branch()
            ↓
    InstitutionalEventLedger.record_correction()
```

### 2.3 InstitutionalEventLedger
**唯一职责**：审计级事件记录
```python
职责：
✅ 记录所有交易事件
✅ 不可篡改的账本
✅ 提供审计追踪
❌ 不管交易决策
❌ 不管执行

输出接口：
- record_event(event)
- get_history(time_range) → [events...]
- get_pnl() → {realized: 100, unrealized: 50}
```

---

## 🧠 第3层：决策层（智能大脑）

### 3.1 LiquidityEnvelope
**唯一职责**：资金分配策略
```python
职责：
✅ 决定每层使用多少资金
✅ 根据库存动态调整
❌ 不决定订单大小
❌ 不管订单执行

输出接口：
- get_allocation() → {
    L0: {capital: 600 USDT, weight: 0.6},
    L1: {capital: 250 USDT, weight: 0.25},
    L2: {capital: 150 USDT, weight: 0.15}
  }
```

### 3.2 QuotePricingService（智能定价引擎）🔄
**单一职责**：专注定价算法（拆分自SmartOrderSystem）
```python
核心职责：
✅ 【定价专业】计算最优报价
✅ 【定价专业】价差动态调整
✅ 【定价专业】库存偏斜调价
✅ 【定价专业】波动率适应性调价
❌ 不管订单创建（OrderOrchestrator负责）
❌ 不管订单执行（IBE负责）
❌ 不管资金分配（LiquidityEnvelope负责）

子组件功能：
1. AdaptiveSpreadCalculator（自适应价差）:
   - calculate_base_spread() → 基础价差
   - adjust_for_volatility() → 波动率调整
   - adjust_for_liquidity() → 流动性调整

2. VolatilityEstimator（波动率估算）:
   - estimate_short_term() → 短期波动
   - estimate_medium_term() → 中期趋势
   - get_confidence_interval() → 置信区间

3. InventoryAdjustment（库存调价）:
   - calculate_skew() → 库存偏斜调整
   - get_target_ratio() → 目标库存比例
   - apply_gradient() → 渐进式调价

4. PriceGuard（价格保护）:
   - validate_spread() → 价差合理性检查
   - validate_price_bounds() → 价格边界检查
   - detect_anomaly() → 异常价格检测

输出接口：
- calculate_quotes(market) → QuoteSet{bid, ask, confidence}
- get_spread_analysis() → SpreadMetrics
- validate_quote_quality() → QualityScore
```

### 3.3 OrderOrchestrator（订单协调引擎）🔄
**单一职责**：专注执行协调（拆分自SmartOrderSystem）
```python
核心职责：
✅ 【执行专业】根据报价创建订单
✅ 【执行专业】智能拆单策略
✅ 【执行专业】智能撤单决策
✅ 【执行专业】队列位置优化
❌ 不管定价计算（QuotePricingService负责）
❌ 不管实际执行（IBE负责）
❌ 不管资金分配（LiquidityEnvelope负责）

子组件功能：
1. SmartPlacement（智能下单）:
   - create_orders_from_quotes() → Order列表
   - optimize_order_sizes() → 优化下单量
   - select_placement_strategy() → 下单策略选择

2. QPEWithFallback（可用性感知的队列估算）:
   - Level 1: estimate_from_l3() → 真实L3数据估算
   - Level 2: estimate_from_l2_depth() → L2深度估算
   - Level 3: estimate_from_history() → 历史概率估算
   - Level 4: conservative_estimate() → 保守估算
   - 自动降级机制，适应不同交易所L3数据质量

3. MicroLotEngine（智能拆单）:
   - split_large_orders() → 大单拆分
   - calculate_lot_sizes() → 动态批次大小
   - minimize_market_impact() → 减少市场冲击

4. CancellationAnalyzer（撤单分析）:
   - analyze_order_performance() → 订单表现分析
   - suggest_cancellations() → 撤单建议
   - prioritize_cancellation() → 撤单优先级

5. OrderRiskValidator（订单风险验证）:
   - validate_order_risk() → 订单风险检查
   - check_position_limits() → 仓位限制检查
   - verify_compliance() → 合规性验证

输出接口：
- execute_quotes(quotes) → ExecutionPlan
- generate_orders(quotes, capital) → [Order...]
- evaluate_cancellation(orders, market) → [CancelHint...]
- get_queue_position_estimate() → QueueEstimate
```

### 3.4 ThreeDomainInventorySystem
**唯一职责**：库存平衡管理
```python
职责：
✅ 监控库存偏离
✅ 三时域调整策略
✅ 触发再平衡信号
❌ 不管订单生成
❌ 不管执行

三个时域：
- MillisecondDomain: 毫秒级响应失衡
- SecondDomain: 秒级调整倾斜
- MinuteDomain: 分钟级TWAP再平衡

输出接口：
- get_inventory_ratio() → 0.45 (45% DOGE)
- get_rebalance_signal() → {action: BUY, urgency: HIGH}
- get_skew_adjustment() → {bid_boost: 1.2, ask_reduce: 0.8}
```

---

## ⚡ 第4层：执行层（高速引擎）

### 4.1 IBE (Intelligent Batch Executor)
**唯一职责**：批量并发执行（包括批量撤单）
```python
职责：
✅ 批量并发发送订单
✅ 批量并发撤销订单
✅ 动态TTL生命周期管理（毫秒级~秒级自适应）
✅ 失败重试机制
❌ 不决定订单大小
❌ 不决定撤单策略（只执行）
❌ 不分析市场深度

动态TTL策略：
- L0层（贴边单）: 500ms-2s（高频调整）
- L1层（近距离）: 2s-5s（中频调整）
- L2层（深度单）: 5s-10s（低频调整）
- 极端行情: 100ms-500ms（紧急模式）

TTL影响因素：
1. 市场流速（成交速度）
2. 队列位置（QPE估算）
3. 价格偏离度
4. 事件毒性（突发事件）

撤单去重机制：
- OrderOrchestrator建议撤单 → 标记
- TTL到期撤单 → 检查是否已标记
- 避免重复撤单请求

核心方法：
- execute_batch(orders) → {success: 25, failed: 5}
- cancel_batch(order_ids) → {success: 20, failed: 2}
- set_dynamic_ttl(order_id, ttl_ms) → bool
- cleanup_expired() → removed_count  # 动态TTL撤单
- merge_cancel_hints(smart_hints, ttl_hints) → unique_hints
```

### 4.2 EmergencyKillSwitch（紧急停止开关）🚨
**唯一职责**：紧急情况下立即停止所有交易
```python
职责：
✅ 一键撤销所有订单
✅ 立即停止新订单创建
✅ 绕过所有决策层（直接执行）
✅ 多触发源监控
❌ 不需要等待确认
❌ 不依赖其他组件

触发条件：
1. 延迟异常：P99 > 10ms
2. 余额异常：突然减少>5%
3. 时钟漂移：>100ms
4. 行情异常：序列缺口/卡顿
5. 手动触发：人工紧急按钮

执行动作：
- IMMEDIATE: 立即撤销所有活跃订单
- BLOCK: 阻止所有新订单提交
- ALERT: 发送紧急告警
- LOG: 记录触发原因和时间

核心方法：
- trigger_kill_switch(reason) → bool
- emergency_cancel_all() → {cancelled: 100}
- block_new_orders() → bool
- reset_kill_switch() → bool  # 恢复交易
- is_active() → bool  # 检查是否已触发
```

### 4.3 MillisecondResponseSystem
**唯一职责**：毫秒级事件响应
```python
职责：
✅ 成交后立即补单
✅ 优先级队列管理
✅ 事件驱动架构
❌ 不决定订单内容
❌ 不管批量执行

输出接口：
- on_fill(callback) → immediate_response
- get_response_latency() → 0.5ms
```

### 4.3 APIRateLimiter（全局限流管理器）
**唯一职责**：API配额与限流控制
```python
职责：
✅ 全局API权重管理
✅ 按交易所/订单类型配额分配
✅ 动态限流调整
✅ 多级熔断保护
❌ 不执行交易
❌ 不做决策

限流维度：
1. Per-Venue（按交易所）:
   - Binance: 1200权重/分钟
   - OKX: 600权重/分钟

2. Per-OrderType（按订单类型）:
   - LIMIT_MAKER: 1权重
   - MARKET: 5权重
   - CANCEL: 1权重

3. Per-Symbol（按交易对）:
   - DOGEUSDT: 100订单/10秒
   - BTCUSDT: 50订单/10秒

保护机制：
- 50%使用率：正常
- 70%使用率：警告，降速
- 90%使用率：紧急，只撤单
- 95%使用率：完全停止

核心方法：
- check_quota(venue, order_type) → bool
- consume_weight(weight) → remaining
- get_cooldown_time() → seconds
- emergency_throttle() → bool
```

### 4.4 CoreTradeConnector (建议替代TurboConnector)
**唯一职责**：与交易所通信
```python
职责：
✅ 发送订单到交易所
✅ 取消订单
✅ 获取交易规则
❌ 不管市场数据（用DualActiveMarketData）
❌ 不管账户数据（用UserDataStream）
❌ 不管WebSocket（由专门组件管理）
❌ 不管限流（由APIRateLimiter管理）

核心方法（只有5个）：
- create_order(order) → order_id
- cancel_order(order_id) → success
- cancel_replace(old_id, new_order) → new_id
- test_order(order) → valid
- get_symbol_filters() → {min_qty: 1, tick_size: 0.00001}
```

---

## 📊 第5层：做市质量分析层（专业级）

### 5.1 ToxicityMonitor（订单流毒性监控器）🆕
**唯一职责**：实时监控订单流毒性和市场异常
```python
职责：
✅ VPIN（Volume-Synchronized Probability of Informed Trading）计算
✅ 订单流毒性检测（Informed vs Noise traders）
✅ Price Impact异常识别
✅ 市场结构变化检测（流动性枯竭/异常波动）
✅ Adverse Selection风险量化
❌ 不直接干预交易决策

核心算法：
1. VPIN指标：
   - 基于成交量同步的知情交易概率
   - 滑动窗口计算（1000笔成交）
   - 阈值：VPIN > 0.8 视为高毒性

2. 订单流分类：
   - Informed Flow：大单、单向、连续
   - Noise Flow：小单、双向、随机
   - Toxic Flow：异常价格冲击的订单

3. 毒性级别：
   - LOW (0-0.3)：正常市场环境
   - MEDIUM (0.3-0.7)：需要提高警惕
   - HIGH (0.7-0.9)：减少报价积极性
   - EXTREME (>0.9)：考虑暂停做市

实时指标：
- vpin_score: 当前VPIN值
- flow_toxicity: 订单流毒性评分
- adverse_selection_cost: 逆向选择成本
- price_impact_ratio: 价格冲击比率

输出接口：
- calculate_vpin(trades, window=1000) → vpin_score
- detect_toxic_flow(order_flow) → toxicity_level
- measure_adverse_selection() → selection_cost
- get_toxicity_alert() → alert_level
```

### 5.2 QuoteQualityService（报价质量服务）🆕
**唯一职责**：综合评估和优化报价质量
```python
职责：
✅ Microprice vs Quote偏差分析
✅ Fair-Value模型和偏差计算
✅ Quote Quality Score综合评分
✅ Cancel-to-Fill比率优化分析
✅ Inventory-Cost曲线建模
✅ Realized vs Expected Spread分析
❌ 不替代现有定价逻辑

Fair-Value模型：
1. Microprice计算：
   - microprice = (bid*ask_size + ask*bid_size)/(bid_size + ask_size)
   - 反映订单簿不平衡的"真实"价格

2. Fair-Value基准：
   - VWAP基准：成交量加权平均价
   - Tick-by-Tick模型：基于逐笔数据
   - Cross-Venue套利价格

3. Quote Quality Score (0-100)：
   - Spread Competitiveness: 25%
   - Fill Rate: 25%
   - Adverse Selection Avoidance: 25%
   - Inventory Management: 25%

核心指标：
- fair_value_deviation: |quote - fair_value| / fair_value
- quote_quality_score: 综合质量评分
- cancel_to_fill_ratio: 撤单成交比
- realized_spread: 实际获得的价差
- inventory_cost: 库存持有成本
- fill_probability: 预期成交概率

策略反馈：
1. 定价优化：
   - 偏差过大 → 调整报价向fair value靠拢
   - 成交率低 → 缩小spread
   - 逆选严重 → 扩大spread或暂停

2. TTL策略：
   - 质量评分高 → 延长TTL
   - 毒性环境 → 缩短TTL
   - 库存压力 → 动态调整

输出接口：
- calculate_microprice(orderbook) → microprice
- estimate_fair_value() → fair_value
- compute_quality_score() → quality_score
- analyze_fill_performance() → fill_metrics
- optimize_quote_parameters() → suggested_params
```

### 5.3 MarketQualityDashboard（做市质量仪表板）🆕
**唯一职责**：可视化做市质量指标和提供决策支持
```python
职责：
✅ 实时做市质量面板
✅ 毒性告警和建议
✅ 策略效果分析
✅ 盈利归因分析
❌ 不执行交易决策

仪表板模块：
1. 实时监控面板：
   - VPIN实时曲线
   - Quote Quality Score
   - Fair-Value偏差
   - 库存成本实时显示

2. 策略效果面板：
   - Realized PnL vs Theoretical
   - Cancel-to-Fill趋势
   - Adverse Selection损失
   - 最优spread建议

3. 风险告警面板：
   - 毒性级别告警
   - 异常订单流检测
   - 库存风险预警
   - 市场结构变化提醒

与现有组件集成：
- QuotePricingService: 接收定价优化建议
- IBE: 接收TTL调整建议
- ThreeDomainInventory: 提供库存数据
- ObservabilityDashboard: 集成到主监控面板

输出接口：
- render_realtime_panel() → 实时面板
- generate_quality_report() → 质量报告
- send_toxicity_alert() → 毒性告警
- suggest_strategy_adjustment() → 策略建议
```

## 🔄 做市质量反馈闭环

```
市场数据 → ToxicityMonitor → 毒性评估
    ↓
订单执行 → QuoteQualityService → 质量评分
    ↓
MarketQualityDashboard → 策略建议
    ↓
QuotePricingService ← 定价参数优化
    ↓
IBE ← TTL策略调整
    ↓
ThreeDomainInventory ← 库存管理优化
```

## 📊 第6层：监控层（全局视野）

### 6.1 ObservabilityDashboard
**唯一职责**：系统健康监控
```python
职责：
✅ 收集所有组件指标
✅ 计算健康分数
✅ 触发告警
❌ 不干预执行
❌ 不修改策略

8大指标：
1. 订单成功率
2. 平均延迟
3. 库存偏斜
4. 价差质量
5. 流动性供给
6. 风险敞口
7. 系统吞吐量
8. 异常计数

输出接口：
- get_health_score() → 0.85
- get_alerts() → [alert1, alert2]
- get_metrics() → {success_rate: 0.95, latency: 2ms}
```

---

## 🔄 撤单策略详解

### 两种撤单机制

#### 1. 智能撤单（OrderOrchestrator决策 → IBE执行）
```python
# OrderOrchestrator的QPEWithFallback组件分析队列位置
open_orders = user_data_stream.get_open_orders()
market = dual_active_market_data.get_snapshot()

# 评估每个订单是否需要撤销
cancel_hints = smart_order_system.evaluate_cancellation(open_orders, market)
# 返回: [
#   CancelHint(order_id=123, reason="队列位置>100，预计30秒内无法成交"),
#   CancelHint(order_id=456, reason="价格偏离>2%，已远离最优价"),
#   CancelHint(order_id=789, reason="市场流速下降80%，成交概率低")
# ]

# IBE执行批量撤单
if cancel_hints:
    order_ids = [hint.order_id for hint in cancel_hints]
    result = await ibe.cancel_batch(order_ids)  # 并发撤销
```

#### 2. 动态TTL撤单（IBE自适应管理）
```python
# IBE内部维护动态TTL
class IBE:
    def __init__(self):
        self.order_ttls = {}  # {order_id: {create_time, ttl_ms, layer}}
        self.market_volatility = 0.0  # 市场波动率
        self.cancel_marks = set()  # 已标记撤单的订单（去重）

    def calculate_dynamic_ttl(self, order_layer, market_speed, queue_position):
        """根据多因素计算动态TTL"""
        base_ttl = {
            'L0': 1000,   # L0基础1秒
            'L1': 3000,   # L1基础3秒
            'L2': 7000    # L2基础7秒
        }[order_layer]

        # 市场流速调整
        if market_speed > 100:  # 高速市场
            ttl = base_ttl * 0.3  # 缩短70%
        elif market_speed > 50:
            ttl = base_ttl * 0.5
        else:
            ttl = base_ttl

        # 队列位置调整
        if queue_position > 100:  # 队列太长
            ttl = min(ttl, 500)  # 最多500ms

        # 极端行情检测
        if self.market_volatility > 0.05:  # 5%波动
            ttl = min(ttl, 200)  # 紧急模式200ms

        return ttl

    async def cleanup_expired(self):
        """高频检查（每100ms）"""
        now = time.time() * 1000  # 毫秒
        expired = []

        for order_id, info in self.order_ttls.items():
            if order_id in self.cancel_marks:
                continue  # 已被SmartOrder标记，跳过避免重复

            if now - info['create_time'] > info['ttl_ms']:
                expired.append(order_id)
                self.cancel_marks.add(order_id)  # 标记避免重复

        if expired:
            await self.cancel_batch(expired)
            logger.info(f"动态TTL撤单: {len(expired)}个订单")
```

### 撤单优先级与去重
1. **紧急撤单**：极端行情，100-200ms内全部撤销
2. **智能撤单**：QPE分析后的精准撤单（标记去重）
3. **动态TTL**：自适应兜底（检查标记避免重复）

### 职责明确
- **OrderOrchestrator**：决定哪些订单该撤（智能分析）
- **IBE**：执行撤单 + 动态TTL管理 + 去重机制
- **协同工作**：通过标记系统避免重复撤单

## 🔄 数据流示例：一个完整的交易循环

```python
# 1. 数据获取
market = dual_active_market_data.get_snapshot()  # {bid: 0.4, ask: 0.401}
balance = user_data_stream.get_balance()         # {DOGE: 1000, USDT: 500}

# 2. 资金分配
allocation = liquidity_envelope.get_allocation()  # L0: 60%, L1: 25%, L2: 15%

# 3. 库存检查
signal = inventory_system.get_rebalance_signal() # {action: BUY, urgency: MEDIUM}

# 4. 智能决策
orders = smart_order_system.generate_orders(
    capital=allocation['L0']['capital'],        # 300 USDT
    market_depth=market['depth'],               # 市场深度
    signal=signal                                # 买入信号
)
# 输出: [Order(50 DOGE), Order(30 DOGE), Order(20 DOGE)...]

# 5. 风控验证
for order in orders:
    if not pessimistic_reservation.reserve(order.id, order.value):
        orders.remove(order)  # 资金不足，移除订单

# 6. 批量执行
result = await ibe.execute_batch(orders)        # 并发执行

# 7. 状态更新（通过回调）
# UserDataStream自动推送余额更新
# SSOTClosedLoop自动释放/确认预扣
# MillisecondResponse自动触发补单

# 8. 监控记录
dashboard.record_execution(result)
ledger.record_event(ExecutionEvent(orders, result))
```

---

## 🛡️ 第7层：对冲引擎层（FAHE - Fast Aggressive Hedging Engine）

### 7.1 DeltaBus（Delta事件总线）
**唯一职责**：Delta变化事件的发布订阅
```python
职责：
✅ 实时发布Delta变化事件
✅ 多订阅者支持
✅ 事件缓冲与批处理
❌ 不执行对冲决策

功能：
- publish_delta(delta_event) → 发布Delta变化
- subscribe(callback) → 订阅Delta事件
- get_current_delta() → 当前总Delta
```

### 7.2 PositionBook（仓位账本）
**唯一职责**：多市场仓位聚合管理
```python
职责：
✅ 现货仓位追踪
✅ 永续仓位追踪
✅ 期货仓位追踪（预留）
✅ 期权仓位追踪（预留）
✅ 跨市场Delta计算
❌ 不执行交易

功能：
- update_spot_position(qty) → 更新现货仓位
- update_perp_position(qty) → 更新永续仓位
- calculate_total_delta() → 计算总Delta
- get_venue_exposure() → 各市场敞口
```

### 7.3 ModeController（模式控制器）
**唯一职责**：根据市场状态动态切换对冲模式
```python
职责：
✅ 市场信号检测（流速、波动率、深度）
✅ 动态模式切换（被动/主动/混合）
✅ 参数自适应调整
❌ 不生成具体订单

三种模式：
1. PASSIVE（被动模式）：
   - 低流速市场（<0.07%/min）
   - 使用限价单排队
   - 成本最优

2. ACTIVE（主动模式）：
   - 高流速市场（>0.15%/min）
   - 使用IOC/市价单
   - 速度最优

3. HYBRID（混合模式）：
   - 中等流速（0.07-0.15%/min）
   - 组合使用
   - 平衡成本与速度

功能：
- detect_market_signals() → {liquidity, volatility, flow_rate}
- select_mode() → PASSIVE/ACTIVE/HYBRID
- get_mode_params() → 模式参数
```

### 7.4 PassivePlanner（被动腿计划器）
**唯一职责**：生成被动对冲订单（限价单）
```python
职责：
✅ 计算最优报价层级
✅ 队列位置估算（QPE）
✅ 订单大小优化
✅ 预期成交时间预测
❌ 不执行订单

功能：
- plan_passive_orders(delta) → [PassiveLeg订单列表]
- estimate_queue_position() → 队列位置
- predict_fill_time() → 预期成交时间
- optimize_order_size() → 最优订单大小
```

### 7.5 ActivePlanner（主动腿计划器）
**唯一职责**：生成主动对冲订单（IOC/市价单）
```python
职责：
✅ 快速清风险方案
✅ 滑点控制
✅ 订单拆分（防冲击）
✅ 场所选择（现货/永续）
❌ 不执行订单

功能：
- plan_active_orders(delta) → [ActiveLeg订单列表]
- calculate_slippage() → 预期滑点
- split_large_order() → 拆分大单
- select_best_venue() → 最优执行场所
```

### 7.6 HedgeRouter（对冲路由器）
**唯一职责**：订单路由与执行管理
```python
职责：
✅ 多场所订单路由
✅ 执行状态追踪
✅ 失败重试机制
✅ 执行结果汇总
❌ 不做对冲决策

支持场所：
- SPOT（现货市场）
- PERP（永续合约）
- FUTURES（交割期货）- 预留
- OPTIONS（期权）- 预留

功能：
- route_order(order, venue) → 路由订单
- track_execution() → 执行状态
- handle_failure() → 失败处理
- aggregate_results() → 结果汇总
```

### 7.7 HedgeGovernor（对冲治理器）
**唯一职责**：风险预算与限制管理
```python
职责：
✅ 对冲预算控制（成交/改价/撤单）
✅ 风险限额管理
✅ 成本分析（费率/资金费/滑点）
✅ 性能指标追踪
❌ 不执行具体对冲

预算类型：
- FILL_BUDGET：成交预算（12/min）
- REPRICE_BUDGET：改价预算（12/min）
- CANCEL_BUDGET：撤单预算（40/min）

功能：
- check_budget(action_type) → bool
- consume_budget(action_type, amount)
- calculate_cost() → {fee, funding, slippage}
- get_performance_metrics() → 性能指标
```

### 7.8 HedgeService（对冲服务主控）
**唯一职责**：协调所有对冲组件
```python
职责：
✅ 组件生命周期管理
✅ Delta监控与触发
✅ 对冲流程编排
✅ 健康状态监控
❌ 不直接执行交易

对冲流程：
1. DeltaBus发布Delta事件
2. ModeController选择模式
3. Planner生成订单计划
4. Governor检查预算
5. Router执行订单
6. PositionBook更新仓位

功能：
- start_hedging() → 启动对冲
- stop_hedging() → 停止对冲
- get_hedge_status() → 对冲状态
- force_rebalance() → 强制再平衡
```

## 🔄 对冲数据流

```
现货Delta变化
     ↓
DeltaBus（事件发布）
     ↓
ModeController（模式选择）
     ↓
  ┌──────────┴──────────┐
  ↓                     ↓
PassivePlanner    ActivePlanner
(限价单方案)       (IOC/市价方案)
  ↓                     ↓
  └──────────┬──────────┘
             ↓
      HedgeGovernor
      (预算检查)
             ↓
       HedgeRouter
    (多场所路由执行)
             ↓
      ┌──────┴──────┐
      ↓             ↓
   现货市场      永续市场
      ↓             ↓
      └──────┬──────┘
             ↓
       PositionBook
       (仓位更新)
```

## 📊 对冲策略特点

### 多市场协同
- **现货主力**：主要做市场所
- **永续对冲**：Delta风险对冲
- **期货预留**：交割合约对冲
- **期权预留**：尾部风险保护

### 智能模式切换
- **平静市场**：被动模式，成本优先
- **活跃市场**：主动模式，速度优先
- **极端市场**：紧急清仓，风险优先

### 成本模型
```python
总成本 = 交易费率 + 资金费率 + 滑点成本 + 队列等待成本

场所选择优先级：
1. 费率最低
2. 流动性最好
3. 资金费率有利
4. 队列位置优势
```

### 风险控制
- **带宽控制**：目标±150 DOGE
- **死区设置**：40 DOGE容忍度
- **预算限制**：防止过度对冲
- **滑点上限**：最大5bp

## 🚀 第8层：实验与生产工程化层（世界级标准）🆕

### 8.1 ParameterServer（参数服务器）🆕
**唯一职责**：统一参数管理与热更新
```python
职责：
✅ 集中管理所有策略参数
✅ 支持热更新（无需重启）
✅ 版本管理与回滚
✅ A/B测试参数分配
❌ 不执行交易逻辑

功能：
- get_params(strategy_id) → 参数集
- update_params(key, value) → 热更新
- rollback_params(version) → 版本回滚
- get_ab_variant(user_id) → A/B测试参数
```

### 8.2 FeatureConsistency（特征一致性引擎）🆕
**唯一职责**：保证离线训练与在线推理特征一致
```python
职责：
✅ 特征工程统一管道
✅ 离线/在线特征校验
✅ 特征版本控制
✅ 特征漂移检测
❌ 不做策略决策

功能：
- compute_features(data) → 标准化特征
- validate_consistency() → 一致性检查
- detect_drift() → 特征漂移告警
```

### 8.3 ReplaySimulator（重放仿真器）🆕
**唯一职责**：基于历史事件的精确重放
```python
职责：
✅ 事件溯源重放
✅ 策略回测验证
✅ 故障场景重现
✅ 性能基准测试
❌ 不影响生产交易

功能：
- replay_events(time_range) → 重放历史
- backtest_strategy(params) → 回测结果
- simulate_failure(scenario) → 故障模拟
- benchmark_performance() → 性能报告

与InstitutionalEventLedger集成：
- EventLedger记录 → ReplaySimulator重放
- 完整的事件溯源能力
```

### 8.4 ShadowTrading（影子交易系统）🆕
**唯一职责**：零风险实盘验证
```python
职责：
✅ 接收实时数据
✅ 模拟下单（不发送到交易所）
✅ 追踪虚拟PnL
✅ 对比真实vs影子性能
❌ 不执行真实交易

功能：
- shadow_order(order) → 记录但不执行
- calculate_virtual_pnl() → 虚拟盈亏
- compare_performance() → 对比分析
- detect_divergence() → 偏离告警

运行模式：
1. 完全影子：100%流量复制，0%执行
2. 验证模式：追踪但不干预
3. 告警模式：发现问题立即通知
```

### 8.5 CanaryDeployment（金丝雀放量系统）🆕
**唯一职责**：渐进式新策略上线
```python
职责：
✅ 流量分配控制（1%→5%→10%→50%→100%）
✅ 风险指标监控
✅ 自动回滚机制
✅ 逐步放量决策
❌ 不做策略开发

放量阶段：
1. 金丝雀（1-5%）：小流量验证
2. 灰度（10-20%）：扩大测试
3. 蓝绿（50%）：对比测试
4. 全量（100%）：完全切换

功能：
- set_traffic_ratio(0.05) → 5%流量
- monitor_metrics() → 监控指标
- auto_rollback() → 自动回滚
- promote_stage() → 晋级下一阶段

安全机制：
- 损失上限：单日最大损失0.1%
- 自动熔断：异常指标触发回滚
- 人工审核：关键阶段需确认
```

### 8.6 EventSourcingEngine（事件溯源引擎）🆕
**唯一职责**：完整事件流管理
```python
职责：
✅ 事件存储与索引
✅ 状态重建能力
✅ 时间旅行调试
✅ 审计追踪
❌ 不修改业务逻辑

功能：
- store_event(event) → 持久化存储
- rebuild_state(timestamp) → 重建历史状态
- time_travel_debug() → 时间旅行调试
- audit_trail() → 完整审计链

与现有组件集成：
- InstitutionalEventLedger：提供事件
- EventSourcingEngine：存储与重建
- ReplaySimulator：基于事件重放
```

## 🔄 工程化数据流

```
生产环境 ────┬──→ 主交易系统（100%真实交易）
             │
             ├──→ ShadowTrading（100%模拟）
             │      ↓
             │    性能对比分析
             │
             ├──→ CanaryDeployment（1-5%真实）
             │      ↓
             │    风险评估 → 放量/回滚
             │
             └──→ EventSourcingEngine
                    ↓
                  历史事件存储
                    ↓
                  ReplaySimulator
                    ↓
                  策略优化/调试
```

---

## ⚠️ 重要提醒：需要清理的重叠

### TurboConnector需要拆分
当前TurboConnector有28个方法，50%功能重叠：
- 市场数据功能 → 移除（使用DualActiveMarketData）
- 账户查询功能 → 移除（使用UserDataStream）
- WebSocket管理 → 移除（由专门组件管理）
- 只保留交易执行 → 重构为CoreTradeConnector（5个方法）

### 职责必须清晰
- 数据组件不执行交易
- 执行组件不做决策
- 决策组件不管数据获取
- 每个组件专注自己的领域

---

## 🎯 总结：清晰的职责边界

| 层级 | 组件 | 唯一职责 | 不该做的事 |
|------|------|---------|-----------|
| 时间层 | TimeAuthority | 时间同步 | 业务逻辑、数据处理 |
| 时间层 | LatencyTracker | 延迟监控 | 干预执行、修改时间 |
| 数据层 | DualActiveMarketData | 市场数据 | 账户数据、交易执行 |
| 数据层 | UserDataStream | 账户数据 | 市场数据、交易执行 |
| 风控层 | PessimisticReservation | 资金预扣 | 订单决策、执行 |
| 风控层 | SSOTClosedLoop | 资金一致性 | 交易策略、执行 |
| 决策层 | LiquidityEnvelope | 资金分配 | 订单大小、执行 |
| 决策层 | QuotePricingService + OrderOrchestrator | 定价+订单生成 | 职责分离、降耦 |
| 决策层 | InventorySystem | 库存平衡 | 订单生成、执行 |
| 执行层 | IBE | 批量执行+动态TTL | 决策、市场分析 |
| 执行层 | EmergencyKillSwitch | 紧急停止 | 等待确认、依赖决策 |
| 执行层 | APIRateLimiter | 限流控制 | 执行交易、做决策 |
| 执行层 | CoreTradeConnector | 交易通信 | 数据获取、决策、限流 |
| 监控层 | ObservabilityDashboard | 健康监控 | 直接执行（通过KillSwitch） |

每个组件像乐高积木，职责单一，组合起来构建世界级交易系统！