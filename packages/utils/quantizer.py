"""
Phase 9 B Fix 2 P0-1: Unified price/quantity/notional quantizer
Ensures all orders meet exchange precision and minimum requirements
"""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import logging

logger = logging.getLogger(__name__)


def quantize_price(px: float, tick: float) -> float:
    """Quantize price to exchange tick size"""
    q = Decimal(str(px)) / Decimal(str(tick))
    return float((q.to_integral_value(rounding=ROUND_DOWN) * Decimal(str(tick))))


def quantize_qty(qty: float, step: float) -> float:
    """Quantize quantity to exchange step size"""
    q = Decimal(str(qty)) / Decimal(str(step))
    return float((q.to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step))))


def min_qty_for_notional(price: float, min_notional: float, step: float) -> float:
    """Calculate minimum quantity to meet notional requirement"""
    need = Decimal(str(min_notional)) / Decimal(str(price))
    steps = (need / Decimal(str(step))).to_integral_value(rounding=ROUND_HALF_UP)
    if steps * Decimal(str(step)) * Decimal(str(price)) < Decimal(str(min_notional)):
        steps += 1
    return float(steps * Decimal(str(step)))


def pretrade_sanitize(side: str, price: float, qty: float, limits: dict) -> tuple:
    """
    Phase 9 B Fix 2: Sanitize order parameters before trading
    
    Args:
        side: 'BUY' or 'SELL'
        price: Raw price
        qty: Raw quantity
        limits: Exchange limits dict with tick_size, step_size, min_notional, min_qty
        
    Returns:
        Tuple of (quantized_price, quantized_qty, notional)
    """
    tick = limits.get('tick_size', 0.00001)
    step = limits.get('step_size', 1.0)
    min_notional = limits.get('min_notional', 1.0)
    min_qty = limits.get('min_qty', step)

    # Quantize price and quantity
    price_q = quantize_price(price, tick)
    qty_q = quantize_qty(qty, step)

    # Ensure minimum quantity
    if qty_q < min_qty:
        qty_q = quantize_qty(min_qty, step)

    # Check notional and adjust if needed
    notional = price_q * qty_q
    if notional < min_notional:
        need_qty = min_qty_for_notional(price_q, min_notional, step)
        qty_q = quantize_qty(need_qty, step)
        notional = price_q * qty_q

    # Log the sanitization for verification
    logger.info(f"[Sanity] in: px={price:.5f}, qty={qty:.1f} | "
                f"out: px_q={price_q:.5f}, qty_q={qty_q:.1f}, notional={notional:.2f}")

    return price_q, qty_q, notional


def fill_gate_scale(fill_per_10s: int, cap: int, floor: float = 0.30) -> float:
    """
    Phase 9 B Fix 2 P0-2: Soft decay instead of hard block for fill gate
    
    Returns a scale factor [floor, 1.0] instead of boolean block
    """
    if fill_per_10s <= 0:
        return 1.0
    ratio = min(fill_per_10s / cap, 1.0)
    scale = max(1.0 - ratio, floor)
    logger.debug(f"[FillGate] fill={fill_per_10s}/{cap} -> scale={scale:.2f}")
    return scale


def cashfloor_scale(usdt_free: float, floor: float, min_scale: float = 0.15) -> float:
    """
    Phase 9 B Fix 2 P0-3: Non-zero scale for cash floor
    
    Returns a scale factor [min_scale, 1.0] instead of 0
    """
    if usdt_free >= floor:
        return 1.0
    gap = max(floor - usdt_free, 0.0)
    scale = max(min_scale, 1.0 - gap / max(floor, 1e-9))
    logger.info(f"[CashFloor] usdt_free={usdt_free:.1f} < floor={floor:.1f} -> buy_scale={scale:.2f}")
    return scale