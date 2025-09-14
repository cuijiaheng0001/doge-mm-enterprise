#!/usr/bin/env python3
"""
Event Ledger - 事件驱动账本系统
实现单一真实源(SSOT)的订单事件回放和差分对账机制
"""

import time
import threading
import logging
from typing import Dict, List, Optional, Tuple, Any, NamedTuple
from collections import defaultdict, deque
from enum import Enum
import json
import os

logger = logging.getLogger(__name__)


class EventType(Enum):
    """订单事件类型"""
    NEW = "NEW"           # 新订单
    ACK = "ACK"           # 订单确认  
    TRADE = "TRADE"       # 成交
    CANCELED = "CANCELED" # 撤销
    REJECT = "REJECT"     # 拒绝
    BALANCE_SYNC = "BALANCE_SYNC"  # 余额同步


class OrderEvent(NamedTuple):
    """订单事件记录"""
    event_id: str
    event_type: EventType
    timestamp: float
    order_id: str
    symbol: str
    side: str  # BUY/SELL
    asset: str  # 影响的资产
    amount: float  # 金额变化
    price: Optional[float] = None
    fee: Optional[float] = None
    fee_asset: Optional[str] = None
    exchange_timestamp: Optional[float] = None
    raw_data: Optional[Dict] = None


class LedgerBalance:
    """账本余额状态"""
    
    def __init__(self, asset: str):
        self.asset = asset
        self.free = 0.0
        self.locked = 0.0
        self.pending_new = 0.0  # 待确认新订单
        self.pending_cancel = 0.0  # 待确认撤销
        self.last_update = time.time()
        
    @property
    def total(self) -> float:
        return self.free + self.locked
        
    @property
    def available(self) -> float:
        """考虑待处理订单的可用余额"""
        return max(0, self.free - self.pending_new)
        
    def to_dict(self) -> Dict:
        return {
            'asset': self.asset,
            'free': self.free,
            'locked': self.locked,
            'pending_new': self.pending_new,
            'pending_cancel': self.pending_cancel,
            'available': self.available,
            'total': self.total,
            'last_update': self.last_update
        }


