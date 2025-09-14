#!/usr/bin/env python3
"""
Metrics System & Risk Circuit Breaker - 完整指标体系和风险熔断器
"""

import time
import math
import logging
import threading
from typing import Dict, List, Optional, Any, Callable
from collections import defaultdict, deque
from enum import Enum
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)


class MetricType(Enum):
    GAUGE = "gauge"       # 瞬时值
    COUNTER = "counter"   # 累计计数
    HISTOGRAM = "histogram"  # 分布统计


@dataclass
class MetricConfig:
    name: str
    metric_type: MetricType
    description: str
    unit: str = ""
    labels: List[str] = None
    
    def __post_init__(self):
        if self.labels is None:
            self.labels = []


class Metric:
    """指标基类"""
    
    def __init__(self, config: MetricConfig):
        self.config = config
        self.values = {}  # label_key -> value
        self.timestamps = {}  # label_key -> timestamp
        self.lock = threading.RLock()
        
    def _get_label_key(self, labels: Dict[str, str] = None) -> str:
        """生成标签键"""
        if not labels:
            return "default"
        return "|".join(f"{k}={v}" for k, v in sorted(labels.items()))
        
    def set(self, value: float, labels: Dict[str, str] = None):
        """设置值（适用于GAUGE）"""
        with self.lock:
            key = self._get_label_key(labels)
            self.values[key] = value
            self.timestamps[key] = time.time()
            
    def inc(self, delta: float = 1.0, labels: Dict[str, str] = None):
        """增加值（适用于COUNTER）"""
        with self.lock:
            key = self._get_label_key(labels)
            self.values[key] = self.values.get(key, 0) + delta
            self.timestamps[key] = time.time()
            
    def observe(self, value: float, labels: Dict[str, str] = None):
        """观测值（适用于HISTOGRAM）"""
        with self.lock:
            key = self._get_label_key(labels)
            if key not in self.values:
                self.values[key] = []
            self.values[key].append(value)
            self.timestamps[key] = time.time()
            
    def get(self, labels: Dict[str, str] = None) -> Any:
        """获取值"""
        with self.lock:
            key = self._get_label_key(labels)
            return self.values.get(key)
            
    def get_all(self) -> Dict[str, Any]:
        """获取所有值"""
        with self.lock:
            return self.values.copy()


