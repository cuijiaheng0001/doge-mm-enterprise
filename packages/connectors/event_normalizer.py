"""
Event Normalizer - 机构级事件规范化层
对标Jane Street/Citadel标准，统一所有交易所/网关的事件格式
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class ExecReport:
    """标准化执行报告 - 所有交易所/网关的统一格式"""
    order_id: int
    symbol: str
    side: Literal['BUY', 'SELL']
    order_status_raw: str   # X - 原始状态
    exec_type_raw: str      # x - 执行类型
    status: Literal['NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'PENDING_CANCEL']
    last_qty: Decimal       # l - 本次成交量
    cum_qty: Decimal        # z - 累计成交量
    last_quote: Decimal     # Y - 本次成交额（无则按last_qty*price回推）
    cum_quote: Decimal      # Z - 累计成交额
    price: Decimal          # p or L - 成交价格
    is_maker: bool          # m - 是否maker成交
    ts: int                 # E/T - 事件时间戳
    update_id: int          # u - 更新序号（用于去重）

class EventNormalizer:
    """事件规范化器 - 统一所有来源的差异"""
    
    # 状态别名映射表 - 关键修复：PARTIAL_FILL → PARTIALLY_FILLED
    STATUS_ALIAS = {
        'PARTIAL_FILL': 'PARTIALLY_FILLED',      # 关键映射
        'PARTIALLYFILLED': 'PARTIALLY_FILLED',   # 偶见连写
        'PENDING_CANCEL': 'PENDING_CANCEL',      # 保持
        'PENDING_NEW': 'NEW',                    # 待确认新订单
    }
    
    @classmethod
    def normalize_execution_report(cls, raw_event: dict) -> ExecReport:
        """将原始executionReport转换为标准化ExecReport"""
        
        def _safe_upper(v):
            """安全转大写"""
            return (v or '').upper()
        
        def _safe_decimal(v, default=Decimal(0)):
            """安全转Decimal"""
            try:
                return Decimal(str(v or 0))
            except:
                return default
        
        def _safe_int(v, default=0):
            """安全转int"""
            try:
                return int(v or 0)
            except:
                return default
        
        # 兼容多种键名格式（Binance原始/封装后/标准格式）
        order_id = raw_event.get('i') or raw_event.get('orderId') or raw_event.get('order_id')
        symbol = raw_event.get('s') or raw_event.get('symbol') or 'DOGEUSDT'
        side = _safe_upper(raw_event.get('S') or raw_event.get('side'))
        
        # 状态字段（原始+别名映射）- 关键处理
        status_raw = _safe_upper(raw_event.get('X') or raw_event.get('orderStatus') or raw_event.get('status'))
        exec_raw = _safe_upper(raw_event.get('x') or raw_event.get('executionType'))
        
        # 应用别名映射 - 修复PARTIAL_FILL问题
        status = cls.STATUS_ALIAS.get(status_raw, status_raw)
        
        # 成交量字段
        last_qty = _safe_decimal(raw_event.get('l') or raw_event.get('lastQty'))
        cum_qty = _safe_decimal(raw_event.get('z') or raw_event.get('cumQty'))
        
        # 成交额字段
        last_quote = _safe_decimal(raw_event.get('Y') or raw_event.get('lastQuote'))
        cum_quote = _safe_decimal(raw_event.get('Z') or raw_event.get('cumQuote'))
        
        # 价格字段
        price = _safe_decimal(raw_event.get('p') or raw_event.get('L') or raw_event.get('price'))
        
        # 如果缺少last_quote，用last_qty*price计算（关键修复）
        if last_quote == 0 and last_qty > 0 and price > 0:
            last_quote = last_qty * price
            logger.debug(f"[EventNormalizer] Calculated lastQuote={last_quote} from qty*price")
        
        # 其他字段
        is_maker = raw_event.get('m', False)
        ts = _safe_int(raw_event.get('E') or raw_event.get('T') or raw_event.get('ts'))
        update_id = _safe_int(raw_event.get('u') or raw_event.get('update_id'))
        
        # 日志记录映射结果
        if status_raw != status:
            logger.info(f"[EventNormalizer] Status mapped: {status_raw}→{status} for order {order_id}")
        
        logger.debug(f"[EventNormalizer] Normalized: oid={order_id} status={status} "
                    f"lastQty={last_qty} cumQty={cum_qty} exec={exec_raw}")
        
        return ExecReport(
            order_id=_safe_int(order_id),
            symbol=symbol,
            side=side,
            order_status_raw=status_raw,
            exec_type_raw=exec_raw,
            status=status,
            last_qty=last_qty,
            cum_qty=cum_qty,
            last_quote=last_quote,
            cum_quote=cum_quote,
            price=price,
            is_maker=is_maker,
            ts=ts,
            update_id=update_id
        )
    
    @classmethod
    def to_shadow_format(cls, exec_report: ExecReport) -> dict:
        """将标准化ExecReport转换为Shadow Balance期望的格式"""
        return {
            'order_id': str(exec_report.order_id),
            'orderId': str(exec_report.order_id),     # 兼容多种键名
            'i': exec_report.order_id,                # Binance原始键
            
            'symbol': exec_report.symbol,
            's': exec_report.symbol,
            
            'side': exec_report.side,
            'S': exec_report.side,
            
            'status': exec_report.status,             # 已经是PARTIALLY_FILLED
            'orderStatus': exec_report.status,        # 双保险
            'X': exec_report.order_status_raw,        # 保留原始
            
            'executionType': exec_report.exec_type_raw,
            'x': exec_report.exec_type_raw,
            
            'lastQty': float(exec_report.last_qty),
            'l': float(exec_report.last_qty),
            
            'cumQty': float(exec_report.cum_qty),
            'z': float(exec_report.cum_qty),
            
            'lastQuote': float(exec_report.last_quote),
            'Y': float(exec_report.last_quote),
            
            'cumQuote': float(exec_report.cum_quote),
            'Z': float(exec_report.cum_quote),
            
            'price': float(exec_report.price),
            'p': float(exec_report.price),
            'L': float(exec_report.price),
            
            'is_maker': exec_report.is_maker,
            'm': exec_report.is_maker,
            
            'ts': exec_report.ts,
            'E': exec_report.ts,
            'T': exec_report.ts,
            
            'update_id': exec_report.update_id,
            'u': exec_report.update_id
        }