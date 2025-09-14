"""
Shadow Balance V2 - 机构级Delta驱动实现
纯数值增量驱动，不依赖状态字符串
对标Jane Street/Citadel标准
"""
import logging
import time
from decimal import Decimal
from typing import Dict, Any, Optional
from threading import Lock
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRecord:
    """执行记录 - 存储每个订单的累计成交"""
    order_id: str
    cum_qty: Decimal = Decimal(0)
    cum_quote: Decimal = Decimal(0)
    update_id: int = 0
    last_update_ts: int = 0
    side: str = ""
    symbol: str = ""


@dataclass
class Phase5Metrics:
    """Phase 5关键指标监控"""
    shadow_updates: int = 0
    delta_success: int = 0
    delta_negative: int = 0
    delta_zero: int = 0
    duplicate_events: int = 0
    reconcile_count: int = 0
    error_2010: int = 0
    total_fills: int = 0
    
    def get_error_rate(self) -> float:
        """计算-2010错误率"""
        if self.total_fills == 0:
            return 0
        return self.error_2010 / self.total_fills
    
    def log_summary(self):
        """输出监控摘要"""
        error_rate = self.get_error_rate()
        logger.info(
            "[Phase5-Metrics] Shadow更新=%d Delta成功=%d 负Delta=%d "
            "重复事件=%d -2010错误率=%.2f%% (目标<5%%)",
            self.shadow_updates,
            self.delta_success,
            self.delta_negative,
            self.duplicate_events,
            error_rate * 100
        )