class EventLedger:
    """事件驱动账本 - NEW/ACK/TRADE/CANCELED/REJECT事件回放"""
    
    def __init__(self, max_events: int = 10000, persist_path: str = "/tmp/event_ledger.json"):
        """
        初始化事件账本
        
        Args:
            max_events: 最大事件缓存数量
            persist_path: 持久化文件路径
        """
        self.max_events = max_events
        self.persist_path = persist_path
        self.lock = threading.RLock()
        
        # 事件存储
        self.events = deque(maxlen=max_events)  # 有限长度队列
        self.event_index = {}  # event_id -> event 快速查找
        
        # 账本状态
        self.balances = {}  # asset -> LedgerBalance
        self.orders = {}  # order_id -> order_info
        
        # 同步状态
        self.last_exchange_sync = 0
        self.exchange_balances = {}  # 交易所余额快照
        self.divergence_threshold = 0.001  # 0.1%差异阈值
        
        # 统计
        self.stats = {
            'total_events': 0,
            'events_by_type': defaultdict(int),
            'balance_divergences': 0,
            'reconciliations': 0,
            'replay_count': 0,
            'last_replay_duration_ms': 0
        }
        
        # 冷启动状态
        self.cold_start_mode = True
        self.min_consistency_checks = 3
        self.consistency_checks_passed = 0
        
        # 尝试从持久化恢复
        self._load_from_disk()
        
    def _load_from_disk(self):
        """从磁盘恢复事件和状态"""
        try:
            if os.path.exists(self.persist_path):
                with open(self.persist_path, 'r') as f:
                    data = json.load(f)
                    
                # 恢复事件
                for event_data in data.get('events', []):
                    event = OrderEvent(**event_data)
                    self.events.append(event)
                    self.event_index[event.event_id] = event
                    
                # 恢复余额状态
                for asset, balance_data in data.get('balances', {}).items():
                    balance = LedgerBalance(asset)
                    for k, v in balance_data.items():
                        if hasattr(balance, k):
                            setattr(balance, k, v)
                    self.balances[asset] = balance
                    
                # 恢复统计
                self.stats.update(data.get('stats', {}))
                
                logger.info(f"[Ledger] 从磁盘恢复 {len(self.events)} 个事件")
                
        except Exception as e:
            logger.warning(f"[Ledger] 磁盘恢复失败: {e}")
            
    def _save_to_disk(self):
        """保存状态到磁盘"""
        try:
            data = {
                'timestamp': time.time(),
                'events': [event._asdict() for event in list(self.events)],
                'balances': {asset: balance.to_dict() for asset, balance in self.balances.items()},
                'stats': dict(self.stats)
            }
            
            # 转换Enum为字符串
            for event_data in data['events']:
                if isinstance(event_data.get('event_type'), EventType):
                    event_data['event_type'] = event_data['event_type'].value
                    
            with open(self.persist_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
                
        except Exception as e:
            logger.error(f"[Ledger] 磁盘保存失败: {e}")
            
    def add_event(self, event: OrderEvent) -> bool:
        """添加订单事件到账本"""
        with self.lock:
            # 检查重复事件
            if event.event_id in self.event_index:
                logger.debug(f"[Ledger] 重复事件 {event.event_id}")
                return False
                
            # 添加事件
            self.events.append(event)
            self.event_index[event.event_id] = event
            
            # 更新统计
            self.stats['total_events'] += 1
            self.stats['events_by_type'][event.event_type.value] += 1
            
            # 应用事件到账本状态
            self._apply_event(event)
            
            logger.debug(f"[Ledger] 添加事件: {event.event_type.value} {event.order_id}")
            
            # 定期保存
            if self.stats['total_events'] % 100 == 0:
                self._save_to_disk()
                
            return True
            
    def _apply_event(self, event: OrderEvent):
        """应用事件到账本余额"""
        if event.asset not in self.balances:
            self.balances[event.asset] = LedgerBalance(event.asset)
            
        balance = self.balances[event.asset]
        
        if event.event_type == EventType.NEW:
            # 新订单: 预锁定资金
            if event.side == 'BUY':
                # 买单预锁定quote asset (USDT)
                balance.pending_new += event.amount * (event.price or 0)
            else:
                # 卖单预锁定base asset (DOGE)
                balance.pending_new += event.amount
                
        elif event.event_type == EventType.ACK:
            # 订单确认: 从pending转为locked
            if event.side == 'BUY':
                locked_amount = event.amount * (event.price or 0)
                balance.pending_new = max(0, balance.pending_new - locked_amount)
                balance.free = max(0, balance.free - locked_amount) 
                balance.locked += locked_amount
            else:
                balance.pending_new = max(0, balance.pending_new - event.amount)
                balance.free = max(0, balance.free - event.amount)
                balance.locked += event.amount
                
            # 记录订单状态
            self.orders[event.order_id] = {
                'side': event.side,
                'amount': event.amount,
                'price': event.price,
                'locked_amount': event.amount * (event.price or 0) if event.side == 'BUY' else event.amount,
                'status': 'OPEN'
            }
            
        elif event.event_type == EventType.TRADE:
            # 成交: 调整余额
            if event.side == 'BUY':
                # 买入: 减少USDT locked, 增加DOGE free
                usdt_balance = self.balances.get('USDT')
                doge_balance = self.balances.get('DOGE') 
                if usdt_balance and doge_balance:
                    trade_value = event.amount * (event.price or 0)
                    usdt_balance.locked = max(0, usdt_balance.locked - trade_value)
                    doge_balance.free += event.amount
                    # 扣除手续费
                    if event.fee and event.fee_asset:
                        fee_balance = self.balances.get(event.fee_asset)
                        if fee_balance:
                            fee_balance.free = max(0, fee_balance.free - event.fee)
            else:
                # 卖出: 减少DOGE locked, 增加USDT free
                doge_balance = self.balances.get('DOGE')
                usdt_balance = self.balances.get('USDT')
                if doge_balance and usdt_balance:
                    doge_balance.locked = max(0, doge_balance.locked - event.amount)
                    trade_value = event.amount * (event.price or 0)
                    usdt_balance.free += trade_value
                    # 扣除手续费
                    if event.fee and event.fee_asset:
                        fee_balance = self.balances.get(event.fee_asset)
                        if fee_balance:
                            fee_balance.free = max(0, fee_balance.free - event.fee)
                            
        elif event.event_type == EventType.CANCELED:
            # 撤销: 释放锁定资金
            order_info = self.orders.get(event.order_id)
            if order_info:
                if order_info['side'] == 'BUY':
                    balance.locked = max(0, balance.locked - order_info['locked_amount'])
                    balance.free += order_info['locked_amount']
                else:
                    balance.locked = max(0, balance.locked - order_info['locked_amount'])
                    balance.free += order_info['locked_amount']
                order_info['status'] = 'CANCELED'
                
        elif event.event_type == EventType.REJECT:
            # 拒绝: 释放pending资金
            if event.side == 'BUY':
                rejected_amount = event.amount * (event.price or 0)
                balance.pending_new = max(0, balance.pending_new - rejected_amount)
            else:
                balance.pending_new = max(0, balance.pending_new - event.amount)
                
        balance.last_update = time.time()
        
    def sync_exchange_balances(self, exchange_balances: Dict[str, Dict[str, float]]):
        """同步交易所余额并检查差异"""
        with self.lock:
            self.exchange_balances = exchange_balances
            self.last_exchange_sync = time.time()
            
            # 检查差异
            divergences = self._check_balance_divergence()
            
            if divergences:
                self.stats['balance_divergences'] += len(divergences)
                logger.warning(f"[Ledger] 发现余额差异: {divergences}")
                
                # 差异过大时触发重置
                max_divergence = max(abs(d['divergence_pct']) for d in divergences)
                if max_divergence > self.divergence_threshold * 100:
                    logger.error(f"[Ledger] 余额差异过大 {max_divergence:.2f}%, 触发重置")
                    self._force_reconcile()
                    return False
            else:
                # 一致性检查通过
                self.consistency_checks_passed += 1
                if self.cold_start_mode and self.consistency_checks_passed >= self.min_consistency_checks:
                    self.cold_start_mode = False
                    logger.info("[Ledger] 冷启动完成, 账本与交易所一致")
                    
            return len(divergences) == 0
            
    def _check_balance_divergence(self) -> List[Dict]:
        """检查账本与交易所余额差异"""
        divergences = []
        
        for asset, exchange_info in self.exchange_balances.items():
            exchange_free = exchange_info.get('free', 0)
            exchange_total = exchange_free + exchange_info.get('locked', 0)
            
            ledger_balance = self.balances.get(asset)
            if ledger_balance:
                ledger_total = ledger_balance.total
                
                if exchange_total > 0:
                    divergence_pct = (ledger_total - exchange_total) / exchange_total * 100
                else:
                    divergence_pct = 0 if ledger_total == 0 else float('inf')
                    
                if abs(divergence_pct) > self.divergence_threshold * 100:
                    divergences.append({
                        'asset': asset,
                        'ledger_total': ledger_total,
                        'exchange_total': exchange_total,
                        'divergence_pct': divergence_pct
                    })
                    
        return divergences
        
    def _force_reconcile(self):
        """强制对账 - 重置账本到交易所状态"""
        with self.lock:
            logger.info("[Ledger] 开始强制对账")
            
            # 重置账本余额
            for asset, exchange_info in self.exchange_balances.items():
                if asset not in self.balances:
                    self.balances[asset] = LedgerBalance(asset)
                    
                balance = self.balances[asset]
                balance.free = exchange_info.get('free', 0)
                balance.locked = exchange_info.get('locked', 0)
                balance.pending_new = 0
                balance.pending_cancel = 0
                balance.last_update = time.time()
                
            # 重置订单状态
            self.orders.clear()
            self.consistency_checks_passed = 0
            self.cold_start_mode = True
            self.stats['reconciliations'] += 1
            
            logger.info("[Ledger] 强制对账完成")
            
    def replay_events(self, from_timestamp: Optional[float] = None) -> int:
        """重放事件重建账本状态"""
        with self.lock:
            start_time = time.time()
            
            # 备份当前状态
            backup_balances = {asset: balance.to_dict() for asset, balance in self.balances.items()}
            
            try:
                # 重置状态
                self.balances.clear()
                self.orders.clear()
                
                # 筛选事件
                events_to_replay = []
                for event in self.events:
                    if from_timestamp is None or event.timestamp >= from_timestamp:
                        events_to_replay.append(event)
                        
                # 重放事件
                for event in events_to_replay:
                    self._apply_event(event)
                    
                replay_count = len(events_to_replay)
                duration_ms = (time.time() - start_time) * 1000
                
                self.stats['replay_count'] += 1
                self.stats['last_replay_duration_ms'] = duration_ms
                
                logger.info(f"[Ledger] 重放 {replay_count} 个事件, 耗时 {duration_ms:.1f}ms")
                return replay_count
                
            except Exception as e:
                # 恢复备份状态
                logger.error(f"[Ledger] 重放失败: {e}, 恢复备份状态")
                for asset, balance_data in backup_balances.items():
                    balance = LedgerBalance(asset)
                    for k, v in balance_data.items():
                        if hasattr(balance, k):
                            setattr(balance, k, v)
                    self.balances[asset] = balance
                return 0
                
    def is_ready_for_trading(self) -> bool:
        """检查是否准备好交易 - 冷启动完成且账本一致"""
        with self.lock:
            if self.cold_start_mode:
                return False
                
            # 检查最近同步时间
            if time.time() - self.last_exchange_sync > 60:  # 1分钟
                logger.warning("[Ledger] 交易所余额同步过期")
                return False
                
            return True
            
    def get_balance(self, asset: str) -> Optional[LedgerBalance]:
        """获取资产余额"""
        with self.lock:
            return self.balances.get(asset)
            
    def get_available_balance(self, asset: str) -> float:
        """获取可用余额"""
        with self.lock:
            balance = self.balances.get(asset)
            return balance.available if balance else 0.0
            
    def get_status(self) -> Dict:
        """获取账本状态"""
        with self.lock:
            return {
                'cold_start_mode': self.cold_start_mode,
                'ready_for_trading': self.is_ready_for_trading(),
                'consistency_checks_passed': self.consistency_checks_passed,
                'last_exchange_sync': self.last_exchange_sync,
                'sync_age_seconds': time.time() - self.last_exchange_sync,
                'total_events': len(self.events),
                'balances': {asset: balance.to_dict() for asset, balance in self.balances.items()},
                'stats': dict(self.stats)
            }
            
    def get_summary(self) -> str:
        """获取账本状态摘要"""
        status = self.get_status()
        balance_summary = []
        
        for asset, balance_info in status['balances'].items():
            balance_summary.append(f"{asset}({balance_info['available']:.0f})")
            
        mode = "COLD" if status['cold_start_mode'] else "READY"
        return (f"ledger=[{','.join(balance_summary)}] "
               f"events={status['total_events']} "
               f"mode={mode} sync_age={status['sync_age_seconds']:.0f}s")


# 全局事件账本实例
_ledger_instance = None
_ledger_lock = threading.Lock()


def get_event_ledger(max_events: int = 10000) -> EventLedger:
    """获取全局事件账本实例"""
    global _ledger_instance
    
    with _ledger_lock:
        if _ledger_instance is None:
            _ledger_instance = EventLedger(max_events)
            
        return _ledger_instance


def reset_event_ledger():
    """重置全局实例（用于测试）"""
    global _ledger_instance
    with _ledger_lock:
        _ledger_instance = None


if __name__ == "__main__":
    # 简单测试
    ledger = EventLedger()
    
    # 模拟交易所余额同步
    ledger.sync_exchange_balances({
        'USDT': {'free': 1000.0, 'locked': 0},
        'DOGE': {'free': 5000.0, 'locked': 0}
    })
    
    print(f"初始状态: {ledger.get_summary()}")
    
    # 模拟订单事件
    events = [
        OrderEvent("e1", EventType.NEW, time.time(), "order1", "DOGEUSDT", "BUY", "USDT", 100, 0.1),
        OrderEvent("e2", EventType.ACK, time.time(), "order1", "DOGEUSDT", "BUY", "USDT", 100, 0.1),
        OrderEvent("e3", EventType.TRADE, time.time(), "order1", "DOGEUSDT", "BUY", "DOGE", 100, 0.1, fee=0.01, fee_asset="DOGE")
    ]
    
    for event in events:
        ledger.add_event(event)
        print(f"{event.event_type.value}后: {ledger.get_summary()}")
        
    print(f"详细状态: {ledger.get_status()}")