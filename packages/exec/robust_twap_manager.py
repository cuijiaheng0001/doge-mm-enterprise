"""
TWAP稳健性管理器 - 解决启动阶段和市场数据不稳定时的TWAP执行问题
"""
import time
import logging
from typing import Dict, Any, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


class RobustTWAPManager:
    """稳健的TWAP管理器 - 处理市场数据不稳定和启动阶段问题"""
    
    def __init__(self, startup_delay: float = 5.0):  # Phase4: 默认5秒启动延迟
        # 基础配置
        self.min_mid_price = 0.00001        # 最小有效中价
        self.max_mid_price = 1000.0         # 最大有效中价
        self.min_slice_usd = 1.0           # 最小切片金额
        self.max_slice_usd = 1000.0        # 最大切片金额
        
        # 数据稳定性检查
        self.price_history = deque(maxlen=10)    # 价格历史
        self.stability_window = 30.0              # 稳定性检查窗口(秒)
        self.min_stable_samples = 1               # Phase4紧急优化: 3样本→1样本
        self.max_price_deviation = 0.15          # Phase4紧急优化: 10%偏差→15%偏差
        
        # 启动延迟保护 - Phase 4: 默认5秒快速启动
        self.startup_delay = startup_delay        # 启动延迟(秒) - Phase4默认5s
        self.startup_time = time.time()
        
        # 重试机制
        self.max_retries = 3
        self.retry_delay = 5.0
        self.failed_attempts = deque(maxlen=5)
        
        logger.info(f"[TWAP-Robust] Manager initialized: startup_delay={self.startup_delay}s")
    
    def update_price_data(self, mid_price: float):
        """更新价格数据用于稳定性检查"""
        now = time.time()
        if self.is_valid_price(mid_price):
            self.price_history.append((now, mid_price))
            self._cleanup_old_data()
    
    def _cleanup_old_data(self):
        """清理过期价格数据"""
        now = time.time()
        cutoff = now - self.stability_window
        while self.price_history and self.price_history[0][0] < cutoff:
            self.price_history.popleft()
    
    def is_valid_price(self, price: float) -> bool:
        """检查价格是否有效"""
        return (self.min_mid_price <= price <= self.max_mid_price)
    
    def is_startup_phase(self) -> bool:
        """检查是否处于启动阶段"""
        return (time.time() - self.startup_time) < self.startup_delay
    
    def is_price_stable(self) -> bool:
        """检查价格是否稳定"""
        if len(self.price_history) < self.min_stable_samples:
            return False
            
        prices = [p for _, p in self.price_history]
        if not prices:
            return False
            
        # 计算价格稳定性
        mean_price = sum(prices) / len(prices)
        max_deviation = max(abs(p - mean_price) / mean_price for p in prices)
        
        return max_deviation <= self.max_price_deviation
    
    def can_execute_twap(self, mid_price: float, reason: str = "") -> Tuple[bool, str]:
        """
        综合检查是否可以执行TWAP
        
        Returns:
            (can_execute: bool, reason: str)
        """
        # 检查1：启动阶段保护
        if self.is_startup_phase():
            remaining = self.startup_delay - (time.time() - self.startup_time)
            return False, f"startup_delay (剩余{remaining:.1f}s)"
        
        # 检查2：价格有效性
        if not self.is_valid_price(mid_price):
            return False, f"invalid_price ({mid_price})"
        
        # 检查3：价格稳定性
        if not self.is_price_stable():
            stable_samples = len(self.price_history)
            return False, f"price_unstable (样本{stable_samples}/{self.min_stable_samples})"
        
        # 检查4：最近失败记录
        recent_failures = len([ts for ts in self.failed_attempts 
                              if time.time() - ts < self.retry_delay])
        if recent_failures >= self.max_retries:
            return False, f"too_many_failures ({recent_failures}/{self.max_retries})"
        
        return True, f"ready ({reason})"
    
    def safe_calculate_quantity(self, slice_usd: float, mid_price: float, 
                              direction: str) -> Optional[float]:
        """
        安全计算TWAP切片数量，包含多重保护
        
        Returns:
            数量 或 None (如果计算失败)
        """
        try:
            # 验证输入
            if not self.is_valid_price(mid_price):
                logger.warning(f"[TWAP-Safe] 无效中价: {mid_price}")
                return None
                
            if not (self.min_slice_usd <= slice_usd <= self.max_slice_usd):
                logger.warning(f"[TWAP-Safe] 无效切片金额: {slice_usd}")
                return None
            
            # 安全除法计算
            if mid_price <= 0:
                logger.error(f"[TWAP-Safe] 中价为零或负数: {mid_price}")
                return None
                
            qty = slice_usd / mid_price
            
            # 数量合理性检查
            if qty < 1.0:
                logger.warning(f"[TWAP-Safe] 计算数量过小: {qty:.2f}")
                return None
                
            if qty > 1000000:  # 防止异常大数量
                logger.warning(f"[TWAP-Safe] 计算数量过大: {qty:.2f}")
                return None
            
            # 四舍五入到整数
            rounded_qty = round(qty)
            
            logger.debug(f"[TWAP-Safe] {direction}: ${slice_usd:.2f} / ${mid_price:.5f} = {rounded_qty}")
            
            return rounded_qty
            
        except ZeroDivisionError:
            logger.error(f"[TWAP-Safe] 除零错误: slice_usd={slice_usd}, mid_price={mid_price}")
            self.record_failure()
            return None
        except Exception as e:
            logger.error(f"[TWAP-Safe] 计算错误: {e}")
            self.record_failure()
            return None
    
    def record_failure(self):
        """记录TWAP失败"""
        self.failed_attempts.append(time.time())
    
    def validate_twap_execution(self, side: str, qty: float, price: float) -> bool:
        """验证TWAP执行参数"""
        # 基本参数检查
        if side not in ['BUY', 'SELL']:
            logger.error(f"[TWAP-Valid] 无效方向: {side}")
            return False
            
        if qty <= 0:
            logger.error(f"[TWAP-Valid] 无效数量: {qty}")
            return False
            
        if not self.is_valid_price(price):
            logger.error(f"[TWAP-Valid] 无效价格: {price}")
            return False
        
        # 金额合理性检查
        notional = qty * price
        if not (self.min_slice_usd <= notional <= self.max_slice_usd):
            logger.warning(f"[TWAP-Valid] 名义金额超范围: ${notional:.2f}")
            return False
            
        return True
    
    def get_market_readiness_score(self, market_data: Dict[str, Any]) -> float:
        """
        计算市场数据就绪评分 [0,1]
        
        Args:
            market_data: 包含bid, ask, mid等的市场数据
        """
        score = 0.0
        factors = []
        
        # 因子1：基础数据完整性 (40%)
        required_fields = ['bid', 'ask', 'mid']
        available_fields = sum(1 for field in required_fields 
                             if field in market_data and market_data[field] > 0)
        completeness = available_fields / len(required_fields)
        factors.append(('completeness', completeness * 0.4))
        
        # 因子2：价格稳定性 (30%)
        stability = 1.0 if self.is_price_stable() else 0.0
        factors.append(('stability', stability * 0.3))
        
        # 因子3：启动状态 (20%)
        startup_ready = 0.0 if self.is_startup_phase() else 1.0
        factors.append(('startup', startup_ready * 0.2))
        
        # 因子4：错误历史 (10%)
        error_penalty = min(1.0, len(self.failed_attempts) / self.max_retries)
        error_factor = (1.0 - error_penalty) * 0.1
        factors.append(('errors', error_factor))
        
        # 计算总分
        total_score = sum(weight for _, weight in factors)
        
        logger.debug(f"[TWAP-Ready] 市场就绪评分: {total_score:.2f} "
                    f"factors={dict(factors)}")
        
        return total_score
    
    def log_twap_status(self, market_data: Dict[str, Any]):
        """记录TWAP状态信息"""
        readiness = self.get_market_readiness_score(market_data)
        can_exec, reason = self.can_execute_twap(market_data.get('mid', 0))
        
        logger.info(f"[TWAP-Status] readiness={readiness:.2f} "
                   f"can_execute={can_exec} reason='{reason}' "
                   f"samples={len(self.price_history)} "
                   f"failures={len(self.failed_attempts)}")
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'startup_time': self.startup_time,
            'is_startup_phase': self.is_startup_phase(),
            'price_samples': len(self.price_history),
            'is_price_stable': self.is_price_stable(),
            'failed_attempts': len(self.failed_attempts),
            'last_failure': self.failed_attempts[-1] if self.failed_attempts else None
        }