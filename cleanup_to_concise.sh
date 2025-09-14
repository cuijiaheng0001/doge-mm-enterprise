#!/bin/bash

# 清理Enterprise仓库，只保留CONCISE架构中定义的模块

echo "🧹 开始清理Enterprise仓库..."

# ========== EXEC包清理 ==========
echo "清理exec包..."

# 保留的exec模块（根据CONCISE架构）
KEEP_EXEC=(
    "intelligent_batch_executor.py"           # 4.1 IBE
    "millisecond_response_system.py"          # 4.3 毫秒响应
    "three_domain_inventory_system.py"        # 3.4 三域库存
)

# 删除不在CONCISE中的exec模块
cd /home/ubuntu/doge-mm-enterprise/packages/exec/
for file in *.py; do
    if [[ ! " ${KEEP_EXEC[@]} " =~ " ${file} " ]] && [ "$file" != "__init__.py" ]; then
        echo "  删除: $file"
        rm -f "$file"
    fi
done

# ========== RISK包清理 ==========
echo "清理risk包..."

# 保留的risk模块
KEEP_RISK=(
    "pessimistic_reservation_model.py"        # 2.1 悲观预扣
    "institutional_event_ledger.py"           # 2.3 事件账本
    "budget_governor.py"                      # 预算治理
)

cd /home/ubuntu/doge-mm-enterprise/packages/risk/
for file in *.py; do
    if [[ ! " ${KEEP_RISK[@]} " =~ " ${file} " ]] && [ "$file" != "__init__.py" ]; then
        echo "  删除: $file"
        rm -f "$file"
    fi
done

# ========== UTILS包清理 ==========
echo "清理utils包..."

# 保留的utils模块
KEEP_UTILS=(
    "dual_active_market_data.py"              # 1.1 双活市场数据
    "observability_dashboard.py"              # 6.1 监控仪表板
    "liquidity_envelope.py"                   # 3.1 流动性包络
)

cd /home/ubuntu/doge-mm-enterprise/packages/utils/
for file in *.py; do
    if [[ ! " ${KEEP_UTILS[@]} " =~ " ${file} " ]] && [ "$file" != "__init__.py" ]; then
        echo "  删除: $file"
        rm -f "$file"
    fi
done

# ========== CONNECTORS包清理 ==========
echo "清理connectors包..."

# 保留的connectors模块
KEEP_CONNECTORS=(
    "core_trade_connector.py"                 # 4.5 核心交易连接器
    "user_stream.py"                          # 1.2 用户数据流
)

cd /home/ubuntu/doge-mm-enterprise/packages/connectors/
for file in *.py; do
    if [[ ! " ${KEEP_CONNECTORS[@]} " =~ " ${file} " ]] && [ "$file" != "__init__.py" ]; then
        echo "  删除: $file"
        rm -f "$file"
    fi
done

# ========== HEDGE包保持不变（全部需要） ==========
echo "hedge包保持不变（全部都在CONCISE架构中）"

echo "✅ 清理完成！"