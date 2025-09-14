#!/usr/bin/env python3
"""
User Data Stream Service - Phase 1
实时获取订单和余额事件，解决观测层断层问题
"""
import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any
import aiohttp

logger = logging.getLogger(__name__)

class AsyncSingleFlight:
    """防止并发重复请求的工具"""
    def __init__(self):
        self._inflight = {}
    
    async def do(self, key: str, fn):
        """确保同一key只有一个请求在飞行中"""
        if key in self._inflight:
            # 等待已有请求完成
            return await self._inflight[key]
        
        # 创建新的Future
        future = asyncio.create_future()
        self._inflight[key] = future
        
        try:
            result = await fn()
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            del self._inflight[key]


class UserDataStreamService:
    """User Data Stream服务 - 通过WebSocket接收实时订单/余额事件"""
    
    def __init__(self, connector, awg, order_mirror, dle, shadow, log=None):
        self.connector = connector     # REST连接器
        self.awg = awg                 # AWG Pro（授权/熔断）
        self.order_mirror = order_mirror
        self.dle = dle                 # DLE Pro（含live_orders）
        self.shadow = shadow           # Shadow Balance
        self.log = log or logger
        
        # WebSocket相关
        self.listen_key = None
        self.ws = None
        self.keepalive_task = None
        self.recv_task = None
        self.reconnect_lock = asyncio.Lock()
        self.singleflight_openorders = AsyncSingleFlight()
        
        # 统计
        self.stats = {
            'connected': False,
            'last_msg_time': 0,
            'reconnect_count': 0,
            'events_received': 0,
            'orders_updated': 0,
            'balances_updated': 0
        }
        
        # 配置
        self.keepalive_interval = 25 * 60  # 25分钟
        self.max_reconnects = 10
        self.reconnect_delay = 5
    
    async def start(self):
        """启动User Data Stream"""
        try:
            # 1. 创建listenKey
            await self._ensure_listen_key()
            
            # 2. 连接WebSocket
            await self._connect_ws()
            
            # 3. 启动keepalive任务
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            # 4. 初次种子同步（SingleFlight保护）
            await self._seed_state_from_openorders_once()
            
            self.log.info("✅ User Data Stream started successfully")
            
        except Exception as e:
            self.log.error(f"❌ Failed to start User Data Stream: {e}")
            raise
    
    async def stop(self):
        """停止User Data Stream"""
        try:
            # 1. 取消keepalive
            if self.keepalive_task:
                self.keepalive_task.cancel()
            
            # 2. 关闭WebSocket
            if self.ws:
                await self.ws.close()
            
            # 3. 删除listenKey
            if self.listen_key:
                await self._close_listen_key()
            
            self.log.info("✅ User Data Stream stopped")
            
        except Exception as e:
            self.log.error(f"❌ Error stopping User Data Stream: {e}")
    
    async def _ensure_listen_key(self):
        """确保有有效的listenKey"""
        try:
            # 通过connector创建listenKey（走AWG）
            self.listen_key = await self.connector.create_listen_key()
            self.log.info(f"✅ Created listenKey: {self.listen_key[:8]}...")
            
        except Exception as e:
            self.log.error(f"❌ Failed to create listenKey: {e}")
            raise
    
    async def _connect_ws(self):
        """连接WebSocket"""
        if not self.listen_key:
            raise ValueError("No listenKey available")
        
        ws_url = f"wss://stream.binance.com:9443/ws/{self.listen_key}"
        
        try:
            session = aiohttp.ClientSession()
            self.ws = await session.ws_connect(ws_url)
            
            # 启动接收任务
            self.recv_task = asyncio.create_task(self._recv_loop())
            
            self.stats['connected'] = True
            self.log.info("✅ WebSocket connected")
            
        except Exception as e:
            self.log.error(f"❌ WebSocket connection failed: {e}")
            raise
    
    async def _recv_loop(self):
        """接收WebSocket消息循环"""
        while self.ws and not self.ws.closed:
            try:
                msg = await self.ws.receive()
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._handle_event(data)
                    
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self.log.warning(f"WebSocket closed/error: {msg}")
                    await self._reconnect()
                    break
                    
            except Exception as e:
                self.log.error(f"Error in recv_loop: {e}")
                await asyncio.sleep(1)
    
    async def _handle_event(self, data):
        """处理WebSocket事件"""
        event_type = data.get('e')
        
        # 更新统计
        self.stats['last_msg_time'] = time.time()
        self.stats['events_received'] += 1
        
        try:
            if event_type == 'executionReport':
                # 订单事件
                await self._handle_execution_report(data)
                self.stats['orders_updated'] += 1
                
            elif event_type == 'outboundAccountPosition':
                # 账户余额快照
                await self._handle_account_position(data)
                self.stats['balances_updated'] += 1
                
            elif event_type == 'balanceUpdate':
                # 余额变化
                await self._handle_balance_update(data)
                self.stats['balances_updated'] += 1
                
            elif event_type == 'listStatus':
                # OCO订单（暂不处理）
                self.log.debug(f"Received listStatus: {data}")
                
        except Exception as e:
            self.log.error(f"Error handling event {event_type}: {e}")
    
    async def _handle_execution_report(self, er):
        """处理订单执行报告"""
        order_id = er.get('i')  # orderId
        status = er.get('X')     # orderStatus
        side = er.get('S')       # side
        price = float(er.get('p', 0))  # price
        orig_qty = float(er.get('q', 0))  # origQty
        filled_qty = float(er.get('z', 0))  # cumulativeFilledQty
        
        if not order_id:
            return
        
        self.log.debug(f"[UDS] Order {order_id} status={status} side={side} price={price}")
        
        if status == 'NEW':
            # 新订单：登记到live_orders
            if self.dle and hasattr(self.dle, 'live_orders'):
                self.dle.live_orders[order_id] = {
                    'side': side,
                    'price': price,
                    'orig_qty': orig_qty,
                    'filled_qty': 0,
                    'remain_qty': orig_qty,
                    'timestamp': er.get('T', time.time() * 1000)
                }
            
            # 更新Mirror
            if self.order_mirror:
                await self.order_mirror.upsert_from_event(order_id, er)
            
        elif status == 'PARTIALLY_FILLED':
            # 部分成交：更新剩余量
            remain_qty = orig_qty - filled_qty
            
            if self.dle and hasattr(self.dle, 'live_orders'):
                if order_id in self.dle.live_orders:
                    self.dle.live_orders[order_id]['filled_qty'] = filled_qty
                    self.dle.live_orders[order_id]['remain_qty'] = remain_qty
            
        elif status in ['FILLED', 'CANCELED', 'EXPIRED']:
            # 订单结束：释放资源
            if self.dle and hasattr(self.dle, '_close_and_release'):
                await self.dle._close_and_release(order_id)
            
            # 更新Mirror
            if self.order_mirror:
                await self.order_mirror.close_from_event(order_id)
            
        elif status == 'REJECTED':
            # 订单被拒绝
            if self.dle and hasattr(self.dle, 'live_orders'):
                self.dle.live_orders.pop(order_id, None)
            
            self.log.warning(f"[UDS] Order {order_id} rejected: {er.get('r')}")
    
    async def _handle_account_position(self, data):
        """处理账户余额快照"""
        balances = data.get('B', [])
        
        for balance in balances:
            asset = balance.get('a')  # asset
            free = float(balance.get('f', 0))  # free
            locked = float(balance.get('l', 0))  # locked
            
            if asset in ['USDT', 'DOGE']:
                if self.shadow and hasattr(self.shadow, 'sync_actual_balance'):
                    # Phase 6 Bug Fix: sync_actual_balance expects dict, not individual params
                    self.shadow.sync_actual_balance({asset: {'free': free, 'locked': locked}})
                
                self.log.debug(f"[UDS] Balance update: {asset} free={free} locked={locked}")
    
    async def _handle_balance_update(self, data):
        """处理余额变化事件"""
        asset = data.get('a')  # asset
        delta = float(data.get('d', 0))  # delta
        
        if asset in ['USDT', 'DOGE']:
            self.log.debug(f"[UDS] Balance delta: {asset} {delta:+.4f}")
    
    async def _keepalive_loop(self):
        """Keepalive循环，每25分钟续期一次"""
        while True:
            try:
                await asyncio.sleep(self.keepalive_interval)
                
                if self.listen_key:
                    success = await self.connector.keepalive_listen_key(self.listen_key)
                    
                    if success:
                        self.log.debug(f"✅ Keepalive success")
                    else:
                        self.log.warning("❌ Keepalive failed, recreating...")
                        await self._reconnect()
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"Keepalive error: {e}")
                await asyncio.sleep(60)
    
    async def _reconnect(self):
        """重连逻辑"""
        async with self.reconnect_lock:
            if self.stats['reconnect_count'] >= self.max_reconnects:
                self.log.error("❌ Max reconnects reached, giving up")
                return
            
            self.stats['connected'] = False
            self.stats['reconnect_count'] += 1
            
            # 指数退避
            delay = min(self.reconnect_delay * (2 ** self.stats['reconnect_count']), 300)
            self.log.info(f"Reconnecting in {delay}s... (attempt {self.stats['reconnect_count']})")
            await asyncio.sleep(delay)
            
            try:
                # 1. 关闭旧连接
                if self.ws:
                    await self.ws.close()
                
                # 2. 重新创建listenKey
                await self._ensure_listen_key()
                
                # 3. 重新连接
                await self._connect_ws()
                
                # 4. 种子同步一次
                await self._seed_state_from_openorders_once()
                
                self.log.info("✅ Reconnected successfully")
                self.stats['reconnect_count'] = 0
                
            except Exception as e:
                self.log.error(f"Reconnect failed: {e}")
    
    async def _seed_state_from_openorders_once(self):
        """从openOrders种子同步一次（冷启动/重连后）"""
        try:
            # SingleFlight保证只打一枪
            orders = await self.singleflight_openorders.do(
                key="seed",
                fn=self._fetch_openorders_with_protection
            )
            
            if not orders:
                self.log.info("No open orders to seed")
                return
            
            # 种回live_orders和Mirror
            for order in orders:
                order_id = order['orderId']
                
                if self.dle and hasattr(self.dle, 'live_orders'):
                    self.dle.live_orders[order_id] = {
                        'side': order['side'],
                        'price': float(order['price']),
                        'orig_qty': float(order['origQty']),
                        'filled_qty': float(order.get('executedQty', 0)),
                        'remain_qty': float(order['origQty']) - float(order.get('executedQty', 0)),
                        'timestamp': order.get('time', time.time() * 1000)
                    }
                
                if self.order_mirror:
                    await self.order_mirror.upsert_from_rest(order_id, order)
            
            self.log.info(f"✅ Seeded {len(orders)} orders from snapshot")
            
        except Exception as e:
            self.log.error(f"Seed failed: {e}")
            # 不影响运行，等待UDS事件增量更新
    
    async def _fetch_openorders_with_protection(self):
        """带保护的openOrders获取（AWG+最小间隔）"""
        if self.connector and hasattr(self.connector, 'get_open_orders'):
            return await self.connector.get_open_orders('DOGEUSDT')
        return []
    
    async def _close_listen_key(self):
        """关闭listenKey"""
        try:
            if self.connector and hasattr(self.connector, 'close_listen_key'):
                await self.connector.close_listen_key(self.listen_key)
                self.log.debug("Closed listenKey")
        except Exception as e:
            self.log.error(f"Error closing listenKey: {e}")
    
    def get_stats(self):
        """获取统计信息"""
        msg_age = time.time() - self.stats['last_msg_time'] if self.stats['last_msg_time'] else 999
        
        return {
            'connected': self.stats['connected'],
            'last_msg_age': msg_age,
            'reconnects': self.stats['reconnect_count'],
            'events': self.stats['events_received'],
            'orders': self.stats['orders_updated'],
            'balances': self.stats['balances_updated']
        }