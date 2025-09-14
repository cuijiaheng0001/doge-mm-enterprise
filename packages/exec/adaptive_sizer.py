# doge_mm/packages/exec/adaptive_sizer.py
import math
from dataclasses import dataclass
from typing import Dict

@dataclass
class MarketSnapshot:
    mid: float
    spread: float               # mid 相对基点或 ticks
    bid_size_top: float
    ask_size_top: float
    realized_vol_30s: float     # 30s 实现波动(年化/根时 or 简化%)
    recent_fill_rate: float     # 过去1-5min 成交率 0~1

@dataclass
class InventoryState:
    doge_ratio: float           # DOGE 价值占比 (0~1)
    target_ratio: float = 0.5

@dataclass
class SizerConfig:
    min_qty: float
    step_size: float
    min_notional: float
    layer_mult: Dict[int, float] = None
    max_single_notional: float = 500.0  # 保险上限，可调
    
    def __post_init__(self):
        if self.layer_mult is None:
            self.layer_mult = {0:0.5, 1:1.0, 2:1.5, 3:2.0}

class AdaptiveSizer:
    def __init__(self, cfg: SizerConfig, logger):
        self.cfg = cfg
        self.log = logger

    def _round_step(self, qty):
        step = self.cfg.step_size
        return math.floor(qty / step) * step

    def _ensure_notional(self, qty, price):
        # 对齐最小名义金额要求
        if qty * price < self.cfg.min_notional:
            qty = self.cfg.min_notional / price
        return qty

    def suggest_qty(self, side: str, price: float, layer: int,
                    budget_per_order: float,        # 该笔允许使用的名义金额(USDT)
                    avail_usdt: float, avail_doge: float,
                    mkt: MarketSnapshot, inv: InventoryState) -> float:
        # 1) 以预算为基：把名义金额换成初始 qty
        base_qty = budget_per_order / price

        # 2) 市场深度因子：Top-of-book 越厚，size 越小（避免排位靠后）；越薄，size 可更大
        depth_top = mkt.ask_size_top if side == 'BUY' else mkt.bid_size_top
        depth_factor = 1.0
        if depth_top >= 200: depth_factor = 0.8
        elif depth_top <= 30: depth_factor = 1.2

        # 3) 波动率因子：高波更小，低波更大
        vol = max(0.0001, mkt.realized_vol_30s)
        if vol > 0.05: vol_factor = 0.6
        elif vol < 0.02: vol_factor = 1.4
        else: vol_factor = 1.0

        # 4) 成交率因子：低成交率→放大，超高→缩小
        fr = mkt.recent_fill_rate
        if fr < 0.2: fill_factor = 1.5
        elif fr > 0.8: fill_factor = 0.7
        else: fill_factor = 1.0

        # 5) 库存偏移：DOGE 过多 → SELL 放大 / BUY 缩小；USDT 过多相反
        bias = 1.0
        if inv.doge_ratio > inv.target_ratio + 0.1:
            bias = 0.8 if side == 'BUY' else 1.2
        elif inv.doge_ratio < inv.target_ratio - 0.1:
            bias = 1.2 if side == 'BUY' else 0.8

        # 6) 层级倍率
        layer_mult = self.cfg.layer_mult.get(layer, 1.0)

        qty = base_qty * depth_factor * vol_factor * fill_factor * bias * layer_mult

        # 7) 余额约束：BUY 受 USDT，SELL 受 DOGE
        if side == 'BUY':
            # 不能超过可用 USDT
            max_qty_by_usdt = (avail_usdt / price) * 0.95  # 给手续费和缓冲留5%
            qty = min(qty, max_qty_by_usdt)
        else:
            qty = min(qty, avail_doge * 0.95)

        # 8) 保护上限（名义金额）
        if qty * price > self.cfg.max_single_notional:
            qty = self.cfg.max_single_notional / price

        # 9) 对齐交易所规则
        qty = self._round_step(qty)
        qty = self._ensure_notional(qty, price)

        # 10) 防止负或0
        qty = max(qty, self._round_step(self.cfg.min_qty))

        return qty