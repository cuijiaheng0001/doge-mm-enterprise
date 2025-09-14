# Doge Market Maker - Enterprise Trading System

企业级交易系统，采用世界级架构设计。

## 系统架构
- **world_class_maker**: 企业级交易应用
- **packages**: 共享组件库
  - **exec**: SSOT预扣闭环系统
  - **connectors**: TurboConnector V2 (待重构)
  - **monitoring**: 监控组件
  - **utils**: 工具函数

## 核心特性
- SSOT (Single Source of Truth) 预扣闭环
- 5层企业级架构
- 机构级风险管理
- order_execution_engine (Mock测试引擎)

## 快速启动
```bash
cd world_class_maker
python -m main
```

## 重构计划
- [ ] TurboConnector 架构重设计
- [ ] 组件职责明确分离
- [ ] 性能优化与扩展

## 团队职责
- 重新设计系统架构
- 实现世界级交易标准
- 构建可扩展框架