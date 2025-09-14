"""
SSOTReservationClosedLoop - SSOT预留闭环
Layer 2.2
"""

class SSOTReservationClosedLoop:
    """确保资金一致性 + 订单状态机管理"""

    def register_order(self, order, client_order_id):
        """注册订单"""
        pass

    def on_order_ack(self, order_id):
        """订单确认"""
        pass

    def on_order_filled(self, order_id, amount):
        """订单成交"""
        pass
