"""
Phase 6 步骤7: 可观测性仪表盘系统
实现8项核心指标监控 + 红线预警 + 自保护策略
"""

import time
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
import asyncio
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class AlertLevel(Enum):
    GREEN = "GREEN"      # 正常运行
    YELLOW = "YELLOW"    # 注意监控  
    ORANGE = "ORANGE"    # 需要干预
    RED = "RED"          # 立即停止

@dataclass
class MetricThreshold:
    """指标阈值配置"""
    green_max: float
    yellow_max: float
    orange_max: float
    red_max: float = float('inf')

@dataclass
class SystemMetrics:
    """系统实时指标"""
    timestamp: float = field(default_factory=time.time)
    
    # 核心8指标
    fill_to_repost_latency_p99: float = 0.0    # 指标1: 成交响应延迟P99 (ms)
    order_success_rate: float = 1.0            # 指标2: 订单成功率 (%)
    inventory_skew_ratio: float = 0.0          # 指标3: 库存偏斜比率 (%)
    spread_capture_efficiency: float = 0.0     # 指标4: 价差捕获效率 (%)
    liquidity_provision_score: float = 0.0    # 指标5: 流动性供给评分
    risk_weighted_exposure: float = 0.0        # 指标6: 风险加权敞口 ($)
    api_weight_utilization: float = 0.0        # 指标7: API权重使用率 (%)
    system_health_score: float = 1.0           # 指标8: 系统健康度评分

