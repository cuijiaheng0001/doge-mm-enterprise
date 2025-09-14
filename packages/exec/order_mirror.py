#!/usr/bin/env python3
"""
Order Mirror Pro - 订单镜像专业版
差分对账系统，减少REST API调用
"""

import time
import json
import hashlib
import logging
import asyncio
from typing import Dict, List, Optional, Set, Any
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


class OrderState:
    """订单状态"""
    
    def __init__(self, order_id: str, client_order_id: str, side: str, 
                 price: float, qty: float, status: str = 'NEW'):
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.side = side
        self.price = price
        self.orig_qty = qty
        self.filled_qty = 0.0
        self.status = status
        self.create_time = time.time()
        self.update_time = time.time()
        self.fills = []  # 成交记录
        
    @property
    def remaining_qty(self) -> float:
        """剩余数量"""
        return self.orig_qty - self.filled_qty
        
    @property
    def is_active(self) -> bool:
        """是否活跃"""
        return self.status in ['NEW', 'PARTIALLY_FILLED']
        
    def add_fill(self, qty: float, price: float, fill_time: float = None):
        """添加成交"""
        self.filled_qty += qty
        self.update_time = fill_time or time.time()
        
        self.fills.append({
            'qty': qty,
            'price': price,
            'time': self.update_time
        })
        
        # 更新状态
        if self.filled_qty >= self.orig_qty:
            self.status = 'FILLED'
        elif self.filled_qty > 0:
            self.status = 'PARTIALLY_FILLED'
            
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'client_order_id': self.client_order_id,
            'side': self.side,
            'price': self.price,
            'orig_qty': self.orig_qty,
            'filled_qty': self.filled_qty,
            'status': self.status,
            'create_time': self.create_time,
            'update_time': self.update_time,
            'fills': self.fills
        }
        
    @classmethod
    def from_dict(cls, data: Dict) -> 'OrderState':
        """从字典创建"""
        order = cls(
            data['order_id'],
            data['client_order_id'],
            data['side'],
            data['price'],
            data['orig_qty'],
            data['status']
        )
        order.filled_qty = data['filled_qty']
        order.create_time = data['create_time']
        order.update_time = data['update_time']
        order.fills = data['fills']
        return order


