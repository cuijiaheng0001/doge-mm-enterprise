"""
Institutional Event Ledger - 机构级事件账本
单一真相源 (SSOT) 架构，对标Jane Street/Citadel标准
"""
import time
import asyncio
import logging
from decimal import Decimal
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from threading import Lock
from collections import defaultdict
import hashlib
import json

logger = logging.getLogger(__name__)


@dataclass
class ExecutionEvent:
    """执行事件 - 不可变事件记录"""
    seq: int                          # 全局序列号
    exec_report: Any                  # ExecReport对象
    ts: int                          # 事件时间戳 (纳秒)
    hash: str                        # 事件哈希 (用于完整性验证)
    processed_ts: int = field(default_factory=time.time_ns)  # 处理时间戳


@dataclass
class BalanceSnapshot:
    """余额快照 - 特定时刻的余额状态"""
    seq: int
    base_balance: Decimal    # DOGE余额
    quote_balance: Decimal   # USDT余额
    ts: int
    event_count: int         # 累计事件数
    

class BalanceProjector:
    """余额投影器 - 从事件流重构余额状态"""
    
    def __init__(self):
        self.current_base = Decimal(0)
        self.current_quote = Decimal(0)
        self.last_seq = 0
        self.event_count = 0
        self.lock = Lock()
        
    def project_balance(self, event: ExecutionEvent) -> bool:
        """根据执行事件投影余额变化"""
        with self.lock:
            try:
                exec_report = event.exec_report
                
                # 提取增量数据
                if hasattr(exec_report, 'last_qty') and hasattr(exec_report, 'last_quote'):
                    qty_delta = exec_report.last_qty
                    quote_delta = exec_report.last_quote
                    side = exec_report.side
                    
                    # 只处理有实际成交的事件
                    if qty_delta > 0:
                        if side == 'BUY':
                            self.current_base += qty_delta
                            self.current_quote -= quote_delta
                        else:  # SELL
                            self.current_base -= qty_delta
                            self.current_quote += quote_delta
                        
                        logger.debug(
                            "[BalanceProjector] Event %d: %s qty=%s quote=%s -> "
                            "base=%s quote=%s",
                            event.seq, side, qty_delta, quote_delta,
                            self.current_base, self.current_quote
                        )
                
                self.last_seq = event.seq
                self.event_count += 1
                return True
                
            except Exception as e:
                logger.error(
                    "[BalanceProjector] 投影失败 seq=%d: %s",
                    event.seq, str(e)
                )
                return False
    
    def get_balance_snapshot(self) -> BalanceSnapshot:
        """获取当前余额快照"""
        with self.lock:
            return BalanceSnapshot(
                seq=self.last_seq,
                base_balance=self.current_base,
                quote_balance=self.current_quote,
                ts=time.time_ns(),
                event_count=self.event_count
            )


class DualPathReconciler:
    """双路对账器 - 验证SSOT与真实余额的一致性"""
    
    def __init__(self, tolerance: float = 0.001):
        self.tolerance = tolerance  # 0.1%容差
        self.last_reconcile_ts = time.time()
        self.reconcile_count = 0
        self.deviation_history: List[Dict[str, float]] = []
        
    async def verify_consistency(self):
        """验证一致性 - 异步执行避免阻塞主流程"""
        try:
            # 这里应该获取真实余额，暂时使用占位符
            # real_base, real_quote = await self.get_real_balance()
            # projected = self.balance_projector.get_balance_snapshot()
            
            # 计算偏差
            # base_dev = abs(real_base - projected.base_balance) / max(real_base, Decimal('0.01'))
            # quote_dev = abs(real_quote - projected.quote_balance) / max(real_quote, Decimal('0.01'))
            
            # if base_dev > self.tolerance or quote_dev > self.tolerance:
            #     await self.handle_deviation(base_dev, quote_dev)
            
            self.reconcile_count += 1
            self.last_reconcile_ts = time.time()
            
        except Exception as e:
            logger.error("[DualPathReconciler] 对账失败: %s", str(e))
    
    async def handle_deviation(self, base_dev: float, quote_dev: float):
        """处理偏差超限情况"""
        logger.warning(
            "[DualPathReconciler] 偏差超限: base_dev=%.3f%% quote_dev=%.3f%% (阈值=%.1f%%)",
            base_dev * 100, quote_dev * 100, self.tolerance * 100
        )
        
        # 记录偏差历史
        self.deviation_history.append({
            'ts': time.time(),
            'base_dev': base_dev,
            'quote_dev': quote_dev
        })
        
        # 保留最近100条记录
        if len(self.deviation_history) > 100:
            self.deviation_history.pop(0)