class MetricsCollector:
    """指标收集器"""
    
    def __init__(self):
        self.metrics = {}  # name -> Metric
        self.lock = threading.RLock()
        
        # 核心指标定义
        self._init_core_metrics()
        
    def _init_core_metrics(self):
        """初始化核心指标"""
        core_metrics = [
            # 核心业务指标
            MetricConfig("util_onbook", MetricType.GAUGE, "在册利用率", "%"),
            MetricConfig("usdt_frac_eff", MetricType.GAUGE, "有效USDT比例", "%"),
            MetricConfig("equity_usd", MetricType.GAUGE, "总权益", "USD"),
            MetricConfig("n_buy", MetricType.GAUGE, "买侧锁定金额", "USD"),
            MetricConfig("n_sell", MetricType.GAUGE, "卖侧锁定金额", "USD"),
            
            # 执行质量指标
            MetricConfig("orders_placed", MetricType.COUNTER, "下单总数", "count"),
            MetricConfig("orders_filled", MetricType.COUNTER, "成交总数", "count"),
            MetricConfig("orders_rejected", MetricType.COUNTER, "拒单总数", "count", ["reason"]),
            MetricConfig("fills_per_minute", MetricType.GAUGE, "每分钟成交数", "fills/min"),
            
            # 系统健康指标
            MetricConfig("awg_usage_pct", MetricType.GAUGE, "AWG使用率", "%", ["window"]),
            MetricConfig("circuit_breaker_state", MetricType.GAUGE, "熔断器状态", "enum"),
            MetricConfig("ws_disconnects", MetricType.COUNTER, "WS断线次数", "count"),
            MetricConfig("api_errors", MetricType.COUNTER, "API错误次数", "count", ["error_code"]),
            
            # 延迟指标
            MetricConfig("order_latency", MetricType.HISTOGRAM, "下单延迟", "ms"),
            MetricConfig("cancel_latency", MetricType.HISTOGRAM, "撤单延迟", "ms"),
            MetricConfig("depth_latency", MetricType.HISTOGRAM, "深度获取延迟", "ms"),
            
            # 风险指标
            MetricConfig("drawdown_1h", MetricType.GAUGE, "1小时回撤", "%"),
            MetricConfig("position_imbalance", MetricType.GAUGE, "仓位失衡", "USD"),
            MetricConfig("price_impact", MetricType.HISTOGRAM, "价格冲击", "%"),
        ]
        
        for config in core_metrics:
            self.metrics[config.name] = Metric(config)
            
    def get_metric(self, name: str) -> Optional[Metric]:
        """获取指标"""
        return self.metrics.get(name)
        
    def register_metric(self, config: MetricConfig) -> Metric:
        """注册新指标"""
        with self.lock:
            metric = Metric(config)
            self.metrics[config.name] = metric
            return metric
            
    def set_gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        """设置GAUGE指标"""
        metric = self.get_metric(name)
        if metric:
            metric.set(value, labels)
            
    def inc_counter(self, name: str, delta: float = 1.0, labels: Dict[str, str] = None):
        """增加COUNTER指标"""
        metric = self.get_metric(name)
        if metric:
            metric.inc(delta, labels)
            
    def observe_histogram(self, name: str, value: float, labels: Dict[str, str] = None):
        """观测HISTOGRAM指标"""
        metric = self.get_metric(name)
        if metric:
            metric.observe(value, labels)
            
    def get_snapshot(self) -> Dict[str, Dict]:
        """获取所有指标快照"""
        snapshot = {}
        
        with self.lock:
            for name, metric in self.metrics.items():
                values = metric.get_all()
                
                # 处理不同类型的指标
                if metric.config.metric_type == MetricType.HISTOGRAM:
                    # 计算直方图统计
                    processed_values = {}
                    for key, hist_values in values.items():
                        if hist_values:
                            processed_values[key] = {
                                'count': len(hist_values),
                                'sum': sum(hist_values),
                                'min': min(hist_values),
                                'max': max(hist_values),
                                'avg': sum(hist_values) / len(hist_values),
                                'p50': self._percentile(hist_values, 0.5),
                                'p95': self._percentile(hist_values, 0.95),
                                'p99': self._percentile(hist_values, 0.99)
                            }
                    values = processed_values
                    
                snapshot[name] = {
                    'type': metric.config.metric_type.value,
                    'description': metric.config.description,
                    'unit': metric.config.unit,
                    'values': values
                }
                
        return snapshot
        
    def _percentile(self, values: List[float], p: float) -> float:
        """计算百分位数"""
        if not values:
            return 0
        sorted_values = sorted(values)
        k = (len(sorted_values) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_values[int(k)]
        return sorted_values[int(f)] * (c - k) + sorted_values[int(c)] * (k - f)


class RiskTrigger:
    """风险触发器"""
    
    def __init__(self, name: str, condition: Callable[[Dict], bool], 
                 action: Callable[[], None], description: str = ""):
        self.name = name
        self.condition = condition
        self.action = action
        self.description = description
        self.triggered = False
        self.last_trigger_time = 0
        self.trigger_count = 0
        
    def check(self, metrics: Dict) -> bool:
        """检查触发条件"""
        try:
            if self.condition(metrics):
                if not self.triggered:
                    self.triggered = True
                    self.last_trigger_time = time.time()
                    self.trigger_count += 1
                    
                    logger.error(f"[RiskBreaker] 触发: {self.name} - {self.description}")
                    
                    # 执行动作
                    try:
                        self.action()
                    except Exception as e:
                        logger.error(f"[RiskBreaker] 动作执行失败 {self.name}: {e}")
                        
                return True
                
        except Exception as e:
            logger.error(f"[RiskBreaker] 条件检查异常 {self.name}: {e}")
            
        return False
        
    def reset(self):
        """重置触发状态"""
        self.triggered = False


class RiskCircuitBreaker:
    """风险熔断器"""
    
    def __init__(self, metrics_collector: MetricsCollector):
        self.metrics = metrics_collector
        self.triggers = {}  # name -> RiskTrigger
        self.circuit_open = False
        self.circuit_open_time = 0
        self.emergency_shutdown = False
        
        # 统计
        self.stats = {
            'checks_performed': 0,
            'triggers_fired': 0,
            'circuit_trips': 0,
            'emergency_shutdowns': 0
        }
        
        # 初始化标准触发器
        self._init_standard_triggers()
        
    def _init_standard_triggers(self):
        """初始化标准触发器"""
        
        # 1. 最大回撤触发器
        def check_max_drawdown(m):
            drawdown = self._get_metric_value(m, 'drawdown_1h')
            return drawdown and drawdown > 0.02  # 2%
            
        def action_max_drawdown():
            self.trip_circuit("最大回撤超限")
            
        self.add_trigger(
            "max_drawdown_1h",
            check_max_drawdown,
            action_max_drawdown,
            "1小时回撤超过2%"
        )
        
        # 2. API错误频率触发器
        def check_api_errors(m):
            error_count = self._get_metric_value(m, 'api_errors', labels={'error_code': '-1003'})
            return error_count and error_count > 10  # 10次IP封禁
            
        def action_api_errors():
            self.trip_circuit("API错误频率过高")
            
        self.add_trigger(
            "api_errors_high",
            check_api_errors,
            action_api_errors,
            "API错误次数过多"
        )
        
        # 3. 拒单率触发器
        def check_reject_rate(m):
            placed = self._get_metric_value(m, 'orders_placed') or 1
            rejected = self._get_metric_value(m, 'orders_rejected') or 0
            reject_rate = rejected / placed
            return reject_rate > 0.5  # 50%拒单率
            
        def action_reject_rate():
            self.trip_circuit("拒单率过高")
            
        self.add_trigger(
            "high_reject_rate",
            check_reject_rate,
            action_reject_rate,
            "拒单率超过50%"
        )
        
        # 4. 利用率异常触发器
        def check_util_anomaly(m):
            util = self._get_metric_value(m, 'util_onbook')
            return util is not None and util < 0.1  # 利用率过低
            
        def action_util_anomaly():
            logger.warning("[RiskBreaker] 利用率异常，进入观察模式")
            
        self.add_trigger(
            "util_anomaly",
            check_util_anomaly,
            action_util_anomaly,
            "利用率异常偏低"
        )
        
    def _get_metric_value(self, metrics_snapshot: Dict, metric_name: str, 
                         labels: Dict[str, str] = None) -> Optional[float]:
        """从指标快照中获取值"""
        try:
            metric_data = metrics_snapshot.get(metric_name, {})
            values = metric_data.get('values', {})
            
            if labels:
                label_key = "|".join(f"{k}={v}" for k, v in sorted(labels.items()))
                return values.get(label_key)
            else:
                # 获取默认值或第一个值
                return values.get('default') or next(iter(values.values()), None)
                
        except Exception:
            return None
            
    def add_trigger(self, name: str, condition: Callable, action: Callable, 
                   description: str = ""):
        """添加触发器"""
        self.triggers[name] = RiskTrigger(name, condition, action, description)
        
    def trip_circuit(self, reason: str):
        """触发熔断"""
        if not self.circuit_open:
            self.circuit_open = True
            self.circuit_open_time = time.time()
            self.stats['circuit_trips'] += 1
            
            # 更新指标
            self.metrics.set_gauge('circuit_breaker_state', 1)  # 1 = OPEN
            
            logger.critical(f"[RiskBreaker] 🚨 熔断器打开: {reason}")
            
    def emergency_stop(self, reason: str):
        """紧急停机"""
        self.emergency_shutdown = True
        self.stats['emergency_shutdowns'] += 1
        
        logger.critical(f"[RiskBreaker] 🚨🚨🚨 紧急停机: {reason}")
        
    def check_all_triggers(self) -> Dict[str, bool]:
        """检查所有触发器"""
        self.stats['checks_performed'] += 1
        
        # 获取指标快照
        snapshot = self.metrics.get_snapshot()
        
        # 检查每个触发器
        trigger_results = {}
        for name, trigger in self.triggers.items():
            triggered = trigger.check(snapshot)
            trigger_results[name] = triggered
            
            if triggered:
                self.stats['triggers_fired'] += 1
                
        return trigger_results
        
    def get_status(self) -> Dict:
        """获取状态"""
        return {
            'circuit_open': self.circuit_open,
            'emergency_shutdown': self.emergency_shutdown,
            'circuit_open_time': self.circuit_open_time,
            'time_since_trip': time.time() - self.circuit_open_time if self.circuit_open else 0,
            'triggers': {
                name: {
                    'triggered': t.triggered,
                    'trigger_count': t.trigger_count,
                    'last_trigger_time': t.last_trigger_time,
                    'description': t.description
                }
                for name, t in self.triggers.items()
            },
            'stats': self.stats.copy()
        }
        
    def reset_circuit(self):
        """重置熔断器"""
        self.circuit_open = False
        self.circuit_open_time = 0
        
        # 重置所有触发器
        for trigger in self.triggers.values():
            trigger.reset()
            
        # 更新指标
        self.metrics.set_gauge('circuit_breaker_state', 0)  # 0 = CLOSED
        
        logger.info("[RiskBreaker] 熔断器重置")


# 全局实例
_metrics_collector = None
_risk_breaker = None


def get_metrics_collector() -> MetricsCollector:
    """获取全局指标收集器"""
    global _metrics_collector
    
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
        
    return _metrics_collector


def get_risk_circuit_breaker() -> RiskCircuitBreaker:
    """获取全局风险熔断器"""
    global _risk_breaker, _metrics_collector
    
    if _risk_breaker is None:
        if _metrics_collector is None:
            _metrics_collector = MetricsCollector()
        _risk_breaker = RiskCircuitBreaker(_metrics_collector)
        
    return _risk_breaker


if __name__ == "__main__":
    # 测试
    import random
    
    # 创建指标系统
    metrics = get_metrics_collector()
    breaker = get_risk_circuit_breaker()
    
    # 模拟指标数据
    for i in range(100):
        metrics.set_gauge('util_onbook', 0.85 + random.uniform(-0.1, 0.1))
        metrics.set_gauge('usdt_frac_eff', 0.52 + random.uniform(-0.05, 0.05))
        metrics.inc_counter('orders_placed')
        
        if random.random() < 0.1:  # 10%概率拒单
            metrics.inc_counter('orders_rejected', labels={'reason': 'maker'})
            
        # 观测延迟
        metrics.observe_histogram('order_latency', random.uniform(10, 100))
        
    # 检查触发器
    results = breaker.check_all_triggers()
    print(f"触发器检查结果: {results}")
    
    # 获取快照
    snapshot = metrics.get_snapshot()
    print(f"指标快照: {json.dumps(snapshot, indent=2)}")
    
    # 获取风险状态
    risk_status = breaker.get_status()
    print(f"风险状态: {json.dumps(risk_status, indent=2)}")

# 全局实例
_global_metrics_system = None

def get_metrics_system(**kwargs):
    """获取Metrics System实例（单例）"""
    global _global_metrics_system
    
    if _global_metrics_system is None:
        _global_metrics_system = get_metrics_collector()  # 使用已有的MetricsCollector
        logger.info("[MetricsSystem] 创建新实例")
    
    return _global_metrics_system

def reset_metrics_system():
    """重置Metrics System实例（测试用）"""
    global _global_metrics_system
    _global_metrics_system = None

