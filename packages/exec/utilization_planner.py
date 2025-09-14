# doge_mm/packages/exec/utilization_planner.py
from dataclasses import dataclass
from typing import Dict

@dataclass
class PlannerConfig:
    target_util: float = 0.95
    keep_usdt_cushion: float = 0.10   # 留出10% USDT 缓冲
    layer_weights: Dict[int, float] = None
    
    def __post_init__(self):
        if self.layer_weights is None:
            self.layer_weights = {0: 0.20, 1: 0.35, 2: 0.45}

class UtilizationPlanner:
    def __init__(self, cfg: PlannerConfig, logger):
        self.cfg = cfg
        self.log = logger

    def _dynamic_util(self, risk_signals):
        util = self.cfg.target_util
        if risk_signals.get('awg') == 'RECOVERING':
            util = min(util, 0.85)
        if risk_signals.get('awg') == 'OPEN':
            util = 0.0
        if risk_signals.get('mirror_age', 0) > 10:
            util = min(util, 0.70)
        if risk_signals.get('uds_age', 0) > 3:
            util = min(util, 0.75)
        return max(0.0, min(util, 0.98))

    def plan(self, equity_usdt, price,
             avail_usdt, avail_doge,
             doge_ratio, risk_signals,
             target_doge_ratio=0.5):
        util = self._dynamic_util(risk_signals)

        target_onbook = equity_usdt * util
        # 保留 cushion
        cushion = equity_usdt * self.cfg.keep_usdt_cushion
        usable_usdt = max(0.0, avail_usdt - cushion)

        # 按库存偏移对侧边预算倾斜
        # 基础：两侧均分
        buy_share = sell_share = 0.5
        if doge_ratio > target_doge_ratio + 0.1:     # DOGE 偏多 → 卖侧多
            buy_share, sell_share = 0.4, 0.6
        elif doge_ratio < target_doge_ratio - 0.1:   # USDT 偏多 → 买侧多
            buy_share, sell_share = 0.6, 0.4

        buy_budget = target_onbook * buy_share
        sell_budget = target_onbook * sell_share

        # 余额下限保护：BUY 不得超过 USDT 可用；SELL 不得超过 DOGE 可用名义
        buy_budget = min(buy_budget, usable_usdt)
        sell_budget = min(sell_budget, avail_doge * price)

        # 层级分配
        layer_budgets_buy = {lv: buy_budget * w for lv, w in self.cfg.layer_weights.items()}
        layer_budgets_sell = {lv: sell_budget * w for lv, w in self.cfg.layer_weights.items()}

        return {
            'util_eff': util,
            'target_onbook': target_onbook,
            'buy': layer_budgets_buy,
            'sell': layer_budgets_sell,
        }
    
    def get_order_size(self) -> float:
        """
        获取智能订单金额（供AdaptiveSizer使用）
        基于当前资金利用率和市场状况动态计算
        """
        # 获取当前状态
        balances = getattr(self, '_last_balances', {
            'USDT': {'free': 200.0},
            'DOGE': {'free': 1000.0}
        })
        price = getattr(self, '_last_price', 0.25)
        
        # 计算总权益
        equity_usdt = balances['USDT']['free'] + balances['DOGE']['free'] * price
        
        # 基础订单金额：权益的2-5%
        base_order_pct = self.cfg.cfg.get('order_usd_max_frac', 0.02)
        base_order_usd = equity_usdt * base_order_pct
        
        # 根据利用率调整：利用率低时增大订单
        current_util = getattr(self, '_last_util', 0.1)
        if current_util < 0.3:
            # 利用率很低，订单放大2-3倍
            multiplier = 2.5
        elif current_util < 0.6:
            # 利用率中等，订单放大1.5倍
            multiplier = 1.5
        else:
            # 利用率较高，正常订单
            multiplier = 1.0
        
        # 计算最终订单金额
        order_usd = base_order_usd * multiplier
        
        # 应用上下限
        min_order = self.cfg.cfg.get('order_usd_min', 10)
        max_order = self.cfg.cfg.get('order_usd_max', 150)
        order_usd = max(min_order, min(max_order, order_usd))
        
        return order_usd
    
    def update_state(self, balances: dict, price: float, util: float):
        """更新内部状态供get_order_size使用"""
        self._last_balances = balances
        self._last_price = price
        self._last_util = util