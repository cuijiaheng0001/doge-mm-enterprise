"""
InstrumentMaster - 品种主数据服务
Layer 0.0
"""

class InstrumentMaster:
    """集中管理所有交易品种的完整信息"""

    def __init__(self):
        self.instruments = {}

    def get_instrument(self, symbol):
        """获取品种信息"""
        pass

    def get_trading_status(self, symbol):
        """获取交易状态"""
        pass

    def get_fee_schedule(self, symbol, user_level):
        """获取费率信息"""
        pass
