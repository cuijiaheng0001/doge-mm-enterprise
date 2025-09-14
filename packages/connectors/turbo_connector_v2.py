#!/usr/bin/env python3
"""
TurboConnector V2 with Rate Limiting + Budget Governor Support
包含WeightMonitor和FixedWindowGate双重保护，支持动态预算控制
"""
import asyncio
import aiohttp
import time
import hmac
import hashlib
import json
import logging
import os
from typing import Dict, Optional, List
from urllib.parse import urlencode
from collections import deque, defaultdict

# 导入WeightMonitor
try:
    from weight_monitor import WeightMonitor
except ImportError:
    try:
        from connectors.weight_monitor import WeightMonitor
    except ImportError:
        try:
            from doge_mm.packages.risk.weight_monitor import WeightMonitor
        except ImportError:
            WeightMonitor = None

# 导入固定窗口速率限制器
try:
    from fixed_window_gate import FixedWindowGate
except ImportError:
    try:
        from connectors.fixed_window_gate import FixedWindowGate
    except ImportError:
        FixedWindowGate = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _env_int(default, *names):
    """Phase 6: 从多个可能的环境变量名中读取整数值"""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            try:
                return int(float(v))
            except Exception:
                pass
    return default

def _env_bool(default, *names):
    """Phase 6: 从多个可能的环境变量名中读取布尔值"""
    for n in names:
        v = os.getenv(n)
        if v is not None:
            return str(v).strip() in ("1","true","TRUE","yes","Yes")
    return default

def _int_env(name: str, default: int, aliases: list[str] = None) -> int:
    """兼容旧函数名"""
    return _env_int(default, name, *(aliases or []))

