"""
Shadow Balance - 机构级Delta驱动补丁
Phase 5 Fix: 纯数值增量，零状态字符串依赖
"""
from decimal import Decimal
from typing import Dict, Any
import logging
import time

logger = logging.getLogger(__name__)


def on_execution_report_institutional(self, exec_report: Any) -> bool:
    """
    Phase 5 机构级实现：纯Delta驱动，不依赖状态字符串
    对标Jane Street/Citadel标准
    
    Args:
        exec_report: EventNormalizer规范化后的执行报告
        
    Returns:
        bool: 处理是否成功
    """
    with self.lock:
        try:
            # === 确保exec_records字典存在 ===
            if not hasattr(self, 'exec_records'):
                self.exec_records = {}
            if not hasattr(self, 'phase5_metrics'):
                self.phase5_metrics = {
                    'delta_success': 0,
                    'delta_negative': 0,
                    'delta_zero': 0,
                    'duplicate_events': 0
                }
            
            # === 提取关键字段（从ExecReport对象） ===
            order_id = str(exec_report.order_id)
            cum_qty = Decimal(str(exec_report.cum_qty))
            cum_quote = Decimal(str(exec_report.cum_quote))
            side = exec_report.side
            update_id = exec_report.update_id
            
            # === 防重机制（基于update_id） ===
            last_record = self.exec_records.get(order_id, {})
            if last_record.get('update_id', 0) >= update_id:
                self.phase5_metrics['duplicate_events'] += 1
                logger.debug(
                    "[Shadow-Institutional] 重复事件跳过: order=%s update_id=%d",
                    order_id, update_id
                )
                return True
            
            # === 获取上次记录的累计值 ===
            last_cum_qty = Decimal(str(last_record.get('cum_qty', 0)))
            last_cum_quote = Decimal(str(last_record.get('cum_quote', 0)))
            
            # === 计算Delta（核心：只依赖数值增量） ===
            qty_delta = cum_qty - last_cum_qty
            quote_delta = cum_quote - last_cum_quote
            
            # === 安全性检查：负Delta异常 ===
            if qty_delta < 0 or quote_delta < 0:
                self.phase5_metrics['delta_negative'] += 1
                logger.error(
                    "[Shadow-Institutional] ❌ 负Delta异常: order=%s "
                    "qty_delta=%s quote_delta=%s (cum_qty=%s last=%s)",
                    order_id, qty_delta, quote_delta, cum_qty, last_cum_qty
                )
                return False
            
            # === 零Delta跳过（无实际成交） ===
            if qty_delta == 0:
                self.phase5_metrics['delta_zero'] += 1
                logger.debug(
                    "[Shadow-Institutional] 零Delta跳过: order=%s",
                    order_id
                )
                # 仍然更新update_id防止后续误判
                self.exec_records[order_id] = {
                    'cum_qty': cum_qty,
                    'cum_quote': cum_quote,
                    'update_id': update_id,
                    'ts': time.time_ns()
                }
                return True
            
            # === 核心：基于Delta更新余额 ===
            # 获取资产键（兼容性）
            if symbol := exec_report.symbol:
                if 'DOGE' in symbol.upper():
                    base_asset = 'DOGE'
                    quote_asset = 'USDT'
                else:
                    # 其他交易对处理
                    base_asset = symbol[:4]
                    quote_asset = symbol[4:]
            else:
                base_asset = 'DOGE'
                quote_asset = 'USDT'
            
            # 初始化余额字典（如果不存在）
            if not hasattr(self, 'shadow_balance'):
                self.shadow_balance = {}
            
            # 更新余额
            if side == 'BUY':
                # 买入：增加base，减少quote
                self.shadow_balance[base_asset] = self.shadow_balance.get(base_asset, 0) + float(qty_delta)
                self.shadow_balance[quote_asset] = self.shadow_balance.get(quote_asset, 0) - float(quote_delta)
            else:  # SELL
                # 卖出：减少base，增加quote
                self.shadow_balance[base_asset] = self.shadow_balance.get(base_asset, 0) - float(qty_delta)
                self.shadow_balance[quote_asset] = self.shadow_balance.get(quote_asset, 0) + float(quote_delta)
            
            # === 更新执行记录 ===
            self.exec_records[order_id] = {
                'cum_qty': cum_qty,
                'cum_quote': cum_quote,
                'update_id': update_id,
                'ts': time.time_ns(),
                'side': side
            }
            
            # === 更新统计 ===
            self.phase5_metrics['delta_success'] += 1
            self.stats['shadow_updates'] = self.stats.get('shadow_updates', 0) + 1
            
            logger.info(
                "[Shadow-Institutional-Delta] ✅ 更新成功: order=%s side=%s "
                "delta_qty=%s delta_quote=%s | 新余额: %s=%s %s=%s",
                order_id, side, qty_delta, quote_delta,
                base_asset, self.shadow_balance.get(base_asset, 0),
                quote_asset, self.shadow_balance.get(quote_asset, 0)
            )
            
            # 定期输出指标
            if self.phase5_metrics['delta_success'] % 10 == 0:
                total_events = (
                    self.phase5_metrics['delta_success'] +
                    self.phase5_metrics['delta_negative'] +
                    self.phase5_metrics['delta_zero']
                )
                logger.info(
                    "[Shadow-Institutional-Metrics] Delta成功=%d 负Delta=%d "
                    "零Delta=%d 重复=%d 成功率=%.1f%%",
                    self.phase5_metrics['delta_success'],
                    self.phase5_metrics['delta_negative'],
                    self.phase5_metrics['delta_zero'],
                    self.phase5_metrics['duplicate_events'],
                    (self.phase5_metrics['delta_success'] / max(total_events, 1)) * 100
                )
            
            return True
            
        except Exception as e:
            logger.error(
                "[Shadow-Institutional] 处理异常: %s",
                str(e), exc_info=True
            )
            return False