class OrderMirrorPro:
    """订单镜像专业版 - 差分对账系统"""
    
    def __init__(self, exchange, persist_path: str = "/tmp/order_mirror.json",
                 sync_interval: float = 60):
        """
        初始化Order Mirror Pro
        
        Args:
            exchange: 交易所接口
            persist_path: 持久化路径
            sync_interval: 同步间隔（秒）
        """
        self.exchange = exchange
        self.persist_path = persist_path
        self.sync_interval = sync_interval
        
        # 本地状态
        self.local_orders = {}  # order_id -> OrderState
        self.client_id_map = {}  # client_order_id -> order_id
        
        # 对账状态
        self.last_sync_time = 0
        self.last_sync_hash = None
        self.sync_in_progress = False
        
        # 统计
        self.stats = {
            'syncs_performed': 0,
            'orders_added': 0,
            'orders_updated': 0,
            'orders_removed': 0,
            'discrepancies_found': 0,
            'differential_syncs': 0,
            'full_syncs': 0,
            'rest_calls_saved': 0
        }
        
        # Phase 4: DLE Pro回调
        self.dle_pro_callback = None
        
        # 尝试恢复
        self._load_from_disk()
        
    def _load_from_disk(self):
        """从磁盘恢复状态"""
        try:
            if Path(self.persist_path).exists():
                with open(self.persist_path, 'r') as f:
                    data = json.load(f)
                    
                # 恢复订单状态
                for order_data in data.get('orders', []):
                    order = OrderState.from_dict(order_data)
                    self.local_orders[order.order_id] = order
                    self.client_id_map[order.client_order_id] = order.order_id
                    
                # 恢复元数据
                self.last_sync_time = data.get('last_sync_time', 0)
                self.last_sync_hash = data.get('last_sync_hash')
                
                logger.info(f"[OrderMirror] 从磁盘恢复 {len(self.local_orders)} 个订单")
                
        except Exception as e:
            logger.warning(f"[OrderMirror] 磁盘恢复失败: {e}")
            
    def _save_to_disk(self):
        """保存状态到磁盘"""
        try:
            data = {
                'timestamp': time.time(),
                'last_sync_time': self.last_sync_time,
                'last_sync_hash': self.last_sync_hash,
                'orders': [order.to_dict() for order in self.local_orders.values()]
            }
            
            # 原子写入
            temp_path = self.persist_path + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            Path(temp_path).rename(self.persist_path)
            
        except Exception as e:
            logger.error(f"[OrderMirror] 磁盘保存失败: {e}")
            
    def _calculate_state_hash(self, orders: List[Dict]) -> str:
        """计算订单状态哈希"""
        # 按order_id排序确保一致性
        sorted_orders = sorted(orders, key=lambda x: x.get('orderId', ''))
        
        # 只考虑关键字段
        hash_data = []
        for order in sorted_orders:
            hash_data.append({
                'id': order.get('orderId'),
                'status': order.get('status'),
                'filled': order.get('executedQty')
            })
            
        hash_str = json.dumps(hash_data, sort_keys=True)
        return hashlib.md5(hash_str.encode()).hexdigest()
        
    def add_local_order(self, order_id: str, client_order_id: str, 
                       side: str, price: float, qty: float):
        """添加本地订单"""
        order = OrderState(order_id, client_order_id, side, price, qty)
        self.local_orders[order_id] = order
        self.client_id_map[client_order_id] = order_id
        self.stats['orders_added'] += 1
        
        logger.debug(f"[OrderMirror] 添加本地订单: {order_id}")
        
    def update_local_order(self, order_id: str, **kwargs):
        """更新本地订单"""
        if order_id not in self.local_orders:
            logger.warning(f"[OrderMirror] 订单不存在: {order_id}")
            return
            
        order = self.local_orders[order_id]
        
        # 更新字段
        if 'status' in kwargs:
            order.status = kwargs['status']
        if 'filled_qty' in kwargs:
            order.filled_qty = kwargs['filled_qty']
            
        order.update_time = time.time()
        self.stats['orders_updated'] += 1
        
        logger.debug(f"[OrderMirror] 更新本地订单: {order_id}")
        
    def remove_local_order(self, order_id: str):
        """移除本地订单"""
        if order_id not in self.local_orders:
            return
            
        order = self.local_orders.pop(order_id)
        self.client_id_map.pop(order.client_order_id, None)
        self.stats['orders_removed'] += 1
        
        logger.debug(f"[OrderMirror] 移除本地订单: {order_id}")
        
    def get_active_orders(self) -> List[OrderState]:
        """获取活跃订单"""
        return [order for order in self.local_orders.values() if order.is_active]
        
    def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderState]:
        """通过客户端ID获取订单"""
        order_id = self.client_id_map.get(client_order_id)
        if order_id:
            return self.local_orders.get(order_id)
        return None
    
    async def upsert_from_event(self, order_id: str, event_data: Dict):
        """UDS Phase 1: 从WebSocket事件更新订单（新订单或更新）"""
        status = event_data.get('X')  # orderStatus
        
        if order_id not in self.local_orders:
            # 新订单
            self.add_local_order(
                order_id=order_id,
                client_order_id=event_data.get('c', f"UDS-{order_id}"),
                side=event_data.get('S'),
                price=float(event_data.get('p', 0)),
                qty=float(event_data.get('q', 0))
            )
            logger.debug(f"[OrderMirror] UDS新订单: {order_id}")
        else:
            # 更新现有订单
            order = self.local_orders[order_id]
            order.status = status
            order.filled_qty = float(event_data.get('z', 0))  # cumulativeFilledQty
            order.update_time = event_data.get('T', time.time() * 1000) / 1000
            
            # 如果有新成交，记录
            last_filled = float(event_data.get('l', 0))  # lastFilledQty
            if last_filled > 0:
                order.add_fill(
                    qty=last_filled,
                    price=float(event_data.get('L', 0)),  # lastFilledPrice
                    fill_time=order.update_time
                )
            
            logger.debug(f"[OrderMirror] UDS更新: {order_id} status={status}")
        
        self.stats['orders_updated'] += 1
    
    async def close_from_event(self, order_id: str):
        """UDS Phase 1: 从WebSocket事件关闭订单"""
        if order_id in self.local_orders:
            order = self.local_orders[order_id]
            
            # 标记为终态
            if order.filled_qty >= order.orig_qty:
                order.status = 'FILLED'
            elif order.status not in ['FILLED', 'CANCELED', 'EXPIRED', 'REJECTED']:
                order.status = 'CANCELED'
            
            # 从活跃列表移除
            self.remove_local_order(order_id)
            logger.debug(f"[OrderMirror] UDS关闭订单: {order_id}")
    
    async def upsert_from_rest(self, order_id: str, rest_data: Dict):
        """UDS Phase 1: 从REST响应更新（用于种子同步）"""
        if order_id not in self.local_orders:
            self.add_local_order(
                order_id=order_id,
                client_order_id=rest_data.get('clientOrderId', f"REST-{order_id}"),
                side=rest_data.get('side'),
                price=float(rest_data.get('price', 0)),
                qty=float(rest_data.get('origQty', 0))
            )
        
        # 更新状态
        order = self.local_orders[order_id]
        order.status = rest_data.get('status', 'NEW')
        order.filled_qty = float(rest_data.get('executedQty', 0))
        order.update_time = rest_data.get('updateTime', time.time() * 1000) / 1000
        
        logger.debug(f"[OrderMirror] REST种子同步: {order_id}")
    
    def set_dle_callback(self, callback_func):
        """设置DLE Pro回调函数"""
        self.dle_pro_callback = callback_func
        logger.debug("[OrderMirror] 设置DLE Pro回调")
        
    async def differential_sync(self, force_full_sync: bool = False) -> Dict:
        """
        差分同步
        
        Args:
            force_full_sync: 强制全量同步
            
        Returns:
            同步结果
        """
        if self.sync_in_progress:
            logger.debug("[OrderMirror] 同步进行中，跳过")
            return {'skipped': True}
            
        self.sync_in_progress = True
        sync_result = {'type': 'differential', 'changes': 0, 'errors': []}
        
        try:
            # 获取远程订单
            remote_orders = await self.exchange.get_open_orders()
            
            # 计算远程哈希
            remote_hash = self._calculate_state_hash(remote_orders)
            
            # 检查是否需要同步
            if not force_full_sync and remote_hash == self.last_sync_hash:
                logger.debug("[OrderMirror] 远程状态未变化，跳过同步")
                self.stats['rest_calls_saved'] += 1
                return {'skipped': True, 'reason': 'no_changes'}
                
            # 检查是否需要全量同步
            time_since_sync = time.time() - self.last_sync_time
            needs_full_sync = (
                force_full_sync or 
                self.last_sync_hash is None or
                time_since_sync > self.sync_interval * 10  # 10倍同步间隔
            )
            
            if needs_full_sync:
                sync_result = await self._full_sync(remote_orders)
                self.stats['full_syncs'] += 1
            else:
                sync_result = await self._incremental_sync(remote_orders)
                self.stats['differential_syncs'] += 1
                
            # 更新状态
            self.last_sync_time = time.time()
            self.last_sync_hash = remote_hash
            self.stats['syncs_performed'] += 1
            
            # 定期保存
            if self.stats['syncs_performed'] % 5 == 0:
                self._save_to_disk()
                
            logger.debug(
                f"[OrderMirror] 同步完成: {sync_result['type']} "
                f"changes={sync_result['changes']}"
            )
            
            return sync_result
            
        except Exception as e:
            logger.error(f"[OrderMirror] 同步异常: {e}")
            sync_result['errors'].append(str(e))
            return sync_result
            
        finally:
            self.sync_in_progress = False
            
    async def _full_sync(self, remote_orders: List[Dict]) -> Dict:
        """全量同步"""
        sync_result = {'type': 'full', 'changes': 0, 'errors': []}
        
        # 构建远程订单映射
        remote_map = {}
        for order_data in remote_orders:
            order_id = order_data['orderId']
            remote_map[order_id] = order_data
            
        # 检查本地订单
        local_order_ids = set(self.local_orders.keys())
        remote_order_ids = set(remote_map.keys())
        
        # 移除已不存在的订单
        for order_id in local_order_ids - remote_order_ids:
            # DLE Pro钩子：通知订单关闭
            if hasattr(self, 'dle_pro_instance') and self.dle_pro_instance:
                local_order = self.local_orders.get(order_id)
                if local_order:
                    self.dle_pro_instance.on_order_closed(local_order.price)
            
            self.remove_local_order(order_id)
            sync_result['changes'] += 1
            
        # 更新或添加订单
        for order_id, order_data in remote_map.items():
            if order_id in self.local_orders:
                # 更新现有订单
                local_order = self.local_orders[order_id]
                remote_status = order_data['status']
                remote_filled = float(order_data['executedQty'])
                
                if (local_order.status != remote_status or 
                    local_order.filled_qty != remote_filled):
                    
                    # DLE Pro钩子：检测订单从活跃到关闭的状态变化
                    old_active = local_order.is_active
                    new_active = remote_status in ['NEW', 'PARTIALLY_FILLED']
                    
                    self.update_local_order(
                        order_id,
                        status=remote_status,
                        filled_qty=remote_filled
                    )
                    sync_result['changes'] += 1
                    
                    # Phase 4: 如果从活跃变为关闭，通知DLE Pro价位计数回落
                    if old_active and not new_active:
                        await self._handle_order_closed(order_data)
            else:
                # 添加新订单（可能是外部创建的）
                self.add_local_order(
                    order_id,
                    order_data.get('clientOrderId', ''),
                    order_data['side'],
                    float(order_data['price']),
                    float(order_data['origQty'])
                )
                sync_result['changes'] += 1
                
        return sync_result
        
    async def _incremental_sync(self, remote_orders: List[Dict]) -> Dict:
        """增量同步"""
        sync_result = {'type': 'incremental', 'changes': 0, 'errors': []}
        
        # 简化增量同步：只更新状态变化
        remote_map = {order['orderId']: order for order in remote_orders}
        
        for order_id, local_order in self.local_orders.items():
            if order_id in remote_map:
                remote_order = remote_map[order_id]
                remote_status = remote_order['status']
                remote_filled = float(remote_order['executedQty'])
                
                if (local_order.status != remote_status or 
                    abs(local_order.filled_qty - remote_filled) > 1e-8):
                    self.update_local_order(
                        order_id,
                        status=remote_status,
                        filled_qty=remote_filled
                    )
                    sync_result['changes'] += 1
                    
        return sync_result
        
    async def auto_sync_loop(self):
        """自动同步循环"""
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                await self.differential_sync()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OrderMirror] 自动同步异常: {e}")
                await asyncio.sleep(min(self.sync_interval, 60))
                
    def get_status(self) -> Dict:
        """获取状态"""
        active_orders = self.get_active_orders()
        
        return {
            'total_orders': len(self.local_orders),
            'active_orders': len(active_orders),
            'last_sync_time': self.last_sync_time,
            'time_since_sync': time.time() - self.last_sync_time,
            'last_sync_hash': self.last_sync_hash,
            'sync_in_progress': self.sync_in_progress,
            'stats': self.stats.copy()
        }
        
    def get_summary(self) -> str:
        """获取摘要"""
        status = self.get_status()
        return (
            f"mirror(orders={status['active_orders']}/{status['total_orders']} "
            f"sync={status['time_since_sync']:.0f}s "
            f"saved={self.stats['rest_calls_saved']})"
        )
        
    def cleanup_old_orders(self, max_age_hours: int = 24):
        """清理旧订单"""
        cutoff_time = time.time() - max_age_hours * 3600
        old_orders = []
        
        for order_id, order in self.local_orders.items():
            if not order.is_active and order.update_time < cutoff_time:
                old_orders.append(order_id)
                
        for order_id in old_orders:
            self.remove_local_order(order_id)
            
        if old_orders:
            logger.info(f"[OrderMirror] 清理 {len(old_orders)} 个旧订单")

    async def _handle_order_closed(self, order_data: Dict):
        """处理订单关闭事件"""
        order_id = order_data.get('orderId')
        price = float(order_data.get('price', 0))
        status = order_data.get('status')
        
        # Phase 6 P0-5: Mirror关闭事件状态白名单
        CLOSE_STATES = {'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED'}
        if status not in CLOSE_STATES:
            logger.debug(f"[Mirror] 跳过非关闭状态: {order_id} status={status}")
            return
        
        logger.info(f"[Mirror] 订单关闭: {order_id} price={price} status={status}")
        
        # 通知DLE Pro订单关闭（传递order_id和price）
        if self.dle_pro_callback and order_id:
            try:
                # 如果是异步回调，则await
                import inspect
                if inspect.iscoroutinefunction(self.dle_pro_callback):
                    await self.dle_pro_callback(order_id, price)
                else:
                    self.dle_pro_callback(order_id, price)
                logger.debug(f"[Mirror] 已通知DLE Pro订单关闭: {order_id}, price={price}")
            except Exception as e:
                logger.error(f"[Mirror] DLE回调异常: {e}")

    def set_dle_pro_instance(self, dle_pro_instance):
        """设置DLE Pro实例引用，用于订单关闭钩子"""
        self.dle_pro_instance = dle_pro_instance