class InstitutionalShadowBalance:
    """
    机构级Shadow Balance - 纯Delta驱动
    不依赖任何状态字符串，只依赖数值增量
    """
    
    def __init__(self, reserve_ratio: float = 0.02):
        """
        初始化
        
        Args:
            reserve_ratio: 预留比例（默认2%）
        """
        self.lock = Lock()
        self.reserve_ratio = reserve_ratio
        
        # 余额状态
        self.base_balance = Decimal(0)   # DOGE余额
        self.quote_balance = Decimal(0)  # USDT余额
        
        # 执行记录（按订单ID存储累计值）
        self.exec_records: Dict[str, ExecutionRecord] = {}
        
        # 监控指标
        self.metrics = Phase5Metrics()
        
        # 对账时间戳
        self.last_reconcile_ts = time.time()
        self.reconcile_interval = 30  # 30秒对账一次
        
        logger.info(
            "[InstitutionalShadow] 初始化完成 reserve_ratio=%.2f%%",
            reserve_ratio * 100
        )
    
    def on_execution_report(self, exec_report: Any) -> bool:
        """
        纯数值Delta驱动，不依赖status字段
        
        Args:
            exec_report: EventNormalizer规范化后的执行报告
            
        Returns:
            bool: 处理是否成功
        """
        with self.lock:
            try:
                # 提取关键字段（exec_report应该是ExecReport对象）
                order_id = str(exec_report.order_id)
                cum_qty = exec_report.cum_qty
                cum_quote = exec_report.cum_quote
                side = exec_report.side
                update_id = exec_report.update_id
                
                # 防重机制
                if self._is_duplicate(order_id, update_id):
                    self.metrics.duplicate_events += 1
                    logger.debug(
                        "[InstitutionalShadow] 重复事件跳过: order=%s update_id=%d",
                        order_id, update_id
                    )
                    return True
                
                # 获取上次记录的累计值
                last_record = self.exec_records.get(order_id)
                if last_record is None:
                    # 首次见到此订单
                    last_record = ExecutionRecord(
                        order_id=order_id,
                        side=side,
                        symbol=exec_report.symbol
                    )
                    self.exec_records[order_id] = last_record
                
                # 计算Delta（关键：只依赖数值增量）
                qty_delta = cum_qty - last_record.cum_qty
                quote_delta = cum_quote - last_record.cum_quote
                
                # 安全性检查：负Delta异常
                if qty_delta < 0 or quote_delta < 0:
                    self.metrics.delta_negative += 1
                    logger.error(
                        "[InstitutionalShadow] ❌ 负Delta异常: order=%s "
                        "qty_delta=%s quote_delta=%s",
                        order_id, qty_delta, quote_delta
                    )
                    return False
                
                # 零Delta跳过（无实际成交）
                if qty_delta == 0:
                    self.metrics.delta_zero += 1
                    logger.debug(
                        "[InstitutionalShadow] 零Delta跳过: order=%s",
                        order_id
                    )
                    # 仍然更新update_id防止后续误判
                    last_record.update_id = update_id
                    last_record.last_update_ts = time.time_ns()
                    return True
                
                # === 核心：基于Delta更新余额 ===
                if side == 'BUY':
                    # 买入：增加base，减少quote
                    self.base_balance += qty_delta
                    self.quote_balance -= quote_delta
                else:  # SELL
                    # 卖出：减少base，增加quote
                    self.base_balance -= qty_delta
                    self.quote_balance += quote_delta
                
                # 更新记录
                last_record.cum_qty = cum_qty
                last_record.cum_quote = cum_quote
                last_record.update_id = update_id
                last_record.last_update_ts = time.time_ns()
                
                # 更新指标
                self.metrics.shadow_updates += 1
                self.metrics.delta_success += 1
                self.metrics.total_fills += 1
                
                logger.info(
                    "[InstitutionalShadow-Delta] ✅ 更新成功: order=%s side=%s "
                    "delta_qty=%s delta_quote=%s 新余额: base=%s quote=%s",
                    order_id, side, qty_delta, quote_delta,
                    self.base_balance, self.quote_balance
                )
                
                # 定期输出指标
                if self.metrics.shadow_updates % 10 == 0:
                    self.metrics.log_summary()
                
                return True
                
            except Exception as e:
                logger.error(
                    "[InstitutionalShadow] Delta处理异常: %s",
                    str(e), exc_info=True
                )
                return False
    
    def _is_duplicate(self, order_id: str, update_id: int) -> bool:
        """
        基于update_id去重
        
        Args:
            order_id: 订单ID
            update_id: 更新序号
            
        Returns:
            bool: 是否为重复事件
        """
        last_record = self.exec_records.get(order_id)
        if last_record and last_record.update_id >= update_id:
            return True
        return False
    
    def get_available_balance(self, asset: str) -> Decimal:
        """
        获取可用余额（扣除预留）
        
        Args:
            asset: 资产类型 ('DOGE' or 'USDT')
            
        Returns:
            Decimal: 可用余额
        """
        with self.lock:
            if asset.upper() == 'DOGE':
                real_balance = self.base_balance
            elif asset.upper() == 'USDT':
                real_balance = self.quote_balance
            else:
                return Decimal(0)
            
            # 应用预留比例
            available = real_balance * (1 - Decimal(str(self.reserve_ratio)))
            
            # 确保非负
            return max(Decimal(0), available)
    
    def check_order_feasible(self, side: str, qty: Decimal, price: Decimal) -> bool:
        """
        检查订单可行性（预留模型）
        
        Args:
            side: 买卖方向
            qty: 数量
            price: 价格
            
        Returns:
            bool: 是否可行
        """
        with self.lock:
            if side == 'BUY':
                # 买入需要USDT
                required = qty * price
                available = self.get_available_balance('USDT')
            else:  # SELL
                # 卖出需要DOGE
                required = qty
                available = self.get_available_balance('DOGE')
            
            # 额外安全边际
            feasible = required <= available * Decimal('0.98')
            
            if not feasible:
                logger.warning(
                    "[InstitutionalShadow] 订单不可行: side=%s required=%s available=%s",
                    side, required, available
                )
                self.metrics.error_2010 += 1
            
            return feasible
    
    async def reconcile_with_real_balance(self, real_base: Decimal, real_quote: Decimal):
        """
        对账机制 - 与真实余额同步
        
        Args:
            real_base: 真实DOGE余额
            real_quote: 真实USDT余额
        """
        with self.lock:
            # 计算偏差
            base_deviation = abs(real_base - self.base_balance) / max(real_base, Decimal('0.01'))
            quote_deviation = abs(real_quote - self.quote_balance) / max(real_quote, Decimal('0.01'))
            
            # 0.1%阈值
            threshold = Decimal('0.001')
            
            if base_deviation > threshold or quote_deviation > threshold:
                logger.warning(
                    "[InstitutionalShadow-Reconcile] 偏差超限: "
                    "base_real=%s shadow=%s dev=%.2f%% | "
                    "quote_real=%s shadow=%s dev=%.2f%%",
                    real_base, self.base_balance, base_deviation * 100,
                    real_quote, self.quote_balance, quote_deviation * 100
                )
                
                # 强制同步
                self.base_balance = real_base
                self.quote_balance = real_quote
                self.metrics.reconcile_count += 1
                
                logger.info(
                    "[InstitutionalShadow-Reconcile] 强制同步完成: base=%s quote=%s",
                    self.base_balance, self.quote_balance
                )
            else:
                logger.debug(
                    "[InstitutionalShadow-Reconcile] 偏差正常: base_dev=%.3f%% quote_dev=%.3f%%",
                    base_deviation * 100, quote_deviation * 100
                )
            
            self.last_reconcile_ts = time.time()
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取状态信息
        
        Returns:
            Dict: 状态信息
        """
        with self.lock:
            return {
                'base_balance': float(self.base_balance),
                'quote_balance': float(self.quote_balance),
                'reserve_ratio': self.reserve_ratio,
                'exec_records_count': len(self.exec_records),
                'metrics': {
                    'shadow_updates': self.metrics.shadow_updates,
                    'delta_success': self.metrics.delta_success,
                    'delta_negative': self.metrics.delta_negative,
                    'duplicate_events': self.metrics.duplicate_events,
                    'error_2010_rate': f"{self.metrics.get_error_rate() * 100:.2f}%"
                },
                'last_reconcile_age': time.time() - self.last_reconcile_ts
            }