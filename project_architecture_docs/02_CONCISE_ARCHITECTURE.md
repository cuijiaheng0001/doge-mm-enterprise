# 企业架构简洁版 - 层级功能目录

## Layer -1: 系统调优基线层

### -1.1 NetworkHostTuningBaseline
- CPU亲和性绑定
- NIC多队列绑定
- 忙轮询优化
- HugePages内存
- NUMA拓扑优化
- 内核绕过网络栈

### -1.2 PTSyncService
- PTP/IEEE1588硬件时间戳
- GPS/原子钟时间源
- Grandmaster Clock配置
- 硬件时间戳卸载

## Layer 0: 品种主数据层

### 0.0 InstrumentMaster
- 撮合规则管理
- 价格精度管理
- 费率层级管理
- 交易状态管理
- 交易时段管理
- 合约乘数管理
- 返佣层级管理
- 版本化热更新

### 0.1 SigningService
- API密钥安全存储
- 签名请求代理
- 短期访问票据
- 签名审计日志
- 密钥轮转管理

### 0.2 ChangeGuard
- 参数变更双人复核
- 版本发布审批
- 金丝雀放量审批
- 变更冻结窗口
- 变更回滚机制

### 0.3 LightweightFailoverManager
- 服务健康检查
- 自动重启切换
- 状态快照恢复
- 双服务器备份

### 0.4 SessionStateManager
- 交易会话持久化
- 订单状态快照
- 用户连接维护
- 会话热恢复

### 0.5 TimeAuthority
- NTP/PTP时间同步
- 硬件时间戳获取
- Event Time分离
- 时钟漂移校正

### 0.6 LatencyTracker
- 全链路延迟监控
- 组件间延迟计算
- 性能瓶颈识别

## Layer 1: 数据层

### 1.0 市场数据子系统

#### 1.0.1 MultiSourceDataGovernance
- 主所直连WebSocket
- 辅路聚合源
- 录播回放源
- 源间质量对比
- 陈旧数据全撤

#### 1.0.2 IncrementalStreamProcessor
- 增量订单簿构建
- 序列号严格校验
- 增量流接口
- 流式数据推送

#### 1.0.3 FeedArbiter
- 多源质量评估
- 最优源选择
- 异常源隔离

#### 1.0.4 Sequencer
- 交易所时间戳排序
- 乱序数据处理
- 序列连续性检查

#### 1.0.5 GapFillEngine
- 实时缺口检测
- 历史数据回补
- 插值算法填充

#### 1.0.6 StaleTick Gate
- 时间戳新鲜度检查
- 价格合理性验证
- 异常成交量检测

#### 1.0.7 L3 BookBuilder
- 增量深度更新
- 订单ID级别信息
- 快照增量合并
- 订单簿一致性

### 1.1 DualActiveMarketData
- 经处理市场数据
- 订单簿深度
- 最新成交价
- 双活备份

### 1.2 UserDataStream
- 实时余额推送
- 实时订单状态
- listenKey管理

### 1.3 DropCopyIngestor
- 独立成交流接入
- 订单状态抄送
- 时间锚点校验
- 订单键匹配

### 1.4 StateReconciler
- 本地vs交易所对比
- 状态不一致纠正
- 断连场景处理
- 定期事件对账

## Layer 2: 风控层

### 2.0 风控服务器子系统

#### 2.0.1 CentralizedRiskServer
- 四维限额管理
- 自成交防控STP
- Token Bucket限流
- 前置白名单检查
- 独立进程部署

#### 2.0.2 TokenBucketLimiter
- 策略级别限流
- 账户级别限流
- 交易所级别限流
- 品种级别限流

#### 2.0.3 SelfTradePreventionEngine
- 同账户对敲检测
- 自成交订单阻止
- 多策略协调
- 历史自成交分析

#### 2.0.4 PreTradeGuardrail
- 动态价格围栏
- Fat-Finger检测
- OTR治理监控
- 取消率限制

### 2.1 PessimisticReservationModel
- 下单前预扣资金
- 成交后确认扣除
- 失败后释放资金

### 2.2 SSOTReservationClosedLoop
- 预扣闭环维护
- 订单状态机管理
- 幂等性保证
- 30秒超时释放
- 状态修正分支

### 2.3 InstitutionalEventLedger
- 所有交易事件记录
- 不可篡改账本
- 审计追踪提供

## Layer 3: 决策层

### 3.1 LiquidityEnvelope
- 每层资金分配
- 库存动态调整