class TurboConnectorV2:
    """增强版TurboConnector - 双重速率保护"""
    
    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.api_secret = api_secret or os.getenv('BINANCE_API_SECRET')
        self.base_url = 'https://api.binance.com'
        self.session = None
        self.connector = None
        
        # Phase 6: 记录启动时间用于暖机
        self.start_ts = time.time()
        
        # 统计
        self.request_count = 0
        self.total_latency = 0
        self.min_latency = float('inf')
        self.max_latency = 0
        
        # Phase 6: 权重监控（兼容多种命名）
        self.weight_threshold = _env_int(800, 'WEIGHT_THRESHOLD', 'WEIGHT_MONITOR_THRESHOLD', 'WM_DANGER_TH')
        self.weight_cooldown_s = _env_int(15, 'WEIGHT_COOLDOWN_SEC', 'WEIGHT_MONITOR_COOLDOWN', 'WM_COOLDOWN_S')
        
        # Phase 6: 降档保底 + 暖机 + 可临时禁用降档
        self.min_safe_fill = _env_int(6, 'MIN_SAFE_FILL')
        self.min_safe_cancel = _env_int(20, 'MIN_SAFE_CANCEL')
        self.warmup_sec = _env_int(120, 'RATE_LIMITER_WARMUP_SEC')
        self.disable_degrade = _env_bool(False, 'RATE_LIMITER_DISABLE_DEGRADE')
        
        # Phase 5: AWG Pro引用（用于418/-1003错误上报）
        self.awg_pro = None
        
        # Phase 9 Fix: 权重打点增强（硬证据统计）
        self._rest_calls = {}
        self._rest_weight = 0
        self._last_weight_emit = time.time()
        self.WEIGHT_TABLE = {
            '/api/v3/order': 4,
            '/api/v3/order/cancelReplace': 4,
            '/api/v3/openOrders': 40,
            '/api/v3/account': 20,
            '/api/v3/exchangeInfo': 10,
            '/api/v3/depth': 5,
            '/api/v3/myTrades': 10,
            '/api/v3/order/test': 1,
        }
        
        # Phase 10: 消息滑窗计数（用于BudgetGovernor CQM）
        self._msg_hist = deque(maxlen=1200)  # 20分钟历史记录，足够分析
        
        # Phase 6 P0-7: 连接器层最小间隔硬闸（高成本端点）
        # Phase 2 A1: 可配置最小间隔，放宽过度限流
        self.endpoint_min_intervals = {
            '/api/v3/openOrders': float(os.getenv('MIN_INTERVAL_OPEN_ORDERS', '2.0')),  # was 30.0
            '/api/v3/account': float(os.getenv('MIN_INTERVAL_ACCOUNT', '6.0')),       # was 15.0
            '/api/v3/exchangeInfo': 60.0,  # 60秒最小间隔
        }
        self.endpoint_last_call = {}  # 记录各端点上次调用时间
        
        # Phase 2 A1: 权重自适应回退
        self.used_weight_1m = 0
        self.weight_backoff_until = 0.0
        self.weight_soft_wall = int(os.getenv('WEIGHT_SOFT_WALL', '3000'))
        self.weight_backoff_sec = float(os.getenv('WEIGHT_BACKOFF_SEC', '8.0'))
        
        # 集成WeightMonitor (管理分钟权重)
        if WeightMonitor:
            self.weight_monitor = WeightMonitor(
                danger_threshold=self.weight_threshold,
                cooldown_seconds=self.weight_cooldown_s
            )
            logger.info(f"✅ WeightMonitor已启用: 阈值={self.weight_monitor.danger_threshold}, 冷却={self.weight_monitor.cooldown_seconds}秒")
        else:
            self.weight_monitor = None
            logger.warning("⚠️ WeightMonitor未启用")
            
        # Phase 4: 配额分账机制
        if FixedWindowGate:
            # Phase 6: 统一只用这一套预算，并兼容老名字
            self.fill_budget_10s = _env_int(24, 'FILL_BUDGET_10S', 'NEW_ORDER_BUDGET_10S')
            self.fill_burst = _env_int(24, 'FILL_BURST', 'NEW_ORDER_BURST')
            self.cancel_budget_10s = _env_int(60, 'CANCEL_BUDGET_10S')
            self.cancel_burst = _env_int(90, 'CANCEL_BURST')
            self.reprice_budget_10s = _env_int(2, 'REPRICE_BUDGET_10S')
            self.reprice_burst = _env_int(3, 'REPRICE_BURST')
            
            # Phase 8: TTL专用撤单配额
            self.ttl_cancel_budget_10s = _env_int(10, 'TTL_CANCEL_BUDGET_10S')
            self.ttl_cancel_burst = _env_int(self.ttl_cancel_budget_10s, 'TTL_CANCEL_BURST')
            
            # 兼容旧变量名
            self.new_order_budget_10s = self.fill_budget_10s
            self.new_order_burst = self.fill_burst

            # DRY_RUN 放宽（干跑/回测不希望被速率卡住）
            if os.getenv("DRY_RUN", "0") in ("1", "true", "True"):
                self.new_order_budget_10s = 9999
                self.new_order_burst = 9999
                self.cancel_budget_10s = 9999
                self.cancel_burst = 9999
                logger.info("🧪 DRY_RUN=1 → 放宽速率限制（仅本进程）")
            
            # Phase 6: 使用上面统一读取的值
            fill_budget = self.fill_budget_10s
            reprice_budget = self.reprice_budget_10s
            fill_burst = self.fill_burst
            reprice_burst = self.reprice_burst
            
            # 创建分离的闸门
            self.fill_gate = FixedWindowGate(
                window_s=10.0,
                budget=fill_budget,
                burst=fill_burst,
                name='fill_orders'
            )
            
            self.reprice_gate = FixedWindowGate(
                window_s=10.0,
                budget=reprice_budget,
                burst=reprice_burst,
                name='reprice_orders'
            )
            
            # 保留原有的new_order_gate用于兼容（实际不再使用）
            self.new_order_gate = FixedWindowGate(
                window_s=10.0,
                budget=self.new_order_budget_10s,
                burst=self.new_order_burst,
                name='new_order'
            )
            self.cancel_gate = FixedWindowGate(
                window_s=10.0,
                budget=self.cancel_budget_10s,
                burst=self.cancel_burst,
                name='cancel'
            )
            
            # Phase 8: TTL专用撤单闸门
            self.ttl_cancel_gate = FixedWindowGate(
                window_s=10.0,
                budget=self.ttl_cancel_budget_10s,
                burst=self.ttl_cancel_burst,
                name='ttl-cancel'
            )
            
            # Phase 6: 强化日志输出
            logger.info(
                "✅ 配额(ENV生效): Fill %d/10s(突发%d), Reprice %d/10s(突发%d), Cancel %d/10s(突发%d)",
                self.fill_budget_10s, self.fill_burst, self.reprice_budget_10s, self.reprice_burst,
                self.cancel_budget_10s, self.cancel_burst
            )
            
            # Phase 8: TTL闸门日志
            logger.info(
                "✅ TTL撤单闸门: Cancel %d/10s(突发%d)",
                self.ttl_cancel_budget_10s, self.ttl_cancel_burst
            )
            logger.info(
                "✅ WeightMonitor: 阈值=%d, 冷却=%ds, 暖机=%ds, 保底Fill=%d/10s, 保底Cancel=%d/10s, 禁用降档=%s",
                self.weight_threshold, self.weight_cooldown_s, self.warmup_sec,
                self.min_safe_fill, self.min_safe_cancel, self.disable_degrade
            )
        else:
            self.new_order_gate = None
            self.cancel_gate = None
            logger.warning("⚠️ 速率限制闸门未启用")
        
        logger.info("✅ TurboConnector V2 with Rate Limiting 初始化完成")
    
    def _effective_quotas(self):
        """Phase 6: 获取实际生效的配额（含暖机和保底）"""
        # 禁用降档 或 暖机期内：按目标配额跑
        if self.disable_degrade or (time.time() - self.start_ts < self.warmup_sec):
            return (self.fill_budget_10s, self.cancel_budget_10s, self.reprice_budget_10s)
        
        # 正常：根据权重判断是否降档
        overweight = getattr(self, 'weight_monitor', None)
        over = False
        try:
            over = (overweight and overweight.value > self.weight_threshold)
        except Exception:
            over = False
        
        if over:
            # 有限度降档（保底不低于 min_safe_*）
            eff_fill = max(self.min_safe_fill, max(1, self.fill_budget_10s // 10))
            eff_cancel = max(self.min_safe_cancel, max(1, self.cancel_budget_10s // 2))
            eff_reprice = self.reprice_budget_10s
            logger.warning("⚠️ 权重超阈值，降档: Fill=%d/10s, Cancel=%d/10s (阈值=%d)",
                          eff_fill, eff_cancel, self.weight_threshold)
            return (eff_fill, eff_cancel, eff_reprice)
        else:
            return (self.fill_budget_10s, self.cancel_budget_10s, self.reprice_budget_10s)
        
    async def __aenter__(self):
        await self.initialize()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        
    def set_awg_pro(self, awg_instance):
        """设置AWG Pro实例引用用于错误上报"""
        self.awg_pro = awg_instance
        logger.debug("[TurboConnector] AWG Pro引用已设置")
    
    async def initialize(self):
        """初始化连接"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=5)
            self.connector = aiohttp.TCPConnector(limit=100, force_close=True)
            self.session = aiohttp.ClientSession(
                connector=self.connector,
                timeout=timeout
            )
            # 显示端点选择日志
            logger.info(f"🛠️ OrderEndpoint: base={self.base_url}, dry_run={os.getenv('DRY_RUN','0')}")
            
    async def close(self):
        """关闭连接"""
        if self.session:
            await self.session.close()
            self.session = None
        if self.connector:
            await self.connector.close()
            self.connector = None
    
    def get_session(self):
        """获取session对象（兼容旧策略）"""
        return self.session
            
    def _sign(self, params: dict) -> dict:
        """签名请求"""
        params['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        return params
        
    async def _request_with_weight(self, method: str, endpoint: str, params: dict = None, 
                                  signed: bool = False, critical: bool = False, 
                                  request_type: str = "general") -> dict:
        """统一请求方法 - 带权重监控"""
        if not self.session:
            await self.initialize()
            
        # Phase 2 A1: 权重自适应回退
        now = time.time()
        if now < self.weight_backoff_until and not critical:
            logger.info(f"[Connector] backoff until {self.weight_backoff_until-now:.1f}s")
            return None
            
        # Phase 6 P0-7: 高成本端点最小间隔硬闸
        if endpoint in self.endpoint_min_intervals:
            min_interval = self.endpoint_min_intervals[endpoint]
            last_call = self.endpoint_last_call.get(endpoint, 0)
            elapsed = time.time() - last_call
            if elapsed < min_interval:
                logger.warning(f"[Connector] {endpoint} 最小间隔未满足 ({elapsed:.1f}s < {min_interval}s)")
                if not critical:
                    return None
                # critical请求等待剩余时间
                wait_time = min_interval - elapsed
                logger.info(f"[Connector] 关键请求等待 {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
        
        # Phase 5: 优先使用AWG Pro进行权重检查
        if self.awg_pro:
            # 映射端点到AWG成本
            endpoint_costs = {
                '/api/v3/order': 'new_order',
                '/api/v3/order/cancelReplace': 'cancelReplace', 
                '/api/v3/openOrders': 'openOrders',
                '/api/v3/account': 'account',
                '/api/v3/depth': 'depth',
                '/api/v3/exchangeInfo': 'exchangeInfo',
                '/api/v3/order/test': 'test_order',
                '/api/v3/myTrades': 'myTrades'
            }
            
            awg_endpoint = endpoint_costs.get(endpoint, 'default')
            if not self.awg_pro.acquire(awg_endpoint):
                logger.warning(f"[AWG Pro] 权重配额不足，拒绝 {awg_endpoint} 请求")
                if not critical:
                    return None
                # critical请求等待一下再重试
                await asyncio.sleep(0.5)
                if not self.awg_pro.acquire(awg_endpoint):
                    logger.error(f"[AWG Pro] 关键请求 {awg_endpoint} 仍无配额")
                    return None
        
        # 备用：检查旧权重监控器
        elif self.weight_monitor and not self.weight_monitor.should_allow_request(critical=critical):
            logger.warning(f"⚠️ API权重冷却中，{request_type}请求被延迟")
            if not critical:
                return None
                
        url = f"{self.base_url}{endpoint}"
        headers = {'X-MBX-APIKEY': self.api_key} if self.api_key else {}
        
        if signed:
            if params is None:
                params = {}
            params = self._sign(params)
            
        start_time = time.perf_counter()
        
        try:
            if method == 'GET':
                resp = await self.session.get(url, params=params, headers=headers)
            elif method == 'POST':
                resp = await self.session.post(url, params=params, headers=headers)
            elif method == 'DELETE':
                resp = await self.session.delete(url, params=params, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
                
            # 更新权重监控
            if self.weight_monitor:
                self.weight_monitor.check_response_headers(dict(resp.headers))
            
            # Phase 2 A1: 读取权重头并自适应
            uw = resp.headers.get('X-MBX-USED-WEIGHT-1M', resp.headers.get('x-mbx-used-weight-1m'))
            if uw:
                self.used_weight_1m = int(uw)
                # 若使用权重逼近阈值 → 回退
                if self.used_weight_1m > self.weight_soft_wall:
                    self.weight_backoff_until = time.time() + self.weight_backoff_sec
                    logger.warning(f"[Connector] weight={self.used_weight_1m} > soft wall {self.weight_soft_wall} → backoff {self.weight_backoff_sec:.1f}s")
                
            # 更新延迟统计
            latency = (time.perf_counter() - start_time) * 1000
            self.request_count += 1
            self.total_latency += latency
            self.min_latency = min(self.min_latency, latency)
            self.max_latency = max(self.max_latency, latency)
            
            if resp.status == 200:
                # Phase 6 P0-7: 记录成功调用时间
                if endpoint in self.endpoint_min_intervals:
                    self.endpoint_last_call[endpoint] = time.time()
                
                # Phase 9 Fix: 权重统计与打点
                self._rest_calls[endpoint] = self._rest_calls.get(endpoint, 0) + 1
                self._rest_weight += self.WEIGHT_TABLE.get(endpoint, 1)
                
                # Phase 10: 记录消息类型用于CQM分析
                self._record_msg(endpoint, method)
                
                # 每10秒输出一次统计
                if time.time() - self._last_weight_emit > 10:
                    if self._rest_calls:
                        logger.info(f"[API Weight] 10s calls={dict(self._rest_calls)} weight_used={self._rest_weight}")
                    self._rest_calls.clear()
                    self._rest_weight = 0
                    self._last_weight_emit = time.time()
                
                return await resp.json()
            else:
                text = await resp.text()
                # Phase 5: -2011视为幂等成功（订单已不存在）
                if '-2011' in text or 'Unknown order' in text:
                    if method == 'DELETE' and 'order' in endpoint:
                        logger.info(f"ℹ️ 撤单幂等成功(已不存在): {params.get('orderId', 'N/A')}")
                        return {'code': -2011, 'treated_as': 'success', 'msg': 'Order already gone'}
                
                # Phase 3: cancelReplace特判 - 400 -2022且内部cancelResponse.code = -2011
                if method == 'POST' and 'cancelReplace' in endpoint:
                    if '"code":-2022' in text and '"cancelResponse":{"code":-2011' in text:
                        logger.info("ℹ️ cancelReplace撤单幂等成功(-2011)，新单未尝试；视为无害")
                        return {'code': -2022, 'treated_as': 'success', 'msg': 'cancelReplace: cancel -2011 treated as success'}
                # Phase 5: 检测418/-1003错误并上报到AWG Pro
                if resp.status == 418 or '-1003' in text or 'Too many requests' in text:
                    error_code = 418 if resp.status == 418 else -1003
                    logger.error(f"🔴 触发{error_code}(请求过频)，上报AWG Pro")
                    # 尝试上报到AWG Pro
                    if hasattr(self, 'awg_pro') and self.awg_pro:
                        try:
                            self.awg_pro.on_error(error_code)
                        except Exception as awg_e:
                            logger.warning(f"AWG Pro上报失败: {awg_e}")
                
                # 检测-1015错误
                if '-1015' in text:
                    logger.error("🔴 触发-1015(10秒新单速率限制)，进入2.5秒冷却")
                    # Phase 6 P0-3: -1015同样触发AWG熔断
                    if hasattr(self, 'awg_pro') and self.awg_pro:
                        try:
                            self.awg_pro.on_error(-1015)
                            logger.info("[TurboConnector] 已上报-1015到AWG Pro")
                        except Exception as awg_e:
                            logger.warning(f"AWG Pro上报(-1015)失败: {awg_e}")
                    await asyncio.sleep(2.5)
                logger.error(f"REST {method} {endpoint} 失败 ({latency:.1f}ms): HTTP {resp.status}: {text}")
                return None
                
        except Exception as e:
            latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求异常 ({latency:.1f}ms): {e}")
            return None
            
    # === 核心交易方法 ===
    
    async def get_orderbook_v2(self, symbol: str, limit: int = 1) -> dict:
        """获取订单簿"""
        params = {'symbol': symbol, 'limit': limit}
        return await self._request_with_weight('GET', '/api/v3/depth', params, request_type="orderbook")
        
    async def create_order_v2(self, symbol: str, side: str, order_type: str, 
                             timeInForce: str = None, quantity: str = None, price: str = None,
                             clientOrderId: str = None, tag: str = None, priority: str = 'normal', **kwargs) -> dict:
        """Phase 1: 下单 - 支持 clientOrderId/tag/priority"""
        # Phase 6 Fix: 软限制+硬闸双层（与AWG/usage联动）
        if hasattr(self, 'fill_gate_buy') and hasattr(self, 'fill_gate_sell'):
            # 使用双边预算
            if side.upper() == 'BUY':
                gate = self.fill_gate_buy
                side_budget = getattr(self, 'fill_budget_buy', 8)
                side_burst = getattr(self, 'fill_burst_buy', 8)
            else:
                gate = self.fill_gate_sell
                side_budget = getattr(self, 'fill_budget_sell', 8)
                side_burst = getattr(self, 'fill_burst_sell', 8)
                
            if gate and not gate.allow():
                pct = gate.usage_pct()
                count = gate.count()
                remaining = gate.remaining()
                
                # 获取当前权重使用率
                weight_usage = 0
                try:
                    if hasattr(self, 'weight_monitor') and self.weight_monitor:
                        status = self.weight_monitor.get_status()
                        weight_usage = status.get('usage_pct', 0)
                except:
                    pass
                
                # 软限制/硬闸双层逻辑
                SAFE_WALL = 15.0  # 安全墙阈值
                
                # 硬闸条件：超burst且接近安全墙
                if count >= side_burst * 1.2 and weight_usage >= SAFE_WALL:
                    msg = f"Fill gate限制[{side}]: {pct:.0f}% ({count}/{side_budget}/10s, 突发{side_burst}), weight={weight_usage:.1f}%≥{SAFE_WALL}%"
                    logger.warning("⛔ " + msg)
                    return None
                    
                # 软限制：仅记录不阻断
                if weight_usage < 8.0:
                    # 低用量，完全软限制
                    logger.debug(f"[GATE] {side} usage: {count}/{side_budget} ({pct:.0f}%), weight={weight_usage:.1f}% (soft)")
                elif weight_usage < 12.0:
                    # 中等用量，info级别
                    logger.info(f"[GATE] {side} usage: {count}/{side_budget} ({pct:.0f}%), weight={weight_usage:.1f}% (advisory)")
                else:
                    # 接近安全墙但未触发硬闸
                    logger.info(f"[GATE] {side} approaching limit: {count}/{side_burst} ({pct:.0f}%), weight={weight_usage:.1f}%")
        else:
            # Phase 5: 用 fill_gate 取代 new_order_gate (fallback)
            gate = getattr(self, 'fill_gate', getattr(self, 'new_order_gate', None))
            if gate and not gate.allow():
                pct = gate.usage_pct()
                count = gate.count()
                remaining = gate.remaining()
                budget = getattr(gate, 'budget', self.new_order_budget_10s)
                burst = getattr(gate, 'burst', budget)
                
                # Phase 6: 根据是否超过突发决定日志级别
                msg = f"Fill gate限制: {pct:.0f}% ({count}/{budget}/10s, 突发{burst}), 剩余:{remaining} → 跳过补槽下单"
                if count >= burst:
                    logger.warning("⛔ " + msg)  # 超过突发才警告
                else:
                    logger.info("⏸ " + msg)     # 正常触顶只是信息
                return None
            
        # Phase 9 B Fix 2 P0-1: Apply quantization before sending to API
        # DOGEUSDT specific rules (should be loaded from exchange_info)
        tick_size = 0.00001
        step_size = 1.0
        
        # 构建参数
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
        }
        
        if quantity is not None:
            # Ensure quantity is properly quantized for DOGE (integer only)
            qty_float = float(quantity)
            qty_int = int(qty_float)  # DOGE requires integer quantities
            params['quantity'] = str(qty_int)
            
        if price is not None:
            # Ensure price is properly quantized to tick size
            price_float = float(price)
            price_quantized = round(price_float / tick_size) * tick_size
            # Format with proper precision to avoid trailing zeros
            params['price'] = f"{price_quantized:.5f}"
            
        # ★ Phase 1: LIMIT_MAKER 不传 timeInForce，其它类型才传
        if order_type != 'LIMIT_MAKER' and timeInForce:
            params['timeInForce'] = timeInForce
            
        # Phase 1: 支持显式传入 clientOrderId
        if clientOrderId:
            params['newClientOrderId'] = clientOrderId
        else:
            # Phase 3 加固: 生成带前缀的clientOrderId
            import time
            import random
            import string
            timestamp_ms = int(time.time() * 1000)
            rand_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
            client_order_id = f"V78G-{timestamp_ms}-{rand_suffix}"
            params['newClientOrderId'] = client_order_id
        
        # Phase 2 A2: Fast-lane support - 快车道订单跳过慢对账
        is_critical = True if priority == 'fastlane' else True  # 默认都是critical
        result = await self._request_with_weight('POST', '/api/v3/order', params, 
                                                signed=True, critical=is_critical, request_type="order")
        
        # Phase 6: 先检查result是否为None
        if result is None:
            logger.debug("create_order_v2: 请求被限流或拒绝")
            return None
        
        # 必须拿到真实 orderId 才算成功
        if not (isinstance(result, dict) and result.get('orderId')):
            logger.error(f"❌ create_order_v2 未返回真实 orderId，响应={result}")
            return None
        
        # Phase 5: 按固定频率打印 fill 桶使用情况
        if gate and gate.count() % 5 == 0:
            stats = gate.get_stats()
            logger.info(f"📊 Fill gate: {stats['usage_pct']:.0f}% ({stats['current']}/{stats['budget']}/10s)")
        
        # Phase 1: 记录本地 meta（便于 TTL/TWAP/诊断）
        try:
            oid = str(result.get('orderId'))
            if oid:
                if not hasattr(self, '_local_meta'):
                    self._local_meta = {}
                self._local_meta[oid] = {
                    'clientOrderId': result.get('clientOrderId', clientOrderId or ''),
                    'tag': tag or ''
                }
        except Exception:
            pass
            
        return result
        
    async def cancel_order_v2(self, symbol: str, order_id: int, priority: str = 'normal') -> dict:
        """撤单 - Phase 8: 支持TTL专用通道和幂等处理"""
        # Phase 8: 选择闸门 - TTL用专门闸门
        gate = self.ttl_cancel_gate if priority == 'ttl' else self.cancel_gate
        
        # Phase 1: 添加路由日志，便于确认TTL撤单走对门
        try:
            logger.debug(f"[cancel] route priority={priority} -> gate={getattr(gate,'name','?')} "
                        f"budget={getattr(gate,'budget',0)}/10s burst={getattr(gate,'burst',0)}")
        except Exception:
            pass
        
        # 检查10秒撤单速率限制
        if gate and not gate.allow():
            pct = gate.usage_pct()
            logger.warning(f"⛔ 撤单速率限制(priority={priority}): {pct:.0f}% ({gate.count()}/10s), 跳过撤单")
            return None
            
        params = {
            'symbol': symbol,
            'orderId': order_id
        }
        
        try:
            result = await self._request_with_weight('DELETE', '/api/v3/order', params, 
                                                  signed=True, critical=True, request_type="cancel")
            return result
        except Exception as e:
            msg = str(e)
            # Phase 8: 统一幂等处理：-2011 unknown order / -2022 cancelReplace幂等
            if "-2011" in msg or "Unknown order" in msg or "-2022" in msg or "cancel -2011 treated as success" in msg:
                logger.info(f"ℹ️ cancel 幂等成功: {msg}")
                return {'status': 'FILLED_OR_CANCELLED', 'msg': 'idempotent_success'}
            raise

    async def cancel_replace_order(self, symbol: str, order_id: int, side: str, 
                                 quantity: str, price: str, price_protect: bool = True) -> dict:
        """
        V7.8 Final: cancelReplace 一次操作取消旧单并下新单
        使用币安的 /api/v3/order/cancelReplace 端点，节省API配额
        Phase 4: 使用独立的reprice_gate
        """
        # Phase 5: 使用专用的reprice_gate并定期打印使用情况
        gate = getattr(self, 'reprice_gate', self.new_order_gate)
        if gate and not gate.allow():
            pct = gate.usage_pct()
            budget = getattr(gate, 'budget', self.new_order_budget_10s)
            logger.warning(f"⛔ Reprice速率限制: {pct:.0f}% ({gate.count()}/{budget}/10s), 跳过重价")
            return None
        
        # Phase 5: 每5次打印一次reprice使用情况
        if gate and gate.count() % 5 == 0:
            stats = gate.get_stats()
            logger.info(f"📊 Reprice gate: {stats['usage_pct']:.0f}% ({stats['current']}/{stats['budget']}/10s)")
        
        params = {
            'symbol': symbol,
            'cancelReplaceMode': 'STOP_ON_FAILURE',  # 如果取消失败就不下新单
            'cancelOrderId': order_id,
            'side': side,
            'type': 'LIMIT_MAKER',  # 强制使用LIMIT_MAKER
            'quantity': quantity,
            'price': price,
        }
        
        try:
            result = await self._request_with_weight('POST', '/api/v3/order/cancelReplace', 
                                                   params, signed=True, critical=True, 
                                                   request_type="create")
            if result and result.get('cancelResult') == 'SUCCESS' and result.get('newOrderResult') == 'SUCCESS':
                new_order_id = result['newOrderResponse']['orderId']
                logger.debug(f"✅ cancelReplace成功: {order_id} -> {new_order_id}")
                return result
            else:
                logger.warning(f"⚠️ cancelReplace部分失败: {result}")
                return result
        except Exception as e:
            logger.error(f"❌ cancelReplace失败: {e}")
            return None
                                              
    async def get_open_orders(self, symbol: str = None) -> list:
        """获取挂单"""
        params = {}
        if symbol:
            params['symbol'] = symbol
        result = await self._request_with_weight('GET', '/api/v3/openOrders', params, 
                                                signed=True, request_type="query")
        return result if result else []
        
    async def get_account(self) -> dict:
        """获取账户信息"""
        return await self._request_with_weight('GET', '/api/v3/account', {}, 
                                              signed=True, request_type="account")
                                              
    async def test_order_v2(self, symbol: str, side: str, order_type: str, 
                           timeInForce: str, quantity: str, price: str) -> dict:
        """测试下单"""
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': quantity,
            'price': price
        }
        
        # LIMIT_MAKER不需要timeInForce参数
        if order_type != 'LIMIT_MAKER':
            params['timeInForce'] = timeInForce
        return await self._request_with_weight('POST', '/api/v3/order/test', params, 
                                              signed=True, request_type="test")
                                              
    # === 状态查询方法 ===
    
    def get_api_weight_status(self) -> dict:
        """获取API权重和速率状态"""
        status = {}
        
        # 分钟权重状态
        if self.weight_monitor:
            weight_status = self.weight_monitor.get_status()
            status['weight'] = {
                'current': weight_status['current_weight'],
                'max': weight_status['max_weight'],
                'threshold': self.weight_monitor.danger_threshold,
                'in_cooldown': weight_status['in_cooldown']
            }
            
        # 10秒速率状态
        if self.new_order_gate and self.cancel_gate:
            status['rate_limits'] = {
                'new_orders': {
                    'current': self.new_order_gate.count(),
                    'budget': self.new_order_gate.budget,
                    'usage_pct': self.new_order_gate.usage_pct(),
                    'remaining': self.new_order_gate.remaining()
                },
                'cancels': {
                    'current': self.cancel_gate.count(),
                    'budget': self.cancel_gate.budget,
                    'usage_pct': self.cancel_gate.usage_pct(),
                    'remaining': self.cancel_gate.remaining()
                }
            }
            
        return status
        
    def get_performance_stats_v2(self) -> dict:
        """获取性能统计"""
        if self.request_count == 0:
            return {
                'total_requests': 0,
                'avg_latency_ms': 0,
                'min_latency_ms': 0,
                'max_latency_ms': 0
            }
            
        return {
            'total_requests': self.request_count,
            'avg_latency_ms': self.total_latency / self.request_count,
            'min_latency_ms': self.min_latency,
            'max_latency_ms': self.max_latency
        }
        
    # === 兼容性别名 ===
    
    async def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        return await self.get_orderbook_v2(symbol, limit)
        
    async def create_order(self, symbol: str, side: str, order_type: str, 
                          **kwargs) -> dict:
        return await self.create_order_v2(symbol, side, order_type, 
                                         kwargs.get('timeInForce', 'GTC'),
                                         str(kwargs.get('quantity', 0)),
                                         str(kwargs.get('price', 0)))
                                         
    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self.cancel_order_v2(symbol, order_id)
    
    # === 交易规则获取 ===
    
    async def get_symbol_filters(self, symbol: str) -> dict:
        """获取交易对的过滤规则（PRICE_FILTER, LOT_SIZE, MIN_NOTIONAL等）"""
        try:
            # 使用meta类型请求，权重=1
            data = await self._request_with_weight(
                'GET', 
                '/api/v3/exchangeInfo',
                {'symbol': symbol}, 
                signed=False, 
                request_type='meta'
            )
            
            if not data or 'symbols' not in data or not data['symbols']:
                raise RuntimeError(f"exchangeInfo empty for {symbol}")
            
            # 找到指定交易对
            symbol_info = None
            for s in data['symbols']:
                if s['symbol'] == symbol:
                    symbol_info = s
                    break
            
            if not symbol_info:
                raise RuntimeError(f"Symbol {symbol} not found in exchangeInfo")
            
            # 解析filters
            filters = {}
            for f in symbol_info.get('filters', []):
                filter_type = f.get('filterType')
                
                if filter_type == 'PRICE_FILTER':
                    filters['price_filter'] = {
                        'min_price': float(f.get('minPrice', 0)),
                        'max_price': float(f.get('maxPrice', 0)),
                        'tick_size': float(f.get('tickSize', 0))
                    }
                elif filter_type == 'LOT_SIZE':
                    filters['lot_size'] = {
                        'min_qty': float(f.get('minQty', 0)),
                        'max_qty': float(f.get('maxQty', 0)),
                        'step_size': float(f.get('stepSize', 0))
                    }
                elif filter_type == 'MIN_NOTIONAL':
                    filters['min_notional'] = float(f.get('notional', 0))
                elif filter_type == 'NOTIONAL':
                    filters['notional'] = {
                        'min_notional': float(f.get('minNotional', 0)),
                        'max_notional': float(f.get('maxNotional', 0))
                    }
            
            # 添加基础信息
            filters['base_asset'] = symbol_info.get('baseAsset')
            filters['quote_asset'] = symbol_info.get('quoteAsset')
            filters['status'] = symbol_info.get('status')
            
            logger.info(f"✅ 获取{symbol}交易规则成功: {filters}")
            return filters
            
        except Exception as e:
            logger.error(f"❌ 获取{symbol}交易规则失败: {e}")
            # 返回默认值避免策略崩溃
            return {
                'price_filter': {'min_price': 0.00001, 'max_price': 1000000, 'tick_size': 0.00001},
                'lot_size': {'min_qty': 1, 'max_qty': 1000000, 'step_size': 1},
                'min_notional': 5.0,
                'base_asset': symbol[:4] if len(symbol) > 4 else symbol,
                'quote_asset': symbol[4:] if len(symbol) > 4 else 'USDT',
                'status': 'TRADING'
            }
    
    # === WebSocket订单簿支持 ===
    
    async def subscribe_orderbook_ws(self, symbol: str, callback, depth: int = 5):
        """订阅WebSocket订单簿（权重消耗=0）"""
        ws_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@depth{depth}@100ms"
        self.ws_orderbook = {}
        
        async def handle_message(ws):
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    self.ws_orderbook = {
                        'bids': [[float(p), float(q)] for p, q in data.get('b', [])],
                        'asks': [[float(p), float(q)] for p, q in data.get('a', [])],
                        'timestamp': time.time()
                    }
                    if callback:
                        await callback(self.ws_orderbook)
                        
        try:
            async with self.session.ws_connect(ws_url) as ws:
                logger.info(f"✅ WebSocket订单簿已连接: {symbol}")
                await handle_message(ws)
        except Exception as e:
            logger.error(f"❌ WebSocket订单簿错误: {e}")
            
    def get_ws_orderbook(self) -> dict:
        """获取WebSocket缓存的订单簿（无权重消耗）"""
        if hasattr(self, 'ws_orderbook') and self.ws_orderbook:
            if time.time() - self.ws_orderbook.get('timestamp', 0) < 5:  # 5秒内有效
                return self.ws_orderbook
        return None
    
    # === Phase 9 C Fix: aggTrade市场成交印支持 ===
    
    async def subscribe_aggtrade_ws(self, symbol: str, callback):
        """订阅WebSocket aggTrade市场成交印（权重消耗=0）"""
        ws_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@aggTrade"
        self.ws_trades = []
        self.last_trade_cleanup = time.time()
        
        async def handle_message(ws):
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    # 处理aggTrade数据
                    trade_data = {
                        'symbol': data.get('s'),
                        'price': float(data.get('p', 0)),
                        'qty': float(data.get('q', 0)),
                        'timestamp': data.get('T', 0),  # 成交时间戳
                        'is_maker': not data.get('m', True)  # m=true表示买方是maker
                    }
                    
                    # 保存最近30秒的成交记录
                    now = time.time()
                    current_ms = now * 1000
                    
                    # 清理30秒前的数据（每5秒清理一次）
                    if now - self.last_trade_cleanup > 5:
                        self.ws_trades = [t for t in self.ws_trades 
                                        if current_ms - t['timestamp'] < 30000]
                        self.last_trade_cleanup = now
                    
                    # 添加新成交记录
                    self.ws_trades.append(trade_data)
                    
                    # 回调处理
                    if callback:
                        await callback(trade_data, self.ws_trades)
                        
        try:
            async with self.session.ws_connect(ws_url) as ws:
                logger.info(f"✅ WebSocket aggTrade已连接: {symbol}")
                await handle_message(ws)
        except Exception as e:
            logger.error(f"❌ WebSocket aggTrade错误: {e}")
    
    def get_recent_trades(self, seconds: int = 30) -> List[dict]:
        """获取最近N秒的成交记录（无权重消耗）"""
        if not hasattr(self, 'ws_trades') or not self.ws_trades:
            return []
            
        now_ms = time.time() * 1000
        cutoff_ms = now_ms - (seconds * 1000)
        
        return [t for t in self.ws_trades if t['timestamp'] >= cutoff_ms]
    
    def get_trade_stats_by_price(self, target_price: float, seconds: int = 30) -> dict:
        """Phase 9 C Fix Step 4: 获取指定价位的成交统计（用于QLE的take_rate计算）"""
        recent_trades = self.get_recent_trades(seconds)
        if not recent_trades:
            return {'count': 0, 'volume': 0, 'rate_per_sec': 0.0}
        
        # Phase 9 C Fix Step 4: 放宽价格匹配条件，使用更大的价格容差
        tick_size = 0.00001  # DOGEUSDT的tick size
        price_tolerance = tick_size * 3.0  # 扩大到3个tick的容差
        
        price_trades = [t for t in recent_trades 
                       if abs(t['price'] - target_price) <= price_tolerance]
        
        # 如果精确匹配没有结果，使用更宽泛的区间统计
        if not price_trades:
            # 使用更宽的价格区间（±5 ticks）来确保能捕获到交易数据
            wider_tolerance = tick_size * 5.0
            price_trades = [t for t in recent_trades 
                           if abs(t['price'] - target_price) <= wider_tolerance]
        
        total_volume = sum(t['qty'] for t in price_trades)
        rate_per_sec = total_volume / seconds if seconds > 0 else 0.0
        
        # Phase 9 C Fix Step 4: 添加调试信息以便验证
        if rate_per_sec > 0:
            logger.debug(f"[aggTrade] target_price={target_price:.5f} matches={len(price_trades)} "
                        f"volume={total_volume:.1f} rate={rate_per_sec:.3f}/s")
        
        return {
            'count': len(price_trades),
            'volume': total_volume,
            'rate_per_sec': rate_per_sec,
            'timestamp': time.time()
        }
    
    # ========== Phase 1: User Data Stream Methods ==========
    
    async def create_listen_key(self):
        """创建listenKey用于User Data Stream"""
        try:
            # 走AWG授权（低权重）
            if self.awg_pro:
                self.awg_pro.acquire('userDataStream', cost=1)
            
            url = f"{self.base_url}/api/v3/userDataStream"
            headers = {'X-MBX-APIKEY': self.api_key}
            
            async with self.session.post(url, headers=headers) as resp:
                result = await resp.json()
                
                if 'listenKey' in result:
                    logger.info(f"✅ Created listenKey: {result['listenKey'][:8]}...")
                    return result['listenKey']
                else:
                    logger.error(f"❌ Failed to create listenKey: {result}")
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Error creating listenKey: {e}")
            return None
    
    async def keepalive_listen_key(self, listen_key: str):
        """续期listenKey（每25分钟调用一次）"""
        try:
            # 走AWG授权（低权重）
            if self.awg_pro:
                self.awg_pro.acquire('userDataStream', cost=1)
            
            url = f"{self.base_url}/api/v3/userDataStream"
            headers = {'X-MBX-APIKEY': self.api_key}
            params = {'listenKey': listen_key}
            
            async with self.session.put(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    logger.debug(f"✅ Keepalive listenKey success")
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"❌ Keepalive failed: {text}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error keepalive listenKey: {e}")
            return False
    
    async def close_listen_key(self, listen_key: str):
        """关闭listenKey"""
        try:
            # 走AWG授权（低权重）
            if self.awg_pro:
                self.awg_pro.acquire('userDataStream', cost=1)
            
            url = f"{self.base_url}/api/v3/userDataStream"
            headers = {'X-MBX-APIKEY': self.api_key}
            params = {'listenKey': listen_key}
            
            async with self.session.delete(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    logger.debug(f"✅ Closed listenKey")
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"❌ Close listenKey failed: {text}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error closing listenKey: {e}")
            return False
    
    # ========== Phase 10: Budget Governor Support Methods ==========
    
    def _record_msg(self, endpoint: str, method: str):
        """
        记录消息类型用于CQM分析
        
        Args:
            endpoint: API端点路径
            method: HTTP方法
        """
        now = time.time()
        msg_type = None
        
        # 分类消息类型
        if endpoint.endswith('/api/v3/order') and method == 'POST':
            msg_type = 'fill'
        elif endpoint.endswith('/api/v3/order/cancelReplace') and method == 'POST':
            msg_type = 'reprice'
        elif endpoint.endswith('/api/v3/order') and method == 'DELETE':
            msg_type = 'cancel'
            
        if msg_type:
            self._msg_hist.append((now, msg_type))
    
    def get_msg_counts(self, window_s=10):
        """
        获取滑窗内的消息计数
        
        Args:
            window_s: 时间窗口（秒）
            
        Returns:
            dict: {'fill': count, 'reprice': count, 'cancel': count}
        """
        cutoff = time.time() - window_s
        counts = defaultdict(int)
        
        for ts, msg_type in self._msg_hist:
            if ts >= cutoff:
                counts[msg_type] += 1
                
        return dict(counts)
    
    def set_dynamic_budgets(self, fill_10s, reprice_10s, cancel_10s,
                           burst_fill=None, burst_reprice=None, burst_cancel=None,
                           fill_10s_buy=None, fill_10s_sell=None,
                           burst_fill_buy=None, burst_fill_sell=None):
        """
        设置动态预算，将BudgetGovernor的预算应用到内部token bucket
        
        Phase 6 M1增强：支持双边预算分水
        
        Args:
            fill_10s: 10秒内填单预算（总量）
            reprice_10s: 10秒内重价预算
            cancel_10s: 10秒内撤单预算
            burst_fill: 填单突发预算（总量）
            burst_reprice: 重价突发预算
            burst_cancel: 撤单突发预算
            fill_10s_buy: 买单10秒预算（Phase 6 M1）
            fill_10s_sell: 卖单10秒预算（Phase 6 M1）
            burst_fill_buy: 买单突发预算（Phase 6 M1）
            burst_fill_sell: 卖单突发预算（Phase 6 M1）
        """
        try:
            # Phase 6 M1: 如果提供了双边预算，使用它们；否则均分
            if fill_10s_buy is not None and fill_10s_sell is not None:
                self.fill_budget_buy = fill_10s_buy
                self.fill_budget_sell = fill_10s_sell
                self.fill_burst_buy = burst_fill_buy or fill_10s_buy
                self.fill_burst_sell = burst_fill_sell or fill_10s_sell
                
                # 创建或更新买卖侧的独立gate
                if not hasattr(self, 'fill_gate_buy'):
                    from doge_mm.packages.risk.rate_limiter import TokenBucketRateLimiter
                    self.fill_gate_buy = TokenBucketRateLimiter(
                        rate_limit=fill_10s_buy / 10.0,
                        burst_limit=self.fill_burst_buy,
                        time_window=10.0
                    )
                    self.fill_gate_sell = TokenBucketRateLimiter(
                        rate_limit=fill_10s_sell / 10.0,
                        burst_limit=self.fill_burst_sell,
                        time_window=10.0
                    )
                    # Phase 6 M1 Fix: 更新tokens到突发值
                    self.fill_gate_buy.tokens = self.fill_burst_buy
                    self.fill_gate_sell.tokens = self.fill_burst_sell
                else:
                    # Phase 6 Fix: 更新所有参数，显著上调时预充burst
                    old_buy_budget = self.fill_gate_buy.budget
                    old_sell_budget = self.fill_gate_sell.budget
                    
                    self.fill_gate_buy.rate_limit = fill_10s_buy / 10.0
                    self.fill_gate_buy.budget = fill_10s_buy
                    self.fill_gate_buy.burst_limit = self.fill_burst_buy
                    
                    self.fill_gate_sell.rate_limit = fill_10s_sell / 10.0
                    self.fill_gate_sell.budget = fill_10s_sell
                    self.fill_gate_sell.burst_limit = self.fill_burst_sell
                    
                    # 显著上调(>30%)时，预充burst以便瞬时补位
                    if fill_10s_buy > old_buy_budget * 1.3:
                        self.fill_gate_buy.tokens = self.fill_burst_buy
                        logger.info(f"[GATE] BUY预充burst: {old_buy_budget}→{fill_10s_buy} (+{(fill_10s_buy/old_buy_budget-1)*100:.0f}%)")
                    else:
                        self.fill_gate_buy.tokens = min(self.fill_gate_buy.tokens, self.fill_burst_buy)
                        
                    if fill_10s_sell > old_sell_budget * 1.3:
                        self.fill_gate_sell.tokens = self.fill_burst_sell
                        logger.info(f"[GATE] SELL预充burst: {old_sell_budget}→{fill_10s_sell} (+{(fill_10s_sell/old_sell_budget-1)*100:.0f}%)")
                    else:
                        self.fill_gate_sell.tokens = min(self.fill_gate_sell.tokens, self.fill_burst_sell)
                    
                logger.info(f"[CQM] 双边预算应用: buy={fill_10s_buy}/10s(burst{self.fill_burst_buy}), "
                           f"sell={fill_10s_sell}/10s(burst{self.fill_burst_sell})")
            
            # 原有的总量gate逻辑保留作为fallback
            if hasattr(self, 'fill_gate') and self.fill_gate:
                self.fill_gate.budget = fill_10s
                self.fill_gate.burst_limit = burst_fill or fill_10s
                
            if hasattr(self, 'reprice_gate') and self.reprice_gate:
                self.reprice_gate.budget = reprice_10s
                self.reprice_gate.burst_limit = burst_reprice or reprice_10s
                
            if hasattr(self, 'cancel_gate') and self.cancel_gate:
                self.cancel_gate.budget = cancel_10s
                self.cancel_gate.burst_limit = burst_cancel or cancel_10s
                
            # 更新实例变量以便日志显示正确
            self.fill_budget_10s = fill_10s
            self.reprice_budget_10s = reprice_10s
            self.cancel_budget_10s = cancel_10s
            self.fill_burst = burst_fill or fill_10s
            self.reprice_burst = burst_reprice or reprice_10s
            self.cancel_burst = burst_cancel or cancel_10s
                
            logger.info(f"[GOV] 动态预算应用成功: Fill={fill_10s}/10s(burst{burst_fill or fill_10s}), "
                       f"Reprice={reprice_10s}/10s(burst{burst_reprice or reprice_10s}), "
                       f"Cancel={cancel_10s}/10s(burst{burst_cancel or cancel_10s})")
                       
        except Exception as e:
            logger.warning(f"[GOV] 动态预算应用失败: {e}")
    
    # === Phase 2: Generic WebSocket Support for User Data Stream ===
    async def open_ws(self, ws_url: str, handler_callback=None, error_callback=None):
        """
        打开通用WebSocket连接（用于User Data Stream）
        
        Args:
            ws_url: WebSocket URL (e.g., wss://stream.binance.com:9443/ws/{listenKey})
            handler_callback: 异步回调函数处理接收的消息
            error_callback: 异步回调函数处理错误
        
        Returns:
            WebSocket connection object
        """
        if not self.session:
            await self.initialize()
        
        try:
            ws = await self.session.ws_connect(
                ws_url,
                heartbeat=30,  # 30秒心跳
                timeout=aiohttp.ClientTimeout(total=None)  # 无超时限制
            )
            
            logger.info(f"✅ WebSocket connected: {ws_url[:50]}...")
            
            # 启动消息处理循环
            if handler_callback:
                asyncio.create_task(self._ws_message_loop(ws, handler_callback, error_callback))
            
            return ws
            
        except Exception as e:
            logger.error(f"❌ Failed to open WebSocket: {e}")
            if error_callback:
                await error_callback(e)
            raise
    
    async def _ws_message_loop(self, ws, handler_callback, error_callback):
        """WebSocket消息处理循环"""
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await handler_callback(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Invalid JSON in WebSocket message: {e}")
                        if error_callback:
                            await error_callback(e)
                    except Exception as e:
                        logger.error(f"❌ Error processing WebSocket message: {e}")
                        if error_callback:
                            await error_callback(e)
                            
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"❌ WebSocket error: {ws.exception()}")
                    if error_callback:
                        await error_callback(ws.exception())
                    break
                    
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("⚠️ WebSocket connection closed")
                    break
                    
        except Exception as e:
            logger.error(f"❌ WebSocket message loop error: {e}")
            if error_callback:
                await error_callback(e)
        finally:
            if not ws.closed:
                await ws.close()
                logger.info("✅ WebSocket closed")