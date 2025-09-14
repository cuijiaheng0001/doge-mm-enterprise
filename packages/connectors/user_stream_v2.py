#!/usr/bin/env python3
"""
User Data Stream Service - Phase 2 机构级方案
双WebSocket架构：主WS落地 + 副WS核对
"""
import asyncio
import json
import time
import logging
import aiohttp
from typing import Dict, Any, Optional, Callable
from collections import defaultdict

# Phase 5 Fix: EventNormalizer导入移至模块顶部
from doge_mm.packages.connectors.event_normalizer import EventNormalizer

logger = logging.getLogger(__name__)


class AsyncSingleFlight:
    """防止并发重复请求的工具"""
    def __init__(self):
        self._locks: Dict[str, asyncio.Future] = {}

    async def do(self, key: str, coro_factory: Callable):
        """确保同一key只有一个请求在飞行中"""
        fut = self._locks.get(key)
        if fut: 
            return await fut
            
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._locks[key] = fut
        
        try:
            res = await coro_factory()
            fut.set_result(res)
            return res
        except Exception as e:
            fut.set_exception(e)
            raise
        finally:
            self._locks.pop(key, None)


class UserDataStreamService:
    """User Data Stream Service - 机构级双WS架构"""
    
    def __init__(self, connector, awg, order_mirror, dle, shadow, logger=None,
                 keepalive_sec=1800, reconnect_base_ms=500, reconnect_max_ms=8000,
                 audit_seed_suppress_sec=90):
        # 核心组件
        self.cx = connector         # REST连接器
        self.awg = awg             # AWG Pro
        self.mirror = order_mirror  # OrderMirror
        self.dle = dle             # DLE Pro
        self.shadow = shadow       # ShadowBalance
        self.log = logger or logging.getLogger(__name__)

        # WebSocket相关
        self.listen_key: Optional[str] = None
        self.ws_main = None         # 主WS（落地）
        self.ws_audit = None        # 副WS（核对）
        self.ws_session = None      # aiohttp session
        
        # 任务管理
        self.keepalive_task = None
        self.recv_main_task = None
        self.recv_audit_task = None
        self.reconnect_lock = asyncio.Lock()
        self.sf = AsyncSingleFlight()
        
        # Phase 4 Patch B 状态
        self._connected = False
        self._task = None
        
        # 配置参数
        self.keepalive_sec = keepalive_sec
        self.reconnect_base_ms = reconnect_base_ms
        self.reconnect_max_ms = reconnect_max_ms
        self.audit_seed_suppress_sec = audit_seed_suppress_sec

        # 观测指标
        self.last_msg_ts_main = 0.0
        self.last_msg_ts_audit = 0.0
        self.main_hash = 0
        self.audit_hash = 0
        self.audit_diverged_at = 0.0
        self.seed_suppress_until = 0.0
        self.reconnect_count = 0
        
        # Phase 6: UDS健康守护
        self.uds_last_event_ts = 0.0  # 最后收到executionReport的时间
        self.uds_event_count = 0      # executionReport计数
        
        # 统计
        self.stats = defaultdict(int)
        
        # 幂等键缓存（防止重复处理）
        self._processed_events = {}  # (orderId, eventTime) -> True
        self._event_cache_ttl = 300  # 5分钟过期

    async def start(self):
        """启动User Data Stream服务"""
        try:
            self.log.info("[UDS] 🚀 Starting Phase 2 User Data Stream Service...")
            
            # 1. 确保有listenKey
            self.log.info("[UDS] Step 1: Creating listenKey...")
            await self._ensure_listen_key()
            self.log.info(f"[UDS] ✅ listenKey ready: {self.listen_key[:8] if self.listen_key else 'None'}...")
            
            # 2. 建立双WS连接
            self.log.info("[UDS] Step 2: Connecting dual WebSocket...")
            await self._connect_ws_pair()
            self.log.info("[UDS] ✅ Dual WebSocket connected")
            
            # 3. 启动keepalive任务
            self.log.info("[UDS] Step 3: Starting keepalive task...")
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            self.log.info("[UDS] ✅ Keepalive task started")
            
            # 4. 初始种子同步
            self.log.info("[UDS] Step 4: Initial seed sync...")
            await self._seed_once("startup")
            self.log.info("[UDS] ✅ Initial seed sync completed")
            
            self.log.info("[UDS] 🎉 Phase 2 UDS Service fully operational!")
            
        except Exception as e:
            self.log.error(f"[UDS] ❌ Failed to start: {e}")
            raise

    async def start_background(self):
        """启动后台运行循环 - Phase 4 Patch B"""
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run_loop(), name="uds-runloop")
        self.log.info("🔌 [UDS] runloop started (background)")

    async def wait_connected(self, timeout: float = 5.0) -> bool:
        """等待连接建立 - Phase 4 Patch B"""
        t0 = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t0 < timeout:
            if self._connected:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _run_loop(self):
        """持续连接循环 - Phase 4 Patch B"""
        WS_BASE = "wss://stream.binance.com:9443/ws"
        REST_CREATE = "/api/v3/userDataStream"
        if getattr(self, 'is_futures', False):
            WS_BASE = "wss://fstream.binance.com/ws"
            REST_CREATE = "/fapi/v1/listenKey"
            
        while True:
            try:
                self.log.info("🔑 [UDS-V2] creating listenKey (%s)", "futures" if getattr(self, 'is_futures', False) else "spot")
                lk = await self.cx.create_listen_key()
                self.listen_key = lk
                url = f"{WS_BASE}/{lk}"
                self.log.info("🌐 [UDS-V2] connecting %s", url)
                
                # 简化连接：直接调用现有方法
                await self._connect_ws_pair()
                self._connected = True
                self.log.info("✅ [UDS-V2] both WS connected (reconn=%d)", self.reconnect_count)
                
                # 启动keepalive
                if not self.keepalive_task or self.keepalive_task.done():
                    self.keepalive_task = asyncio.create_task(self._keepalive_loop())
                
                # 等待连接断开
                while self._connected:
                    await asyncio.sleep(1)
                    # 检查连接状态
                    if not (self.ws_main and self.ws_audit):
                        self._connected = False
                        break
                        
            except Exception as e:
                self._connected = False
                self.reconnect_count += 1
                self.log.warning("⚠️ [UDS] disconnected (%s), will reconnect in %ss", e, 5)
                await asyncio.sleep(5)

    def snapshot(self):
        """状态快照 - Phase 4 Patch B"""
        return {
            "connected": self._connected,
            "reconnects": self.reconnect_count,
            "last_msg_age": 0 if self.last_msg_ts_main == 0 else max(0, time.time() - self.last_msg_ts_main),
            "listen_key": bool(self.listen_key),
        }

    async def stop(self):
        """停止User Data Stream服务"""
        try:
            self.log.info("[UDS] Stopping service...")
            
            # 取消所有任务
            for task in [self.recv_main_task, self.recv_audit_task, self.keepalive_task]:
                if task:
                    task.cancel()
                    
            # 关闭WebSocket连接
            await self._close_ws_pair()
            
            # 关闭listenKey
            await self._close_listen_key()
            
            # 关闭session
            if self.ws_session:
                await self.ws_session.close()
                
            self.log.info("[UDS] ✅ Service stopped")
            
        except Exception as e:
            self.log.error(f"[UDS] Error during stop: {e}")

    # ---------- listenKey 管理 ----------
    
    async def _ensure_listen_key(self):
        """确保有有效的listenKey"""
        if self.listen_key:
            return
            
        # AWG授权
        if self.awg and not self.awg.acquire('userDataStream.create', cost=1):
            raise RuntimeError("[UDS] AWG denied userDataStream.create")
            
        # 创建listenKey
        self.listen_key = await self.cx.create_listen_key()
        self.log.info(f"[UDS] listenKey created: {self.listen_key[:8]}...")

    async def _keepalive_loop(self):
        """Keepalive循环，定期续期listenKey"""
        while True:
            try:
                # 提前0.5周期续期，更稳妥
                await asyncio.sleep(self.keepalive_sec * 0.5)
                
                if not self.listen_key:
                    continue
                    
                # AWG授权
                if self.awg and not self.awg.acquire('userDataStream.keepalive', cost=1):
                    self.log.warning("[UDS] Keepalive denied by AWG")
                    continue
                    
                # 续期
                ok = await self.cx.keepalive_listen_key(self.listen_key)
                
                if ok:
                    self.log.debug("[UDS] Keepalive success")
                    self.stats['keepalive_success'] += 1
                else:
                    self.log.warning("[UDS] Keepalive failed, recreating...")
                    self.stats['keepalive_fail'] += 1
                    await self._recreate_listen_key_and_reconnect()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"[UDS] Keepalive error: {e}")
                await asyncio.sleep(60)

    async def _recreate_listen_key_and_reconnect(self):
        """重新创建listenKey并重连"""
        async with self.reconnect_lock:
            self.log.info("[UDS] Recreating listenKey and reconnecting...")
            
            # 1. 关闭现有连接
            await self._close_ws_pair()
            
            # 2. 关闭旧listenKey
            await self._close_listen_key()
            
            # 3. 创建新listenKey
            await self._ensure_listen_key()
            
            # 4. 重新连接
            await self._connect_ws_pair()
            
            # 5. 种子同步
            await self._seed_once("recreate_listen_key")

    async def _close_listen_key(self):
        """关闭listenKey"""
        if self.listen_key:
            try:
                if self.awg and self.awg.acquire('userDataStream.close', cost=1):
                    await self.cx.close_listen_key(self.listen_key)
                    self.log.debug("[UDS] listenKey closed")
            finally:
                self.listen_key = None

    # ---------- WebSocket 连接管理 ----------
    
    async def _connect_ws_pair(self):
        """建立双WebSocket连接"""
        if not self.listen_key:
            raise ValueError("No listenKey available")
            
        url = f"wss://stream.binance.com:9443/ws/{self.listen_key}"
        
        try:
            # 使用connector的open_ws方法连接主WS
            self.ws_main = await self.cx.open_ws(
                url, 
                handler_callback=lambda data: self._handle_ws_message(data, mode="main"),
                error_callback=lambda e: self._handle_ws_error(e, mode="main")
            )
            self.log.info("[UDS] Main WS connected")
            
            # 使用connector的open_ws方法连接副WS
            self.ws_audit = await self.cx.open_ws(
                url,
                handler_callback=lambda data: self._handle_ws_message(data, mode="audit"),
                error_callback=lambda e: self._handle_ws_error(e, mode="audit")
            )
            self.log.info("[UDS] Audit WS connected")
            
            self.stats['ws_connect'] += 1
            
        except Exception as e:
            self.log.error(f"[UDS] Failed to connect WS pair: {e}")
            raise
    
    async def _handle_ws_message(self, data: Dict, mode: str):
        """处理WebSocket消息的回调"""
        await self._handle_uds_event(data, mode)
    
    async def _handle_ws_error(self, error: Exception, mode: str):
        """处理WebSocket错误的回调"""
        self.log.error(f"[UDS] {mode} WS error: {error}")
        self.stats['ws_errors'] += 1
        
        # 触发重连  
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._reconnect_loop())

    async def _close_ws_pair(self):
        """关闭双WebSocket连接"""
        for ws in [self.ws_main, self.ws_audit]:
            if ws:
                try:
                    await ws.close()
                except:
                    pass
        self.ws_main = self.ws_audit = None

    async def _recv_loop(self, ws, mode="main"):
        """接收WebSocket消息循环"""
        backoff = self.reconnect_base_ms
        
        while True:
            try:
                msg = await ws.receive()
                
                # 处理不同消息类型
                if msg.type == aiohttp.WSMsgType.TEXT:
                    now = time.time()
                    
                    # 更新最后消息时间
                    if mode == "main":
                        self.last_msg_ts_main = now
                    else:
                        self.last_msg_ts_audit = now
                    
                    # 解析并处理事件
                    data = json.loads(msg.data)
                    await self._handle_uds_event(data, mode=mode)
                    
                    # 成功收包，重置退避
                    backoff = self.reconnect_base_ms
                    
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self.log.warning(f"[UDS] {mode} WS closed/error: {msg}")
                    await self._reconnect_ws(mode)
                    break
                    
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.log.error(f"[UDS] {mode} recv loop error: {e}")
                await asyncio.sleep(backoff / 1000)
                backoff = min(backoff * 2, self.reconnect_max_ms)
                await self._reconnect_ws(mode)

    async def _reconnect_ws(self, mode):
        """重连单个WebSocket"""
        async with self.reconnect_lock:
            try:
                self.reconnect_count += 1
                self.log.info(f"[UDS] Reconnecting {mode} WS (attempt {self.reconnect_count})...")
                
                url = f"wss://stream.binance.com:9443/ws/{self.listen_key}"
                
                if not self.ws_session:
                    self.ws_session = aiohttp.ClientSession()
                    
                ws = await self.ws_session.ws_connect(url)
                
                if mode == "main":
                    self.ws_main = ws
                else:
                    self.ws_audit = ws
                    
                self.log.info(f"[UDS] {mode} WS reconnected")
                self.reconnect_count = 0
                
            except Exception as e:
                self.log.error(f"[UDS] {mode} reconnect failed: {e}")

    # ---------- 事件处理 ----------
    
    def _bump_hash(self, h: int, s: Any) -> int:
        """轻量级hash计算，用于主副对比"""
        return (h * 1315423911 ^ hash(str(s))) & 0xFFFFFFFF

    async def _handle_uds_event(self, ev: Dict[str, Any], mode: str):
        """处理User Data Stream事件"""
        etype = ev.get('e')
        self.stats[f'{mode}_events'] += 1
        
        if etype == "executionReport":
            await self._handle_execution_report(ev, mode)
            
        elif etype in ("outboundAccountPosition", "balanceUpdate"):
            await self._handle_balance_event(ev, mode)
            
        elif etype == "listStatus":
            # OCO订单事件，暂不处理
            self.log.debug(f"[UDS] {mode} listStatus event")
            
        else:
            self.log.debug(f"[UDS] {mode} unknown event type: {etype}")

    async def _handle_execution_report(self, ev: Dict, mode: str):
        """处理订单执行报告"""
        order_id = str(ev.get('i', ''))
        status = ev.get('X', '')
        event_time = ev.get('E', 0)
        trade_id = ev.get('t', -1)
        
        # 计算事件hash
        event_hash = (order_id, status, event_time, trade_id)
        
        if mode == "main":
            # 主WS：落地处理
            await self._apply_execution_report_main(ev)
            self.main_hash = self._bump_hash(self.main_hash, event_hash)
        else:
            # 副WS：只做校验
            self.audit_hash = self._bump_hash(self.audit_hash, event_hash)
            await self._audit_check()

    async def _apply_execution_report_main(self, ev: Dict):
        """主WS处理executionReport（落地）"""
        order_id = str(ev.get('i', ''))
        status = ev.get('X', '')
        side = ev.get('S', '')
        
        # Phase 6: UDS健康守护 - 更新事件时间戳和计数
        self.uds_last_event_ts = time.time()
        self.uds_event_count += 1
        
        # Phase 6: 证据打点 - 记录所有executionReport事件
        timestamp = ev.get('E', time.time() * 1000)
        self.log.info(f"[OBS][UDS] event=executionReport side={side} id={order_id} status={status} ts={timestamp}")
        
        # 幂等检查
        event_key = (order_id, ev.get('E', 0), ev.get('t', -1))
        if event_key in self._processed_events:
            self.log.debug(f"[UDS] Duplicate event ignored: {event_key}")
            return
        self._processed_events[event_key] = True
        
        # 清理过期幂等键
        self._cleanup_event_cache()
        
        try:
            price = float(ev.get('p', 0))
            orig_qty = float(ev.get('q', 0))
            filled_qty = float(ev.get('z', 0))
            cid = ev.get('c', '')
        except:
            price = orig_qty = filled_qty = 0.0
            cid = ''
            
        remain_qty = max(orig_qty - filled_qty, 0.0)
        
        self.log.debug(f"[UDS] Main executionReport: {order_id} {status} {side} "
                      f"price={price} orig={orig_qty} filled={filled_qty}")
        
        if status == "NEW":
            # 新订单：落地到各组件
            if self.mirror:
                await self.mirror.upsert_from_event(order_id, ev)
            
            if self.dle and hasattr(self.dle, 'register_order_from_uds'):
                await self.dle.register_order_from_uds(order_id, side, price, orig_qty)
            elif self.dle and hasattr(self.dle, 'live_orders'):
                # 直接操作live_orders
                self.dle.live_orders[order_id] = {
                    'cid': cid,
                    'side': side,
                    'price': price,
                    'orig_qty': orig_qty,
                    'filled_qty': 0.0,
                    'remain_qty': orig_qty,
                    'timestamp': time.time() * 1000
                }
            
            self.stats['orders_new'] += 1
            
        elif status == "PARTIALLY_FILLED":
            # 部分成交：更新数量
            if self.mirror:
                await self.mirror.upsert_from_event(order_id, ev)
            
            if self.dle and hasattr(self.dle, 'update_filled_from_uds'):
                await self.dle.update_filled_from_uds(order_id, filled_qty)
            elif self.dle and hasattr(self.dle, 'live_orders'):
                if order_id in self.dle.live_orders:
                    self.dle.live_orders[order_id]['filled_qty'] = filled_qty
                    self.dle.live_orders[order_id]['remain_qty'] = remain_qty
            
            self.stats['orders_partial'] += 1
            
        elif status in ("FILLED", "CANCELED", "EXPIRED"):
            # 订单终态：统一释放流程
            if self.mirror:
                await self.mirror.close_from_event(order_id)
            
            # 先取消TTL，避免重复撤单
            if self.dle and hasattr(self.dle, 'cancel_ttl'):
                await self.dle.cancel_ttl(order_id)
            
            # 释放资源
            if self.dle and hasattr(self.dle, '_close_and_release'):
                await self.dle._close_and_release(order_id)
            
            self.stats[f'orders_{status.lower()}'] += 1
            
        elif status == "REJECTED":
            # 订单被拒绝
            if self.mirror:
                await self.mirror.close_from_event(order_id)
            
            if self.dle and hasattr(self.dle, '_close_and_release'):
                await self.dle._close_and_release(order_id)
            
            self.log.warning(f"[UDS] Order {order_id} rejected: {ev.get('r', 'unknown')}")
            self.stats['orders_rejected'] += 1
        
        # 🚀 Phase 5: Shadow Balance 2.0 executionReport即时更新（机构级方案）
        # 使用EventNormalizer统一格式，消除PARTIAL_FILL状态不兼容问题
        if self.shadow and hasattr(self.shadow, 'on_execution_report'):
            try:
                # === 使用EventNormalizer统一格式 ===
                
                # 添加调用日志验证Phase 5补丁
                self.log.info(f"[Phase5] Calling EventNormalizer for order {ev.get('i', 'unknown')} status={ev.get('X', '')}")
                
                # 规范化事件（包含PARTIAL_FILL→PARTIALLY_FILLED映射）
                exec_report = EventNormalizer.normalize_execution_report(ev)
                
                # 记录映射结果
                self.log.info(f"[Phase5] EventNormalizer result: {ev.get('X', '')} → {exec_report.status}")
                
                # 转换为Shadow期望的格式（保留多种键名兼容性）
                shadow_event = EventNormalizer.to_shadow_format(exec_report)
                
                # 补充原始事件中的额外字段
                shadow_event.update({
                    'timeInForce': ev.get('f', ''),
                    'transactTime': ev.get('T', 0),
                    # 保留原始字段用于调试
                    '_raw_X': ev.get('X', ''),
                    '_raw_x': ev.get('x', ''),
                    '_raw_l': ev.get('l', 0),
                    '_raw_z': ev.get('z', 0)
                })
                
                # 调用Shadow Balance 2.0的核心方法（基于数值delta驱动）
                # 🎯 修复：直接传递ExecReport对象而非字典
                success = self.shadow.on_execution_report(exec_report)
                if success:
                    self.log.info(
                        "[Shadow2.0] ✅ ExecutionReport processed for order %s status=%s lastQty=%s",
                        exec_report.order_id, exec_report.status, exec_report.last_qty
                    )
                else:
                    self.log.warning(
                        "[Shadow2.0] ⚠️ ExecutionReport skipped for order %s status=%s",
                        exec_report.order_id, exec_report.status
                    )
                    
            except ImportError as e:
                self.log.error(f"[Shadow2.0] EventNormalizer not found, using fallback: {e}")
                # Fallback: 直接传递原始事件
                try:
                    self.shadow.on_execution_report(ev)
                except Exception as fallback_error:
                    self.log.error(f"[Shadow2.0] Fallback also failed: {fallback_error}")
            except Exception as e:
                self.log.error(f"[Shadow2.0] Error in executionReport: {e}")
                # 永不抛异常，降级处理
        else:
            self.log.warning(f"[Shadow2.0] Shadow Balance not available or missing on_execution_report method")

    async def _handle_balance_event(self, ev: Dict, mode: str):
        """处理余额事件"""
        etype = ev.get('e')
        event_time = ev.get('E', 0)
        
        # 计算事件hash
        event_hash = (etype, event_time)
        
        if mode == "main":
            # 主WS：落地处理
            await self._apply_balance_event(ev)
            self.main_hash = self._bump_hash(self.main_hash, event_hash)
        else:
            # 副WS：只做校验
            self.audit_hash = self._bump_hash(self.audit_hash, event_hash)
            await self._audit_check()

    async def _apply_balance_event(self, ev: Dict):
        """主WS处理余额事件（落地）"""
        etype = ev.get('e')
        
        try:
            if etype == "outboundAccountPosition":
                # 账户余额快照
                balances = ev.get('B', [])
                for balance in balances:
                    asset = balance.get('a')
                    free = float(balance.get('f', 0))
                    locked = float(balance.get('l', 0))
                    
                    if asset in ['USDT', 'DOGE']:
                        if self.shadow and hasattr(self.shadow, 'sync_actual_balance'):
                            # Phase 6 Bug Fix: sync_actual_balance expects dict, not individual params
                            self.shadow.sync_actual_balance({asset: {'free': free, 'locked': locked}})
                        
                        self.log.debug(f"[UDS] Balance update: {asset} free={free} locked={locked}")
                        
            elif etype == "balanceUpdate":
                # 余额变化
                asset = ev.get('a')
                delta = float(ev.get('d', 0))
                
                if asset in ['USDT', 'DOGE']:
                    self.log.debug(f"[UDS] Balance delta: {asset} {delta:+.4f}")
                    
        except Exception as e:
            self.log.warning(f"[UDS] Balance event apply failed: {e}")

    # ---------- 审计与自愈 ----------
    
    async def _audit_check(self):
        """副WS审计检查"""
        now = time.time()
        
        # 条件1：WS时延过大
        age_gap = abs(self.last_msg_ts_main - self.last_msg_ts_audit)
        if age_gap > 3.0:
            self.log.warning(f"[UDS] Audit: age gap {age_gap:.1f}s > 3s")
            await self._maybe_seed("audit_age_gap>3s")
            return
        
        # 条件2：Hash持续不一致
        if self.main_hash != self.audit_hash:
            if self.audit_diverged_at == 0.0:
                self.audit_diverged_at = now
            elif now - self.audit_diverged_at > 1.0:
                self.log.warning(f"[UDS] Audit: hash diverged for {now - self.audit_diverged_at:.1f}s")
                await self._maybe_seed("audit_hash_diverged>1s")
        else:
            self.audit_diverged_at = 0.0

    async def _maybe_seed(self, reason: str):
        """条件触发种子同步"""
        now = time.time()
        
        # 抑制重复种子
        if now < self.seed_suppress_until:
            self.log.debug(f"[UDS] Seed suppressed until {self.seed_suppress_until - now:.0f}s")
            return
            
        self.seed_suppress_until = now + self.audit_seed_suppress_sec
        await self._seed_once(reason)

    async def _seed_once(self, reason: str):
        """执行一次种子同步"""
        self.log.warning(f"[UDS] Seeding openOrders once: {reason}")
        
        async def do_seed():
            # AWG授权
            if self.awg and not self.awg.acquire('openOrders', cost=10):
                self.log.warning("[UDS] Seed denied by AWG")
                return False
                
            # 获取当前挂单
            orders = await self.cx.get_open_orders(symbol="DOGEUSDT")
            
            if not orders:
                self.log.info("[UDS] No open orders to seed")
                return True
                
            # 同步到live_orders和Mirror
            for order in orders:
                order_id = str(order['orderId'])
                
                # 更新DLE
                if self.dle and hasattr(self.dle, 'live_orders'):
                    self.dle.live_orders[order_id] = {
                        'side': order['side'],
                        'price': float(order['price']),
                        'orig_qty': float(order['origQty']),
                        'filled_qty': float(order.get('executedQty', 0)),
                        'remain_qty': float(order['origQty']) - float(order.get('executedQty', 0)),
                        'timestamp': order.get('time', time.time() * 1000)
                    }
                
                # 更新Mirror
                if self.mirror:
                    await self.mirror.upsert_from_rest(order_id, order)
            
            self.log.info(f"[UDS] ✅ Seeded {len(orders)} orders from snapshot")
            self.stats['seed_count'] += 1
            return True
            
        try:
            await self.sf.do("openorders_seed", do_seed)
        except Exception as e:
            self.log.error(f"[UDS] Seed error: {e}")

    def _cleanup_event_cache(self):
        """清理过期的幂等键缓存"""
        now = time.time()
        if not hasattr(self, '_last_cleanup'):
            self._last_cleanup = now
            
        # 每60秒清理一次
        if now - self._last_cleanup > 60:
            expired = []
            for key in self._processed_events:
                # 假设eventTime是毫秒时间戳
                event_time = key[1] / 1000 if key[1] > 0 else now
                if now - event_time > self._event_cache_ttl:
                    expired.append(key)
            
            for key in expired:
                del self._processed_events[key]
            
            if expired:
                self.log.debug(f"[UDS] Cleaned {len(expired)} expired event keys")
            
            self._last_cleanup = now

    # ---------- 状态查询 ----------
    
    def get_stats(self) -> Dict:
        """获取服务状态统计"""
        now = time.time()
        
        return {
            'connected': bool(self.ws_main and self.ws_audit),
            'last_msg_age_main': now - self.last_msg_ts_main if self.last_msg_ts_main else 999,
            'last_msg_age_audit': now - self.last_msg_ts_audit if self.last_msg_ts_audit else 999,
            'reconnects': self.reconnect_count,
            'main_events': self.stats.get('main_events', 0),
            'audit_events': self.stats.get('audit_events', 0),
            'orders_new': self.stats.get('orders_new', 0),
            'orders_filled': self.stats.get('orders_filled', 0),
            'orders_canceled': self.stats.get('orders_canceled', 0),
            'seed_count': self.stats.get('seed_count', 0),
            'hash_match': self.main_hash == self.audit_hash
        }
    
    def is_healthy(self) -> bool:
        """检查服务是否健康"""
        stats = self.get_stats()
        
        # 健康条件
        if not stats['connected']:
            return False
        if stats['last_msg_age_main'] > 10:
            return False
        if stats['last_msg_age_audit'] > 10:
            return False
            
        return True
    
    def get_uds_health_info(self) -> Dict:
        """Phase 6: 获取UDS健康信息"""
        now = time.time()
        uds_age = now - self.uds_last_event_ts if self.uds_last_event_ts > 0 else 999
        return {
            'uds_age': uds_age,
            'uds_ok': uds_age < 5,
            'event_count': self.uds_event_count,
            'main_age': now - self.last_msg_ts_main,
            'audit_age': now - self.last_msg_ts_audit,
            'reconn': self.reconnect_count
        }