### 3.2 QuotePricingService
- 最优报价计算
- 价差动态调整
- 库存偏斜调价
- 波动率适应调价

### 3.3 OrderOrchestrator
- 根据报价创建订单
- 智能拆单策略
- 智能撤单决策
- 队列位置优化

### 3.4 ThreeDomainInventorySystem
- 库存偏离监控
- 三时域调整策略
- 再平衡信号触发

## Layer 4: 执行层

### 4.1 IBE (Intelligent Batch Executor)
- 批量并发发送
- 批量并发撤销
- 动态TTL管理
- 失败重试机制

### 4.2 EmergencyKillSwitch
- 一键撤销所有
- 停止新订单创建
- 绕过决策层
- 多触发源监控

### 4.3 MillisecondResponseSystem
- 成交后立即补单
- 优先级队列管理
- 事件驱动架构

### 4.4 APIRateLimiter
- 全局API权重管理
- 按类型配额分配
- 动态限流调整
- 多级熔断保护

### 4.5 CoreTradeConnector
- 发送订单到交易所
- 取消订单
- 获取交易规则
- 测试订单
- 获取品种过滤器

## Layer 5: 做市质量分析层

### 5.1 ToxicityMonitor
- VPIN毒性计算
- 订单流毒性检测
- Price Impact识别
- 市场结构变化检测
- Adverse Selection量化

### 5.2 QuoteQualityService
- Microprice偏差分析
- Fair-Value模型计算
- Quote Quality评分
- Cancel-to-Fill优化
- Inventory-Cost建模

### 5.3 MarketQualityDashboard
- 实时质量面板
- 毒性告警建议
- 策略效果分析
- 盈利归因分析

## Layer 6: 监控层

### 6.1 ObservabilityDashboard
- 收集组件指标
- 计算健康分数
- 触发告警
- 8大核心指标

## Layer 7: 对冲引擎层

### 7.1 DeltaBus
- Delta事件发布订阅
- 多订阅者支持
- 事件缓冲批处理

### 7.2 PositionBook
- 现货仓位追踪
- 永续仓位追踪
- 跨市场Delta计算
- 各市场敞口

### 7.3 ModeController
- 市场信号检测
- 动态模式切换
- 参数自适应调整

### 7.4 PassivePlanner
- 最优报价层级计算
- 队列位置估算QPE
- 订单大小优化
- 成交时间预测

### 7.5 ActivePlanner
- 快速清风险方案
- 滑点控制
- 订单拆分
- 场所选择

### 7.6 HedgeRouter
- 多场所订单路由
- 执行状态追踪
- 失败重试机制
- 执行结果汇总

### 7.7 HedgeGovernor
- 对冲预算控制
- 风险限额管理
- 成本分析
- 性能指标追踪

### 7.8 HedgeService
- 组件生命周期管理
- Delta监控触发
- 对冲流程编排
- 健康状态监控

## Layer 8: 实验与生产工程化层

### 8.1 ParameterServer
- 集中参数管理
- 支持热更新
- 版本管理回滚
- A/B测试分配

### 8.2 FeatureConsistency
- 特征工程统一
- 离线在线校验
- 特征版本控制
- 特征漂移检测

### 8.3 ReplaySimulator
- 事件溯源重放
- 策略回测验证
- 故障场景重现
- 性能基准测试

### 8.4 ShadowTrading
- 接收实时数据
- 模拟下单不执行
- 追踪虚拟PnL
- 对比真实性能

### 8.5 CanaryDeployment
- 流量分配控制
- 风险指标监控
- 自动回滚机制
- 逐步放量决策

### 8.6 EventSourcingEngine
- 事件存储索引
- 状态重建能力
- 时间旅行调试
- 审计追踪

---

## 📊 层级统计

| 层级 | 子模块数 | 主要职责 |
|------|---------|----------|
| Layer -1 | 2个 | 硬件与网络基础优化 |
| Layer 0 | 7个 | 基础设施与安全服务 |
| Layer 1 | 11个 | 数据获取与处理 |
| Layer 2 | 7个 | 风险控制与资金管理 |
| Layer 3 | 4个 | 策略决策与智能定价 |
| Layer 4 | 5个 | 高速执行与限流控制 |
| Layer 5 | 3个 | 做市质量分析 |
| Layer 6 | 1个 | 系统健康监控 |
| Layer 7 | 8个 | 对冲引擎管理 |
| Layer 8 | 6个 | 实验与生产工程化 |

**总计**: 10层架构，54个核心模块

---

*版本: 1.0.0*
*更新时间: 2025-01-19*