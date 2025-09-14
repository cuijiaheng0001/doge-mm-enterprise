"""
极薄主循环 - 世界级做市商架构核心
只有20行核心代码，永远不会膨胀
"""

class Engine:
    """极简主循环 - 只做协调，不含业务逻辑"""

    def __init__(self, refs, mkt, acct, risk, pricing, execu, hedge, ops):
        """初始化8个域管理器"""
        self.refs, self.mkt, self.acct = refs, mkt, acct
        self.risk, self.pricing, self.execu = risk, pricing, execu
        self.hedge, self.ops = hedge, ops

    def on_market_tick(self, tick):
        """市场数据事件驱动"""
        snap = self.mkt.get_snapshot()
        quotes = self.pricing.calculate_quotes(snap)          # 只定价
        orders = self.execu.generate_orders(quotes)           # 只编排生成
        approved = [o for o in orders if self.risk.pretrade_check(o).approved]
        self.execu.execute_batch(approved)                    # 只执行

    def on_fill(self, fill):
        """成交事件驱动"""
        self.acct.reconcile_now()
        delta = self.hedge.calc_delta()                      # PositionBook/DeltaBus
        self.hedge.on_delta(delta)

    def on_timer(self):
        """定时器事件驱动"""
        self.ops.quality_report()
        if self.ops.should_kill():
            self.execu.kill_switch()