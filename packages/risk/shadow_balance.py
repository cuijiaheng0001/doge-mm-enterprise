#!/usr/bin/env python3
"""
Shadow Balance - 影子余额预分配机制
防止多个订单同时争抢同一份资金导致的余额冲突
"""

import time
import threading
import logging
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
from decimal import Decimal
import json
import os

from .event_ledger import EventLedger, get_event_ledger, OrderEvent, EventType

logger = logging.getLogger(__name__)


class Reservation:
    """资金预留记录"""
    
    def __init__(self, order_id: str, asset: str, amount: float, 
                 created_at: float = None, ttl: float = 300):
        self.order_id = order_id
        self.asset = asset
        self.amount = amount
        self.created_at = created_at or time.time()
        self.ttl = ttl  # 5分钟TTL
        
    @property
    def is_expired(self) -> bool:
        """是否过期"""
        return time.time() > self.created_at + self.ttl
        
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'asset': self.asset,
            'amount': self.amount,
            'created_at': self.created_at,
            'ttl': self.ttl
        }
        
    @classmethod
    def from_dict(cls, data: Dict) -> 'Reservation':
        """从字典创建"""
        return cls(
            data['order_id'],
            data['asset'], 
            data['amount'],
            data['created_at'],
            data['ttl']
        )


