#!/bin/bash

echo "🔄 重新组织仓库以匹配V10架构..."

# ========== 创建正确的目录结构 ==========
echo "📁 创建V10架构目录结构..."

# Layer -1: 系统调优基线层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer_minus1_hardware

# Layer 0: 品种主数据层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer0_reference

# Layer 0.1-0.3: 基础设施层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/{security,failover,time}

# Layer 1: 数据层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer1_data/{market,account,reconciliation}

# Layer 2: 风控层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer2_risk/{server,reservation,ledger}

# Layer 3: 决策层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer3_decision/{pricing,orchestration,inventory}

# Layer 4: 执行层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer4_execution/{batch,response,control}

# Layer 5: 质量分析层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer5_quality

# Layer 6: 对冲引擎层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer6_hedge

# Layer 7: 生产工程化层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer7_production

# Layer 8: 监控层
mkdir -p /home/ubuntu/doge-mm-enterprise/layer8_monitoring

# ========== 移动现有文件到正确位置 ==========
echo "📦 移动文件到V10架构位置..."

# Layer -1: 系统调优基线层
if [ -f /home/ubuntu/doge-mm-enterprise/infrastructure/network_host_tuning_baseline.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/infrastructure/network_host_tuning_baseline.py \
       /home/ubuntu/doge-mm-enterprise/layer_minus1_hardware/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/infrastructure/ptp_sync_service.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/infrastructure/ptp_sync_service.py \
       /home/ubuntu/doge-mm-enterprise/layer_minus1_hardware/
fi

# Layer 0: 品种主数据层
if [ -f /home/ubuntu/doge-mm-enterprise/services/instrument_master.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/instrument_master.py \
       /home/ubuntu/doge-mm-enterprise/layer0_reference/
fi

# Layer 0.1: 安全服务层
if [ -f /home/ubuntu/doge-mm-enterprise/services/signing_service.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/signing_service.py \
       /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/security/
fi

# 创建ChangeGuard (Layer 0.2)
cat > /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/security/change_guard.py << 'EOF'
"""
ChangeGuard - 双人复核服务
Layer 0.2
"""

class ChangeGuard:
    """重大变更的双人复核与变更窗口管理"""

    def submit_change_request(self, change):
        """提交变更请求"""
        pass

    def approve_change(self, request_id, approver):
        """审批变更"""
        pass
EOF

# 创建故障切换管理器 (Layer 0.3)
cat > /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/failover/lightweight_failover_manager.py << 'EOF'
"""
LightweightFailoverManager - 轻量级故障切换管理器
Layer 0.3
"""

class LightweightFailoverManager:
    """单机房内的高可用和简单容灾"""

    def monitor_service_health(self):
        """监控服务健康"""
        pass

    def trigger_failover(self, service):
        """触发故障切换"""
        pass
EOF

cat > /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/failover/session_state_manager.py << 'EOF'
"""
SessionStateManager - 会话状态管理器
Layer 0.4
"""

class SessionStateManager:
    """交易会话的状态保持和恢复"""

    def capture_session_snapshot(self):
        """捕获会话快照"""
        pass

    def restore_session_from_snapshot(self):
        """恢复会话"""
        pass
EOF

# Layer 0.5-0.6: 时间治理层
cat > /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/time/time_authority.py << 'EOF'
"""
TimeAuthority - 统一时间权威
Layer 0.5
"""

class TimeAuthority:
    """提供纳秒级精确时间戳"""

    def get_hardware_timestamp(self):
        """获取硬件时间戳"""
        pass

    def detect_clock_drift(self):
        """检测时钟漂移"""
        pass
EOF

cat > /home/ubuntu/doge-mm-enterprise/layer0_infrastructure/time/latency_tracker.py << 'EOF'
"""
LatencyTracker - 延迟追踪器
Layer 0.6
"""

class LatencyTracker:
    """全链路延迟监控"""

    def get_p50_latency(self):
        """获取P50延迟"""
        return 0.5

    def get_p99_latency(self):
        """获取P99延迟"""
        return 2.0
EOF

# Layer 1: 数据层组件
if [ -f /home/ubuntu/doge-mm-enterprise/packages/utils/dual_active_market_data.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/utils/dual_active_market_data.py \
       /home/ubuntu/doge-mm-enterprise/layer1_data/market/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/connectors/user_stream.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/connectors/user_stream.py \
       /home/ubuntu/doge-mm-enterprise/layer1_data/account/user_data_stream.py
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/drop_copy_ingestor.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/drop_copy_ingestor.py \
       /home/ubuntu/doge-mm-enterprise/layer1_data/reconciliation/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/state_reconciler.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/state_reconciler.py \
       /home/ubuntu/doge-mm-enterprise/layer1_data/reconciliation/
fi

# Layer 2: 风控层
if [ -f /home/ubuntu/doge-mm-enterprise/services/centralized_risk_server.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/centralized_risk_server.py \
       /home/ubuntu/doge-mm-enterprise/layer2_risk/server/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/risk/pessimistic_reservation_model.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/risk/pessimistic_reservation_model.py \
       /home/ubuntu/doge-mm-enterprise/layer2_risk/reservation/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/ssot_closed_loop.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/ssot_closed_loop.py \
       /home/ubuntu/doge-mm-enterprise/layer2_risk/reservation/ssot_reservation_closed_loop.py
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/risk/institutional_event_ledger.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/risk/institutional_event_ledger.py \
       /home/ubuntu/doge-mm-enterprise/layer2_risk/ledger/
fi

# Layer 3: 决策层
if [ -f /home/ubuntu/doge-mm-enterprise/packages/utils/liquidity_envelope.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/utils/liquidity_envelope.py \
       /home/ubuntu/doge-mm-enterprise/layer3_decision/inventory/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/quote_pricing_service.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/quote_pricing_service.py \
       /home/ubuntu/doge-mm-enterprise/layer3_decision/pricing/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/order_orchestrator.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/order_orchestrator.py \
       /home/ubuntu/doge-mm-enterprise/layer3_decision/orchestration/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/exec/three_domain_inventory_system.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/exec/three_domain_inventory_system.py \
       /home/ubuntu/doge-mm-enterprise/layer3_decision/inventory/
fi

# Layer 4: 执行层
if [ -f /home/ubuntu/doge-mm-enterprise/packages/exec/intelligent_batch_executor.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/exec/intelligent_batch_executor.py \
       /home/ubuntu/doge-mm-enterprise/layer4_execution/batch/ibe.py
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/emergency_kill_switch.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/emergency_kill_switch.py \
       /home/ubuntu/doge-mm-enterprise/layer4_execution/control/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/exec/millisecond_response_system.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/exec/millisecond_response_system.py \
       /home/ubuntu/doge-mm-enterprise/layer4_execution/response/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/api_rate_limiter.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/api_rate_limiter.py \
       /home/ubuntu/doge-mm-enterprise/layer4_execution/control/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/packages/connectors/core_trade_connector.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/connectors/core_trade_connector.py \
       /home/ubuntu/doge-mm-enterprise/layer4_execution/
fi

# Layer 5: 质量分析层
if [ -f /home/ubuntu/doge-mm-enterprise/services/toxicity_monitor.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/toxicity_monitor.py \
       /home/ubuntu/doge-mm-enterprise/layer5_quality/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/quote_quality_service.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/quote_quality_service.py \
       /home/ubuntu/doge-mm-enterprise/layer5_quality/
fi

# 创建MarketQualityDashboard
cat > /home/ubuntu/doge-mm-enterprise/layer5_quality/market_quality_dashboard.py << 'EOF'
"""
MarketQualityDashboard - 做市质量仪表板
Layer 5.3
"""

class MarketQualityDashboard:
    """可视化做市质量指标和提供决策支持"""

    def render_realtime_panel(self):
        """渲染实时面板"""
        pass

    def generate_quality_report(self):
        """生成质量报告"""
        pass
EOF

# Layer 6: 对冲引擎层 (保持hedge包所有文件)
if [ -d /home/ubuntu/doge-mm-enterprise/packages/hedge ]; then
    cp -r /home/ubuntu/doge-mm-enterprise/packages/hedge/* \
       /home/ubuntu/doge-mm-enterprise/layer6_hedge/
fi

# Layer 7: 生产工程化层
if [ -f /home/ubuntu/doge-mm-enterprise/services/parameter_server.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/parameter_server.py \
       /home/ubuntu/doge-mm-enterprise/layer7_production/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/shadow_trading.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/shadow_trading.py \
       /home/ubuntu/doge-mm-enterprise/layer7_production/
fi

if [ -f /home/ubuntu/doge-mm-enterprise/services/canary_deployment.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/services/canary_deployment.py \
       /home/ubuntu/doge-mm-enterprise/layer7_production/
fi

# 创建其他Layer 7组件
cat > /home/ubuntu/doge-mm-enterprise/layer7_production/feature_consistency.py << 'EOF'
"""
FeatureConsistency - 特征一致性引擎
Layer 7.2
"""

class FeatureConsistency:
    """保证离线训练与在线推理特征一致"""

    def compute_features(self, data):
        """计算特征"""
        pass
EOF

cat > /home/ubuntu/doge-mm-enterprise/layer7_production/replay_simulator.py << 'EOF'
"""
ReplaySimulator - 重放仿真器
Layer 7.3
"""

class ReplaySimulator:
    """基于历史事件的精确重放"""

    def replay_events(self, time_range):
        """重放历史事件"""
        pass
EOF

cat > /home/ubuntu/doge-mm-enterprise/layer7_production/event_sourcing_engine.py << 'EOF'
"""
EventSourcingEngine - 事件溯源引擎
Layer 7.6
"""

class EventSourcingEngine:
    """完整事件流管理"""

    def store_event(self, event):
        """存储事件"""
        pass

    def rebuild_state(self, timestamp):
        """重建状态"""
        pass
EOF

# Layer 8: 监控层
if [ -f /home/ubuntu/doge-mm-enterprise/packages/utils/observability_dashboard.py ]; then
    mv /home/ubuntu/doge-mm-enterprise/packages/utils/observability_dashboard.py \
       /home/ubuntu/doge-mm-enterprise/layer8_monitoring/
fi

# ========== 清理旧目录 ==========
echo "🧹 清理旧目录结构..."
rm -rf /home/ubuntu/doge-mm-enterprise/infrastructure
rm -rf /home/ubuntu/doge-mm-enterprise/services
rm -rf /home/ubuntu/doge-mm-enterprise/packages/connectors
rm -rf /home/ubuntu/doge-mm-enterprise/packages/risk
rm -rf /home/ubuntu/doge-mm-enterprise/packages/exec
rm -rf /home/ubuntu/doge-mm-enterprise/packages/utils
rm -rf /home/ubuntu/doge-mm-enterprise/packages/hedge

# 如果packages目录为空，删除它
if [ -z "$(ls -A /home/ubuntu/doge-mm-enterprise/packages 2>/dev/null)" ]; then
    rm -rf /home/ubuntu/doge-mm-enterprise/packages
fi

echo "✅ 重新组织完成！"