# 全局实例
_global_order_mirror = None


def get_order_mirror(exchange, **kwargs):
    """获取Order Mirror Pro实例（单例）"""
    global _global_order_mirror
    
    if _global_order_mirror is None:
        persist_path = kwargs.get('persist_path', '/tmp/order_mirror.json')
        sync_interval = kwargs.get('sync_interval', 60)
        
        _global_order_mirror = OrderMirrorPro(
            exchange=exchange,
            persist_path=persist_path,
            sync_interval=sync_interval
        )
        logger.info(f"[OrderMirror] 创建新实例: {persist_path}")
    
    return _global_order_mirror


# 全局实例管理
_global_order_mirror = None

def reset_order_mirror():
    """重置Order Mirror Pro实例（测试用）"""
    global _global_order_mirror
    _global_order_mirror = None


if __name__ == "__main__":
    # 简单测试
    class MockExchange:
        async def get_open_orders(self):
            return [
                {
                    'orderId': '12345',
                    'clientOrderId': 'test1',
                    'side': 'BUY',
                    'price': '0.24000',
                    'origQty': '100.0',
                    'executedQty': '0.0',
                    'status': 'NEW'
                }
            ]
    
    async def test():
        mirror = OrderMirrorPro(MockExchange())
        
        # 添加本地订单
        mirror.add_local_order('12345', 'test1', 'BUY', 0.24, 100)
        
        # 执行同步
        result = await mirror.differential_sync()
        print(f"同步结果: {result}")
        
        # 获取状态
        status = mirror.get_status()
        print(f"状态: {status}")
        
        # 测试工厂函数
        mirror2 = get_order_mirror(MockExchange())
        print(f"工厂函数测试: {mirror2.get_summary()}")
        
    asyncio.run(test())