class ObservabilityDashboard:
    """可观测性仪表盘系统"""
    
    def __init__(self):
        self.metrics_history = deque(maxlen=3600)  # 1小时历史数据
        self.current_metrics = SystemMetrics()
        self.alert_level = AlertLevel.GREEN
        self.alert_history = deque(maxlen=100)
        
        # 红线阈值配置
        self.thresholds = {
            'fill_to_repost_latency_p99': MetricThreshold(50, 100, 200, 500),      # ms
            'order_success_rate': MetricThreshold(0.98, 0.95, 0.90, 0.80),        # %
            'inventory_skew_ratio': MetricThreshold(0.05, 0.10, 0.20, 0.35),      # %
            'spread_capture_efficiency': MetricThreshold(0.60, 0.45, 0.30, 0.15), # %
            'liquidity_provision_score': MetricThreshold(0.80, 0.65, 0.45, 0.25), # score
            'risk_weighted_exposure': MetricThreshold(5000, 10000, 20000, 35000), # $
            'api_weight_utilization': MetricThreshold(0.60, 0.75, 0.85, 0.95),    # %
            'system_health_score': MetricThreshold(0.85, 0.70, 0.55, 0.35)        # score
        }
        
        # 自保护策略状态
        self.protection_active = False
        self.protection_reason = ""
        self.protection_start_time = 0.0
        
        logger.info("[ObservabilityDashboard] 可观测性仪表盘初始化完成")

    def update_fill_latency(self, latency_ms: float):
        """更新成交响应延迟指标"""
        if not hasattr(self, '_latency_samples'):
            self._latency_samples = deque(maxlen=100)
        
        self._latency_samples.append(latency_ms)
        if len(self._latency_samples) >= 10:
            sorted_samples = sorted(self._latency_samples)
            p99_index = int(len(sorted_samples) * 0.99)
            self.current_metrics.fill_to_repost_latency_p99 = sorted_samples[p99_index]

    def update_order_success_rate(self, success_count: int, total_count: int):
        """更新订单成功率指标"""
        if total_count > 0:
            self.current_metrics.order_success_rate = success_count / total_count

    def update_inventory_skew(self, current_inventory: Decimal, target_inventory: Decimal, 
                             total_capital: Decimal):
        """更新库存偏斜比率指标"""
        if total_capital > 0:
            skew_amount = abs(current_inventory - target_inventory)
            self.current_metrics.inventory_skew_ratio = float(skew_amount / total_capital)

    def update_spread_capture(self, captured_spread: Decimal, theoretical_spread: Decimal):
        """更新价差捕获效率指标"""
        if theoretical_spread > 0:
            efficiency = captured_spread / theoretical_spread
            self.current_metrics.spread_capture_efficiency = float(efficiency)

    def update_liquidity_score(self, bid_depth: Decimal, ask_depth: Decimal, 
                              market_impact: float, uptime_ratio: float):
        """更新流动性供给评分指标"""
        # 综合评分：深度权重40% + 市场影响30% + 在线时间30%
        depth_score = min(1.0, float(bid_depth + ask_depth) / 1000)  # 假设1000为满分深度
        impact_score = max(0.0, 1.0 - market_impact)  # 市场影响越小越好
        
        liquidity_score = (depth_score * 0.4 + 
                          impact_score * 0.3 + 
                          uptime_ratio * 0.3)
        
        self.current_metrics.liquidity_provision_score = liquidity_score

    def update_risk_exposure(self, position_value: Decimal, var_estimate: Decimal):
        """更新风险加权敞口指标"""
        risk_weighted = float(abs(position_value) + var_estimate * 2)  # VaR乘数2
        self.current_metrics.risk_weighted_exposure = risk_weighted

    def update_api_utilization(self, current_weight: int, max_weight: int):
        """更新API权重使用率指标"""
        if max_weight > 0:
            self.current_metrics.api_weight_utilization = current_weight / max_weight

    def calculate_system_health(self):
        """计算系统综合健康度评分"""
        metrics = self.current_metrics
        
        # 8项指标权重分配
        weights = {
            'fill_to_repost_latency_p99': 0.15,    # 响应速度15%
            'order_success_rate': 0.20,            # 订单成功率20% 
            'inventory_skew_ratio': 0.10,          # 库存管理10%
            'spread_capture_efficiency': 0.15,     # 盈利能力15%
            'liquidity_provision_score': 0.15,    # 流动性供给15%
            'risk_weighted_exposure': 0.10,        # 风险控制10%
            'api_weight_utilization': 0.10,        # 资源使用10%
            'system_health_score': 0.05            # 递归权重5%
        }
        
        # 计算各指标标准化得分 (0-1)
        scores = {}
        for metric_name, weight in weights.items():
            if metric_name == 'system_health_score':
                continue
                
            current_value = getattr(metrics, metric_name)
            threshold = self.thresholds[metric_name]
            
            # 标准化到0-1分数 (根据指标特性正向或反向)
            if metric_name in ['order_success_rate', 'spread_capture_efficiency', 
                              'liquidity_provision_score']:
                # 正向指标：越大越好
                if current_value >= threshold.green_max:
                    score = 1.0
                elif current_value <= threshold.red_max:
                    score = 0.0
                else:
                    # 线性映射
                    score = (current_value - threshold.red_max) / (threshold.green_max - threshold.red_max)
            else:
                # 反向指标：越小越好
                if current_value <= threshold.green_max:
                    score = 1.0
                elif current_value >= threshold.red_max:
                    score = 0.0
                else:
                    # 线性映射
                    score = 1.0 - (current_value - threshold.green_max) / (threshold.red_max - threshold.green_max)
            
            scores[metric_name] = max(0.0, min(1.0, score))
        
        # 加权平均计算综合健康度
        total_score = sum(score * weights[metric_name] 
                         for metric_name, score in scores.items())
        
        self.current_metrics.system_health_score = total_score
        return total_score

    def evaluate_alert_level(self) -> AlertLevel:
        """评估当前告警级别"""
        metrics = self.current_metrics
        max_alert_level = AlertLevel.GREEN
        
        for metric_name, threshold in self.thresholds.items():
            current_value = getattr(metrics, metric_name)
            
            # 判断告警级别 (根据指标特性调整判断逻辑)
            if metric_name in ['order_success_rate', 'spread_capture_efficiency', 
                              'liquidity_provision_score', 'system_health_score']:
                # 正向指标：值太小触发告警
                if current_value <= threshold.red_max:
                    max_alert_level = max(max_alert_level, AlertLevel.RED, key=lambda x: x.value)
                elif current_value <= threshold.orange_max:
                    max_alert_level = max(max_alert_level, AlertLevel.ORANGE, key=lambda x: x.value)
                elif current_value <= threshold.yellow_max:
                    max_alert_level = max(max_alert_level, AlertLevel.YELLOW, key=lambda x: x.value)
            else:
                # 反向指标：值太大触发告警
                if current_value >= threshold.red_max:
                    max_alert_level = max(max_alert_level, AlertLevel.RED, key=lambda x: x.value)
                elif current_value >= threshold.orange_max:
                    max_alert_level = max(max_alert_level, AlertLevel.ORANGE, key=lambda x: x.value)
                elif current_value >= threshold.yellow_max:
                    max_alert_level = max(max_alert_level, AlertLevel.YELLOW, key=lambda x: x.value)
        
        self.alert_level = max_alert_level
        return max_alert_level

    def trigger_protection_strategy(self, reason: str):
        """触发自保护策略"""
        if not self.protection_active:
            self.protection_active = True
            self.protection_reason = reason
            self.protection_start_time = time.time()
            
            logger.critical(f"[ObservabilityDashboard] 🚨 自保护策略已激活: {reason}")
            
            # 记录告警历史
            alert_record = {
                'timestamp': time.time(),
                'level': 'PROTECTION_ACTIVATED',
                'reason': reason,
                'metrics_snapshot': self.current_metrics
            }
            self.alert_history.append(alert_record)

    def check_protection_conditions(self):
        """检查是否需要触发自保护策略"""
        metrics = self.current_metrics
        
        # 红线条件检查
        critical_conditions = []
        
        # 条件1: 成交延迟过高
        if metrics.fill_to_repost_latency_p99 > self.thresholds['fill_to_repost_latency_p99'].red_max:
            critical_conditions.append(f"成交响应延迟={metrics.fill_to_repost_latency_p99:.1f}ms超红线")
        
        # 条件2: 订单成功率过低
        if metrics.order_success_rate < self.thresholds['order_success_rate'].red_max:
            critical_conditions.append(f"订单成功率={metrics.order_success_rate:.1%}低于红线")
        
        # 条件3: 库存偏斜过大
        if metrics.inventory_skew_ratio > self.thresholds['inventory_skew_ratio'].red_max:
            critical_conditions.append(f"库存偏斜={metrics.inventory_skew_ratio:.1%}超红线")
        
        # 条件4: 风险敞口过大
        if metrics.risk_weighted_exposure > self.thresholds['risk_weighted_exposure'].red_max:
            critical_conditions.append(f"风险敞口=${metrics.risk_weighted_exposure:.0f}超红线")
        
        # 条件5: API权重接近限制
        if metrics.api_weight_utilization > self.thresholds['api_weight_utilization'].red_max:
            critical_conditions.append(f"API使用率={metrics.api_weight_utilization:.1%}超红线")
        
        # 条件6: 系统健康度过低
        if metrics.system_health_score < self.thresholds['system_health_score'].red_max:
            critical_conditions.append(f"系统健康度={metrics.system_health_score:.2f}低于红线")
        
        # 触发自保护策略
        if critical_conditions and not self.protection_active:
            reason = "; ".join(critical_conditions[:3])  # 最多显示3个原因
            self.trigger_protection_strategy(reason)

    def get_dashboard_summary(self) -> Dict:
        """获取仪表盘摘要信息"""
        metrics = self.current_metrics
        alert_level = self.evaluate_alert_level()
        
        summary = {
            'timestamp': datetime.fromtimestamp(metrics.timestamp).isoformat(),
            'alert_level': alert_level.value,
            'protection_active': self.protection_active,
            'metrics': {
                '成交响应延迟P99(ms)': round(metrics.fill_to_repost_latency_p99, 1),
                '订单成功率(%)': round(metrics.order_success_rate * 100, 1),
                '库存偏斜比率(%)': round(metrics.inventory_skew_ratio * 100, 1),
                '价差捕获效率(%)': round(metrics.spread_capture_efficiency * 100, 1),
                '流动性供给评分': round(metrics.liquidity_provision_score, 2),
                '风险加权敞口($)': round(metrics.risk_weighted_exposure, 0),
                'API权重使用率(%)': round(metrics.api_weight_utilization * 100, 1),
                '系统健康度评分': round(metrics.system_health_score, 2)
            },
            'thresholds_status': {}
        }
        
        # 添加阈值状态
        for metric_name in self.thresholds.keys():
            current_value = getattr(metrics, metric_name)
            threshold = self.thresholds[metric_name]
            
            if metric_name in ['order_success_rate', 'spread_capture_efficiency', 
                              'liquidity_provision_score', 'system_health_score']:
                # 正向指标状态
                if current_value >= threshold.green_max:
                    status = "GREEN"
                elif current_value >= threshold.yellow_max:
                    status = "YELLOW"
                elif current_value >= threshold.orange_max:
                    status = "ORANGE"
                else:
                    status = "RED"
            else:
                # 反向指标状态
                if current_value <= threshold.green_max:
                    status = "GREEN"
                elif current_value <= threshold.yellow_max:
                    status = "YELLOW"
                elif current_value <= threshold.orange_max:
                    status = "ORANGE"
                else:
                    status = "RED"
            
            summary['thresholds_status'][metric_name] = status
        
        return summary

    def update_metrics_snapshot(self):
        """更新指标快照到历史记录"""
        self.calculate_system_health()
        self.check_protection_conditions()
        
        # 保存历史记录
        snapshot = SystemMetrics(
            timestamp=time.time(),
            fill_to_repost_latency_p99=self.current_metrics.fill_to_repost_latency_p99,
            order_success_rate=self.current_metrics.order_success_rate,
            inventory_skew_ratio=self.current_metrics.inventory_skew_ratio,
            spread_capture_efficiency=self.current_metrics.spread_capture_efficiency,
            liquidity_provision_score=self.current_metrics.liquidity_provision_score,
            risk_weighted_exposure=self.current_metrics.risk_weighted_exposure,
            api_weight_utilization=self.current_metrics.api_weight_utilization,
            system_health_score=self.current_metrics.system_health_score
        )
        self.metrics_history.append(snapshot)

# 全局单例实例
_observability_dashboard = None

def get_observability_dashboard() -> ObservabilityDashboard:
    """获取可观测性仪表盘单例实例"""
    global _observability_dashboard
    if _observability_dashboard is None:
        _observability_dashboard = ObservabilityDashboard()
    return _observability_dashboard