class ShadowBalance:
    """影子余额管理器 - 预分配机制防止余额冲突 (SSOT集成版)"""
    
    def __init__(self, sync_interval: float = 30, reserve_factor: float = 1.1,
                 persist_path: str = "/tmp/shadow_balance.json", 
                 use_event_ledger: bool = True):
        """
        初始化Shadow Balance
        
        Args:
            sync_interval: 与真实余额同步间隔（秒）
            reserve_factor: 预留因子，实际预留 = 需求 * factor
            persist_path: 持久化文件路径
            use_event_ledger: 是否使用事件账本作为SSOT
        """
        self.sync_interval = sync_interval
        self.reserve_factor = reserve_factor
        self.persist_path = persist_path
        self.use_event_ledger = use_event_ledger
        self.lock = threading.RLock()
        
        # 集成事件账本
        self.event_ledger = get_event_ledger() if use_event_ledger else None
        
        # 余额状态
        self.actual_balance = {}  # 实际余额（从API获取）
        self.shadow_balance = {}  # 影子余额（减去预留后）
        self.last_sync = 0
        
        # 预留记录
        self.reservations = {}  # order_id -> Reservation
        self.asset_reservations = defaultdict(list)  # asset -> [Reservations]
        
        # 统计
        self.stats = {
            'total_reserves': 0,
            'successful_reserves': 0,
            'failed_reserves': 0,
            'expired_reserves': 0,
            'released_reserves': 0,
            'sync_count': 0,
            'ledger_syncs': 0,
            'ledger_divergences': 0,
            'ssot_mode': use_event_ledger
        }
        
        # 尝试从持久化恢复
        self._load_from_disk()
        
    def _load_from_disk(self):
        """从磁盘恢复状态"""
        try:
            if os.path.exists(self.persist_path):
                with open(self.persist_path, 'r') as f:
                    data = json.load(f)
                    
                # 恢复预留记录
                for res_data in data.get('reservations', []):
                    res = Reservation.from_dict(res_data)
                    if not res.is_expired:
                        self.reservations[res.order_id] = res
                        self.asset_reservations[res.asset].append(res)
                        
                # 恢复余额（但需要重新同步）
                self.actual_balance = data.get('actual_balance', {})
                self.shadow_balance = data.get('shadow_balance', {})
                
                logger.info(f"[Shadow] 从磁盘恢复 {len(self.reservations)} 个预留")
                
        except Exception as e:
            logger.warning(f"[Shadow] 磁盘恢复失败: {e}")
            
    def _save_to_disk(self):
        """保存状态到磁盘"""
        try:
            data = {
                'timestamp': time.time(),
                'actual_balance': self.actual_balance,
                'shadow_balance': self.shadow_balance,
                'reservations': [res.to_dict() for res in self.reservations.values()]
            }
            
            with open(self.persist_path, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"[Shadow] 磁盘保存失败: {e}")
            
    def sync_actual_balance(self, balances: Dict[str, Dict[str, float]]):
        """
        同步实际余额 (SSOT增强版)
        
        Args:
            balances: {asset: {'free': x, 'locked': y}}
        """
        with self.lock:
            # 更新实际余额
            self.actual_balance = {}
            for asset, info in balances.items():
                self.actual_balance[asset] = info['free']
                
            # 如果使用事件账本，进行SSOT差分对账
            if self.event_ledger:
                self.stats['ledger_syncs'] += 1
                
                # 同步到事件账本
                ledger_consistent = self.event_ledger.sync_exchange_balances(balances)
                
                if not ledger_consistent:
                    self.stats['ledger_divergences'] += 1
                    logger.warning("[Shadow+SSOT] 事件账本与交易所余额不一致")
                    
                # 使用事件账本的可用余额作为基础
                self.shadow_balance = {}
                for asset in balances.keys():
                    ledger_available = self.event_ledger.get_available_balance(asset)
                    self.shadow_balance[asset] = ledger_available
                    
                # 减去预留
                self._subtract_reservations()
                
                logger.debug(f"[Shadow+SSOT] SSOT模式余额同步: {self.shadow_balance}")
            else:
                # 传统模式
                self._recalculate_shadow()
                logger.debug(f"[Shadow] 传统模式余额同步: {self.actual_balance}")
                
            self.last_sync = time.time()
            self.stats['sync_count'] += 1
            
    def _recalculate_shadow(self):
        """重新计算影子余额"""
        # 清理过期预留
        self._cleanup_expired()
        
        # 从实际余额开始
        self.shadow_balance = self.actual_balance.copy()
        
        # 减去所有预留
        self._subtract_reservations()
        
    def _subtract_reservations(self):
        """从当前shadow_balance中减去预留金额"""
        for asset, reservations in self.asset_reservations.items():
            total_reserved = sum(res.amount for res in reservations if not res.is_expired)
            if asset in self.shadow_balance:
                self.shadow_balance[asset] = max(0, self.shadow_balance[asset] - total_reserved)
            else:
                self.shadow_balance[asset] = 0
                
    def _cleanup_expired(self):
        """清理过期预留"""
        expired_ids = []
        
        for order_id, res in self.reservations.items():
            if res.is_expired:
                expired_ids.append(order_id)
                
        for order_id in expired_ids:
            self._remove_reservation(order_id, reason="expired")
            self.stats['expired_reserves'] += 1
            
    def _remove_reservation(self, order_id: str, reason: str = "unknown"):
        """移除预留"""
        if order_id not in self.reservations:
            return
            
        res = self.reservations.pop(order_id)
        
        # 从asset索引中移除
        if res.asset in self.asset_reservations:
            try:
                self.asset_reservations[res.asset].remove(res)
            except ValueError:
                pass
                
        logger.debug(f"[Shadow] 移除预留 {order_id}: {res.asset} {res.amount} ({reason})")
        
    def reserve(self, order_id: str, asset: str, amount: float, ttl: float = 300) -> bool:
        """
        预留资金
        
        Args:
            order_id: 订单ID
            asset: 资产类型
            amount: 需要金额
            ttl: 预留TTL（秒）
            
        Returns:
            是否预留成功
        """
        if amount <= 0:
            return False
            
        with self.lock:
            self.stats['total_reserves'] += 1
            
            # 清理过期预留
            self._cleanup_expired()
            
            # 计算实际预留金额
            actual_amount = amount * self.reserve_factor
            
            # 检查影子余额是否足够
            shadow_free = self.shadow_balance.get(asset, 0)
            
            if shadow_free < actual_amount:
                logger.debug(
                    f"[Shadow] 预留失败 {order_id}: {asset} 需要{actual_amount:.2f} "
                    f"影子余额仅{shadow_free:.2f}"
                )
                self.stats['failed_reserves'] += 1
                return False
                
            # 创建预留
            res = Reservation(order_id, asset, actual_amount, ttl=ttl)
            
            # 检查重复
            if order_id in self.reservations:
                self._remove_reservation(order_id, "duplicate")
                
            # 添加预留
            self.reservations[order_id] = res
            self.asset_reservations[asset].append(res)
            
            # 更新影子余额
            self.shadow_balance[asset] = shadow_free - actual_amount
            
            self.stats['successful_reserves'] += 1
            
            logger.debug(
                f"[Shadow] 预留成功 {order_id}: {asset} {actual_amount:.2f} "
                f"剩余影子余额: {self.shadow_balance[asset]:.2f}"
            )
            
            # 定期保存
            if self.stats['successful_reserves'] % 10 == 0:
                self._save_to_disk()
                
            return True
            
    def release(self, order_id: str, reason: str = "manual") -> bool:
        """
        释放预留
        
        Args:
            order_id: 订单ID
            reason: 释放原因
            
        Returns:
            是否释放成功
        """
        with self.lock:
            if order_id not in self.reservations:
                return False
                
            res = self.reservations[order_id]
            
            # 释放到影子余额
            self.shadow_balance[res.asset] = self.shadow_balance.get(res.asset, 0) + res.amount
            
            # 移除预留
            self._remove_reservation(order_id, reason)
            self.stats['released_reserves'] += 1
            
            logger.debug(f"[Shadow] 释放成功 {order_id}: {res.asset} {res.amount} ({reason})")
            
            return True
            
    def get_available(self, asset: str) -> float:
        """获取可用余额 (SSOT增强版 + Phase4紧急修复)"""
        with self.lock:
            # Phase4紧急修复: 如果SSOT账本不一致，临时使用实际余额
            if self.event_ledger:
                ledger_available = self.event_ledger.get_available_balance(asset) if hasattr(self.event_ledger, 'get_available_balance') else 0
                actual_available = self.actual_balance.get(asset, 0)
                
                # 如果账本与实际余额差异过大(>10 USDT)，使用实际余额
                if abs(ledger_available - actual_available) > 10.0:
                    logger.warning(f"[Shadow+SSOT Phase4Fix] 账本余额({ledger_available:.2f})与实际({actual_available:.2f})差异过大，使用实际余额 ({asset})")
                    return max(0, actual_available * 0.9)  # 90%安全边际
                
                # 如果处于冷启动但实际余额充足，使用实际余额
                if not self.event_ledger.is_ready_for_trading() and actual_available > 5.0:
                    logger.warning(f"[Shadow+SSOT Phase4Fix] 冷启动但实际余额充足({actual_available:.2f})，部分释放 ({asset})")
                    return max(0, actual_available * 0.8)  # 80%安全边际
                    
            # 如果太久没同步，返回0（安全模式）
            if time.time() - self.last_sync > self.sync_interval * 2:
                logger.warning(f"[Shadow] 余额同步过期，返回0 ({asset})")
                return 0.0
                
            return max(0, self.shadow_balance.get(asset, 0))
            
    def get_reserved(self, asset: str) -> float:
        """获取已预留金额"""
        with self.lock:
            return sum(
                res.amount for res in self.asset_reservations[asset]
                if not res.is_expired
            )
            
    def get_status(self) -> Dict:
        """获取状态信息 (SSOT增强版)"""
        with self.lock:
            self._cleanup_expired()
            
            # 按资产统计预留
            asset_stats = {}
            for asset, reservations in self.asset_reservations.items():
                valid_reservations = [res for res in reservations if not res.is_expired]
                asset_info = {
                    'actual': self.actual_balance.get(asset, 0),
                    'shadow': self.shadow_balance.get(asset, 0),
                    'reserved': sum(res.amount for res in valid_reservations),
                    'reservation_count': len(valid_reservations)
                }
                
                # 如果使用事件账本，添加账本信息
                if self.event_ledger:
                    ledger_balance = self.event_ledger.get_balance(asset)
                    asset_info.update({
                        'ledger_available': self.event_ledger.get_available_balance(asset),
                        'ledger_total': ledger_balance.total if ledger_balance else 0,
                        'ledger_pending': ledger_balance.pending_new if ledger_balance else 0
                    })
                
                asset_stats[asset] = asset_info
                
            status = {
                'last_sync': self.last_sync,
                'sync_age': time.time() - self.last_sync,
                'total_reservations': len(self.reservations),
                'asset_stats': asset_stats,
                'stats': self.stats.copy()
            }
            
            # 添加事件账本状态
            if self.event_ledger:
                ledger_status = self.event_ledger.get_status()
                status.update({
                    'ssot_enabled': True,
                    'ssot_ready': ledger_status['ready_for_trading'],
                    'ssot_cold_start': ledger_status['cold_start_mode'],
                    'ssot_events': ledger_status['total_events'],
                    'ssot_sync_age': ledger_status['sync_age_seconds']
                })
            else:
                status['ssot_enabled'] = False
                
            return status
            
    def get_summary(self) -> str:
        """获取状态摘要（单行） (SSOT增强版)"""
        status = self.get_status()
        asset_stats = status['asset_stats']
        
        summary_parts = []
        for asset, stats in asset_stats.items():
            if status.get('ssot_enabled'):
                # SSOT模式显示账本可用余额
                summary_parts.append(
                    f"{asset}({stats.get('ledger_available', 0):.0f}+{stats['reserved']:.0f})"
                )
            else:
                summary_parts.append(
                    f"{asset}({stats['shadow']:.0f}+{stats['reserved']:.0f})"
                )
                
        base_summary = (f"shadow=[{','.join(summary_parts)}] "
                       f"reserves={status['total_reservations']} "
                       f"sync_age={status['sync_age']:.0f}s")
                       
        if status.get('ssot_enabled'):
            ssot_mode = "COLD" if status.get('ssot_cold_start') else "READY"
            base_summary += f" ssot={ssot_mode} events={status.get('ssot_events', 0)}"
            
        return base_summary
        
    def record_order_event(self, event_type: EventType, order_id: str, symbol: str, 
                          side: str, asset: str, amount: float, price: float = None, 
                          fee: float = None, fee_asset: str = None, raw_data: dict = None):
        """记录订单事件到SSOT账本"""
        if not self.event_ledger:
            return
            
        event_id = f"{event_type.value}_{order_id}_{int(time.time() * 1000)}"
        event = OrderEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=time.time(),
            order_id=order_id,
            symbol=symbol,
            side=side,
            asset=asset,
            amount=amount,
            price=price,
            fee=fee,
            fee_asset=fee_asset,
            raw_data=raw_data
        )
        
        success = self.event_ledger.add_event(event)
        if success:
            logger.debug(f"[Shadow+SSOT] 记录事件: {event_type.value} {order_id}")
        else:
            logger.warning(f"[Shadow+SSOT] 事件记录失败: {event_type.value} {order_id}")
            
    def is_ready_for_trading(self) -> bool:
        """检查是否准备好交易 (SSOT增强版)"""
        with self.lock:
            if self.event_ledger:
                return self.event_ledger.is_ready_for_trading()
            else:
                # 传统模式检查
                return time.time() - self.last_sync < self.sync_interval * 2
        
    # ===== Phase 5: 机构级Shadow Balance 2.0 =====
    
    def on_execution_report(self, exec_report: Any) -> bool:
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
                symbol = exec_report.symbol or 'DOGEUSDT'
                
                # === 防重机制（基于update_id） ===
                last_record = self.exec_records.get(order_id, {})
                if last_record.get('update_id', 0) >= update_id:
                    self.stats['duplicate_events'] = self.stats.get('duplicate_events', 0) + 1
                    logger.debug(
                        "[Shadow2.0] 重复事件跳过: order=%s update_id=%d",
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
                    logger.error(
                        "[Shadow2.0] ❌ 负Delta异常: order=%s qty_delta=%s quote_delta=%s",
                        order_id, qty_delta, quote_delta
                    )
                    return False
                
                # === 判定是否有成交增量 ===
                has_trade_delta = qty_delta > 0
                
                if has_trade_delta:
                    # 使用计算出的delta值
                    dq = float(qty_delta)
                    dquote = float(quote_delta)
                    
                    if dq > 0 or dquote > 0:
                        logger.info(
                            "[Shadow2.0-Delta] ✅ oid=%s side=%s dq=%.2f dquote=%.2f",
                            order_id, side, dq, dquote
                        )
                        
                        # 应用余额增量
                        if side == 'BUY':
                            # BUY成交：USDT减少，DOGE增加
                            if dquote > 0:
                                self.actual_balance['USDT'] = max(0, self.actual_balance.get('USDT', 0) - dquote)
                                self.shadow_balance['USDT'] = max(0, self.shadow_balance.get('USDT', 0) - dquote)
                            if dq > 0:
                                self.actual_balance['DOGE'] = self.actual_balance.get('DOGE', 0) + dq
                                self.shadow_balance['DOGE'] = self.shadow_balance.get('DOGE', 0) + dq
                        elif side == 'SELL':
                            # SELL成交：DOGE减少，USDT增加
                            if dq > 0:
                                self.actual_balance['DOGE'] = max(0, self.actual_balance.get('DOGE', 0) - dq)
                                self.shadow_balance['DOGE'] = max(0, self.shadow_balance.get('DOGE', 0) - dq)
                            if dquote > 0:
                                self.actual_balance['USDT'] = self.actual_balance.get('USDT', 0) + dquote
                                self.shadow_balance['USDT'] = self.shadow_balance.get('USDT', 0) + dquote
                        
                        # === 更新执行记录 ===
                        self.exec_records[order_id] = {
                            'cum_qty': cum_qty,
                            'cum_quote': cum_quote,
                            'update_id': update_id,
                            'ts': time.time_ns(),
                            'side': side
                        }
                        
                        # 更新统计
                        self.stats['delta_success'] = self.stats.get('delta_success', 0) + 1
                        self.stats['shadow_updates'] = self.stats.get('shadow_updates', 0) + 1
                        
                        # 定期输出指标
                        if self.stats.get('delta_success', 0) % 10 == 0:
                            total_events = (
                                self.stats.get('delta_success', 0) +
                                self.stats.get('delta_negative', 0) +
                                self.stats.get('delta_zero', 0)
                            )
                            logger.info(
                                "[Shadow2.0-Metrics] Delta成功=%d 负Delta=%d "
                                "零Delta=%d 重复=%d 成功率=%.1f%%",
                                self.stats.get('delta_success', 0),
                                self.stats.get('delta_negative', 0),
                                self.stats.get('delta_zero', 0),
                                self.stats.get('duplicate_events', 0),
                                (self.stats.get('delta_success', 0) / max(total_events, 1)) * 100
                            )
                
                # === 第三步：生命周期收尾（释放剩余锁定）===
                if status in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                    self._finalize_order_unlock(order_id, side, cum_qty)
                    logger.info(f"[Shadow2.0-Final] ✅ oid={order_id} status={status} unlock完成")
                
                return True  # 永不抛异常，降级为warn
                
            except Exception as e:
                logger.warning(f"[Shadow2.0] ExecutionReport处理跳过: {e}")
                return True  # 降级处理，不中断流程
    
    def _check_cum_increased(self, order_id: str, cum_qty: float) -> bool:
        """检查累计量是否增加（用于检测成交）"""
        if not hasattr(self, '_cum_qty_seen'):
            self._cum_qty_seen = {}
        
        if order_id not in self._cum_qty_seen:
            self._cum_qty_seen[order_id] = 0
        
        old_cum = self._cum_qty_seen[order_id]
        if cum_qty > old_cum:
            self._cum_qty_seen[order_id] = cum_qty
            return True
        return False
    
    def _get_cum_delta(self, order_id: str, cum_qty: float) -> float:
        """获取累计量增量"""
        if not hasattr(self, '_cum_qty_seen'):
            self._cum_qty_seen = {}
            return 0
        return cum_qty - self._cum_qty_seen.get(order_id, 0)
    
    def _get_cum_quote_delta(self, order_id: str, cum_quote: float) -> float:
        """获取累计成交额增量"""
        if not hasattr(self, '_cum_quote_seen'):
            self._cum_quote_seen = {}
        
        if order_id not in self._cum_quote_seen:
            self._cum_quote_seen[order_id] = 0
        
        old_cum = self._cum_quote_seen[order_id]
        delta = cum_quote - old_cum
        if delta > 0:
            self._cum_quote_seen[order_id] = cum_quote
        return delta
    
    def _finalize_order_unlock(self, order_id: str, side: str, cum_qty: float):
        """生命周期收尾：释放剩余锁定"""
        # 清理累计缓存
        if hasattr(self, '_cum_qty_seen') and order_id in self._cum_qty_seen:
            del self._cum_qty_seen[order_id]
        if hasattr(self, '_cum_quote_seen') and order_id in self._cum_quote_seen:
            del self._cum_quote_seen[order_id]
        
        # 释放锁定余额（如果有的话）
        if hasattr(self, 'locked_balance'):
            if side == 'BUY' and 'USDT' in self.locked_balance:
                self.locked_balance['USDT'] = max(0, self.locked_balance.get('USDT', 0))
            elif side == 'SELL' and 'DOGE' in self.locked_balance:
                self.locked_balance['DOGE'] = max(0, self.locked_balance.get('DOGE', 0))
    
    def _atomic_balance_update_on_fill(self, order_id: str, symbol: str, side: str, 
                                     executed_qty: float, cumulative_quote_qty: float):
        """原子更新：订单成交时的余额变化"""
        if side == 'BUY':
            # BUY成交：USDT减少，DOGE增加
            self.actual_balance['USDT'] -= cumulative_quote_qty
            self.actual_balance['DOGE'] = self.actual_balance.get('DOGE', 0) + executed_qty
            logger.debug(f"[Shadow2.0] BUY成交原子更新: USDT-{cumulative_quote_qty:.2f}, DOGE+{executed_qty:.2f}")
        else:
            # SELL成交：DOGE减少，USDT增加  
            self.actual_balance['DOGE'] -= executed_qty
            self.actual_balance['USDT'] = self.actual_balance.get('USDT', 0) + cumulative_quote_qty
            logger.debug(f"[Shadow2.0] SELL成交原子更新: DOGE-{executed_qty:.2f}, USDT+{cumulative_quote_qty:.2f}")
        
        # 释放订单预留
        self._remove_reservation(order_id, "filled")
        
        # 立即重新计算Shadow Balance
        self._recalculate_shadow()
    
    def _atomic_balance_update_on_new(self, order_id: str, symbol: str, side: str):
        """原子更新：新订单确认时"""
        # NEW订单确认，预留已经在reserve()时处理，这里只需要确认
        logger.debug(f"[Shadow2.0] 订单NEW确认: {order_id} {side}")
    
    def _atomic_balance_update_on_cancel(self, order_id: str):
        """原子更新：订单取消时释放预留"""
        self._remove_reservation(order_id, "canceled")
        self._recalculate_shadow()
        logger.debug(f"[Shadow2.0] 订单取消释放预留: {order_id}")
    
    def ssot_repair(self, exchange_balances: Dict[str, Dict[str, float]]) -> bool:
        """
        Phase 5 机构级功能：SSOT-REPAIR账实对账自愈
        
        差异超阈值时，以交易所回报为准进行修复
        
        Args:
            exchange_balances: 交易所真实余额 {'USDT': {'free': 100, 'locked': 50}, ...}
        
        Returns:
            bool: 修复是否成功
        """
        with self.lock:
            try:
                repair_threshold = 1.0  # 差异阈值 1 USDT
                repaired = False
                
                for asset, balance_data in exchange_balances.items():
                    exchange_free = balance_data.get('free', 0)
                    shadow_free = self.actual_balance.get(asset, 0)
                    
                    diff = abs(exchange_free - shadow_free)
                    
                    if diff > repair_threshold:
                        logger.warning(f"[SSOT-REPAIR] {asset}余额差异过大: "
                                     f"交易所{exchange_free:.2f} vs Shadow{shadow_free:.2f} "
                                     f"差异{diff:.2f}")
                        
                        # 以交易所余额为准进行修复
                        old_balance = self.actual_balance.get(asset, 0)
                        self.actual_balance[asset] = exchange_free
                        
                        logger.warning(f"[SSOT-REPAIR] {asset}余额已修复: "
                                     f"{old_balance:.2f} → {exchange_free:.2f}")
                        
                        repaired = True
                
                if repaired:
                    # 重新计算Shadow Balance
                    self._recalculate_shadow()
                    logger.info("[SSOT-REPAIR] 账实对账修复完成，Shadow Balance已重新计算")
                
                return True
                
            except Exception as e:
                logger.error(f"[SSOT-REPAIR] 修复失败: {e}")
                return False
    
    def three_dimensional_audit(self) -> Dict[str, Any]:
        """
        Phase 5 机构级功能：三维对账（free/locked/onbook）
        
        每5-10秒执行一次全面对账
        
        Returns:
            Dict: 对账结果和统计信息
        """
        with self.lock:
            audit_result = {
                'timestamp': time.time(),
                'assets': {},
                'inconsistencies': [],
                'repair_needed': False
            }
            
            try:
                # 对每个资产进行三维对账
                for asset in ['USDT', 'DOGE']:
                    actual_free = self.actual_balance.get(asset, 0)
                    reserved = self.get_reserved(asset)
                    shadow_available = self.get_available(asset)
                    
                    # 计算理论上应该可用的余额
                    theoretical_available = max(0, actual_free - reserved)
                    
                    # 检查一致性
                    inconsistent = abs(shadow_available - theoretical_available) > 0.01
                    
                    audit_result['assets'][asset] = {
                        'actual_free': actual_free,
                        'reserved': reserved,
                        'shadow_available': shadow_available,
                        'theoretical_available': theoretical_available,
                        'consistent': not inconsistent,
                        'diff': abs(shadow_available - theoretical_available)
                    }
                    
                    if inconsistent:
                        audit_result['inconsistencies'].append({
                            'asset': asset,
                            'shadow': shadow_available,
                            'theoretical': theoretical_available,
                            'diff': shadow_available - theoretical_available
                        })
                        audit_result['repair_needed'] = True
                
                # 记录对账结果
                if audit_result['inconsistencies']:
                    logger.warning(f"[3D-AUDIT] 发现{len(audit_result['inconsistencies'])}个不一致项")
                    for incon in audit_result['inconsistencies']:
                        logger.warning(f"[3D-AUDIT] {incon['asset']}: "
                                     f"Shadow{incon['shadow']:.2f} vs 理论{incon['theoretical']:.2f}")
                else:
                    logger.debug("[3D-AUDIT] 三维对账通过，余额一致")
                
                return audit_result
                
            except Exception as e:
                logger.error(f"[3D-AUDIT] 三维对账失败: {e}")
                return {'error': str(e), 'timestamp': time.time()}

    def force_cleanup(self):
        """强制清理（用于测试）"""
        with self.lock:
            self.reservations.clear()
            self.asset_reservations.clear()
            self._recalculate_shadow()


# 全局Shadow Balance实例
_shadow_instance = None
_shadow_lock = threading.Lock()


def get_shadow_balance(sync_interval: float = 30, reserve_factor: float = 1.1) -> ShadowBalance:
    """获取全局Shadow Balance实例"""
    global _shadow_instance
    
    with _shadow_lock:
        if _shadow_instance is None:
            _shadow_instance = ShadowBalance(sync_interval, reserve_factor)
            
        return _shadow_instance


def reset_shadow_balance():
    """重置全局实例（用于测试）"""
    global _shadow_instance
    with _shadow_lock:
        _shadow_instance = None


if __name__ == "__main__":
    # 简单测试
    sb = ShadowBalance()
    
    # 模拟同步余额
    sb.sync_actual_balance({
        'USDT': {'free': 1000.0, 'locked': 0},
        'DOGE': {'free': 5000.0, 'locked': 0}
    })
    
    # 预留测试
    print("=== 预留测试 ===")
    print(f"初始状态: {sb.get_summary()}")
    
    # 成功预留
    result = sb.reserve('order1', 'USDT', 100)
    print(f"预留100 USDT: {result}")
    print(f"预留后: {sb.get_summary()}")
    
    # 再次预留
    result = sb.reserve('order2', 'USDT', 200) 
    print(f"再预留200 USDT: {result}")
    print(f"预留后: {sb.get_summary()}")
    
    # 释放测试
    sb.release('order1')
    print(f"释放order1后: {sb.get_summary()}")
    
    print(f"详细状态: {sb.get_status()}")