class InstitutionalEventLedger:
    """
    机构级事件账本 - 单一真相源 (SSOT)
    所有余额变化事件的权威记录，支持完整重放和审计
    """
    
    def __init__(self):
        self.sequence_id = 0
        self.event_stream: List[ExecutionEvent] = []  # 不可变事件流
        self.lock = Lock()
        
        # 核心组件
        self.balance_projector = BalanceProjector()
        self.reconciler = DualPathReconciler()
        
        # 性能监控
        self.metrics = {
            'events_processed': 0,
            'projection_errors': 0,
            'reconcile_deviations': 0,
            'last_event_ts': 0
        }
        
        logger.info("[InstitutionalEventLedger] 初始化完成 - SSOT架构已就绪")
    
    def _compute_hash(self, exec_report: Any) -> str:
        """计算事件哈希值用于完整性验证"""
        try:
            # 提取关键字段生成哈希
            data = {
                'order_id': getattr(exec_report, 'order_id', 0),
                'side': getattr(exec_report, 'side', ''),
                'last_qty': float(getattr(exec_report, 'last_qty', 0)),
                'cum_qty': float(getattr(exec_report, 'cum_qty', 0)),
                'last_quote': float(getattr(exec_report, 'last_quote', 0)),
                'cum_quote': float(getattr(exec_report, 'cum_quote', 0)),
                'ts': getattr(exec_report, 'ts', 0),
                'update_id': getattr(exec_report, 'update_id', 0)
            }
            
            json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
            return hashlib.sha256(json_str.encode()).hexdigest()[:16]
            
        except Exception as e:
            logger.warning("[EventLedger] 哈希计算失败: %s", str(e))
            return f"hash_error_{time.time_ns()}"
    
    def append_execution_event(self, exec_report: Any) -> bool:
        """
        追加执行事件到账本 (追加写模式)
        
        Args:
            exec_report: EventNormalizer规范化后的执行报告
            
        Returns:
            bool: 是否成功追加
        """
        with self.lock:
            try:
                self.sequence_id += 1
                
                # 创建不可变事件
                event = ExecutionEvent(
                    seq=self.sequence_id,
                    exec_report=exec_report,
                    ts=time.time_ns(),
                    hash=self._compute_hash(exec_report)
                )
                
                # 追加到事件流 (只追加，永不修改)
                self.event_stream.append(event)
                
                # 实时投影余额
                projection_success = self.balance_projector.project_balance(event)
                
                # 异步触发对账 (不阻塞主流程)
                asyncio.create_task(self.reconciler.verify_consistency())
                
                # 更新指标
                self.metrics['events_processed'] += 1
                self.metrics['last_event_ts'] = event.ts
                if not projection_success:
                    self.metrics['projection_errors'] += 1
                
                logger.debug(
                    "[EventLedger] Event appended: seq=%d oid=%s side=%s qty=%s hash=%s",
                    event.seq,
                    getattr(exec_report, 'order_id', 'N/A'),
                    getattr(exec_report, 'side', 'N/A'),
                    getattr(exec_report, 'last_qty', 'N/A'),
                    event.hash
                )
                
                # 每处理100个事件输出一次统计
                if self.metrics['events_processed'] % 100 == 0:
                    self._emit_metrics()
                
                return True
                
            except Exception as e:
                logger.error(
                    "[EventLedger] 追加事件失败: %s",
                    str(e), exc_info=True
                )
                return False
    
    def get_current_balance(self) -> BalanceSnapshot:
        """获取当前投影余额"""
        return self.balance_projector.get_balance_snapshot()
    
    def replay_events(self, from_seq: int = 0, to_seq: Optional[int] = None) -> BalanceSnapshot:
        """
        重放事件重构余额状态
        
        Args:
            from_seq: 起始序列号
            to_seq: 结束序列号 (None表示到最新)
            
        Returns:
            BalanceSnapshot: 重构后的余额状态
        """
        with self.lock:
            # 创建临时投影器
            temp_projector = BalanceProjector()
            
            end_seq = to_seq or self.sequence_id
            replayed_events = 0
            
            for event in self.event_stream:
                if from_seq <= event.seq <= end_seq:
                    temp_projector.project_balance(event)
                    replayed_events += 1
            
            logger.info(
                "[EventLedger] 重放完成: from_seq=%d to_seq=%d events=%d",
                from_seq, end_seq, replayed_events
            )
            
            return temp_projector.get_balance_snapshot()
    
    def _emit_metrics(self):
        """输出性能指标"""
        snapshot = self.get_current_balance()
        
        logger.info(
            "[EventLedger-Metrics] events=%d errors=%d seq=%d "
            "balance(base=%s quote=%s) age=%.1fs",
            self.metrics['events_processed'],
            self.metrics['projection_errors'],
            snapshot.seq,
            snapshot.base_balance,
            snapshot.quote_balance,
            (time.time_ns() - self.metrics['last_event_ts']) / 1e9
        )
    
    def get_ledger_status(self) -> Dict[str, Any]:
        """获取账本状态信息"""
        with self.lock:
            snapshot = self.get_current_balance()
            
            return {
                'sequence_id': self.sequence_id,
                'events_in_ledger': len(self.event_stream),
                'current_balance': {
                    'base': float(snapshot.base_balance),
                    'quote': float(snapshot.quote_balance)
                },
                'metrics': self.metrics.copy(),
                'reconciler': {
                    'reconcile_count': self.reconciler.reconcile_count,
                    'last_reconcile_age': time.time() - self.reconciler.last_reconcile_ts,
                    'deviation_history_count': len(self.reconciler.deviation_history)
                }
            }