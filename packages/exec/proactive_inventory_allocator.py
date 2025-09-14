#!/usr/bin/env python3
"""
Phase 7.4 PIA: 主动库存感知尺寸分配器
根据库存偏差主动调整订单尺寸，实现确定性收敛
"""

import logging
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class ProactiveInventoryAllocator:
    """主动库存感知尺寸分配器"""
    
    def __init__(self, 
                 alpha_base: float = 0.15,
                 k_factor: float = 2.0,
                 alpha_min: float = 0.10,
                 alpha_max: float = 0.35,
                 error_threshold: float = 0.05):
        """
        初始化PIA分配器
        
        Args:
            alpha_base: 基础激进度参数
            k_factor: 库存误差放大系数
            alpha_min: 最小激进度
            alpha_max: 最大激进度
            error_threshold: 库存误差触发阈值
        """
        self.alpha_base = alpha_base
        self.k_factor = k_factor
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.error_threshold = error_threshold
        
        # 收敛历史追踪
        self.convergence_history = []
        
        logger.info(f"🎯 [Phase7.4-PIA] 初始化主动库存感知分配器")
        logger.info(f"   参数: α_base={alpha_base}, k={k_factor}, α_range=[{alpha_min}, {alpha_max}]")
        logger.info(f"   阈值: error_threshold={error_threshold}")
    
    def calculate_inventory_error(self, doge_value: float, total_value: float) -> float:
        """
        计算库存误差
        
        Args:
            doge_value: DOGE资产价值
            total_value: 总资产价值
            
        Returns:
            库存误差 e = w_target - w_doge (目标50% - 当前DOGE权重)
        """
        if total_value <= 0:
            logger.warning(f"⚠️ [Phase7.4-PIA] 总资产价值异常: {total_value}")
            return 0.0
            
        w_doge = doge_value / total_value
        w_target = 0.50  # 50%目标权重
        
        error = w_target - w_doge
        
        logger.debug(f"📊 [Phase7.4-PIA] 库存误差计算:")
        logger.debug(f"   DOGE价值: {doge_value:.2f} USDT")
        logger.debug(f"   总价值: {total_value:.2f} USDT") 
        logger.debug(f"   DOGE权重: {w_doge:.4f} (目标: 0.5000)")
        logger.debug(f"   库存误差: {error:.4f}")
        
        return error
    
    def calculate_dynamic_alpha(self, error: float) -> float:
        """
        计算动态激进度参数
        
        Args:
            error: 库存误差
            
        Returns:
            动态alpha值 = clamp(alpha_base + k * |e|, alpha_min, alpha_max)
        """
        alpha = self.alpha_base + self.k_factor * abs(error)
        alpha = max(self.alpha_min, min(self.alpha_max, alpha))
        
        logger.debug(f"🔄 [Phase7.4-PIA] 动态α计算:")
        logger.debug(f"   |误差|: {abs(error):.4f}")
        logger.debug(f"   α = {self.alpha_base} + {self.k_factor} * {abs(error):.4f} = {alpha:.4f}")
        logger.debug(f"   限制后α: {alpha:.4f}")
        
        return alpha
        
    def calculate_proactive_size_adjustment(self, 
                                          error: float, 
                                          alpha: float, 
                                          base_size: float) -> Tuple[float, float]:
        """
        计算主动尺寸调整倍数
        
        Args:
            error: 库存误差
            alpha: 动态激进度
            base_size: 基础订单尺寸
            
        Returns:
            (buy_multiplier, sell_multiplier): 买单和卖单尺寸倍数
        """
        if abs(error) < self.error_threshold:
            # 在容忍范围内，保持平衡
            buy_multiplier = 1.0
            sell_multiplier = 1.0
            logger.debug(f"⚖️ [Phase7.4-PIA] 库存均衡状态，无需调整")
            
        elif error > self.error_threshold:
            # DOGE不足，需要买入更多DOGE
            buy_multiplier = 1.0 + alpha * abs(error)  # 买单加大
            sell_multiplier = 1.0 - alpha * abs(error) * 0.5  # 卖单减小
            
            logger.info(f"📈 [Phase7.4-PIA] DOGE不足调整:")
            logger.info(f"   误差: {error:.4f} > {self.error_threshold}")
            logger.info(f"   买单倍数: {buy_multiplier:.3f}x (加大)")
            logger.info(f"   卖单倍数: {sell_multiplier:.3f}x (减小)")
            
        else:  # error < -self.error_threshold
            # DOGE过多，需要卖出更多DOGE
            buy_multiplier = 1.0 - alpha * abs(error) * 0.5  # 买单减小
            sell_multiplier = 1.0 + alpha * abs(error)  # 卖单加大
            
            logger.info(f"📉 [Phase7.4-PIA] DOGE过多调整:")
            logger.info(f"   误差: {error:.4f} < {-self.error_threshold}")
            logger.info(f"   买单倍数: {buy_multiplier:.3f}x (减小)")
            logger.info(f"   卖单倍数: {sell_multiplier:.3f}x (加大)")
        
        # 确保倍数在合理范围内
        buy_multiplier = max(0.1, min(3.0, buy_multiplier))
        sell_multiplier = max(0.1, min(3.0, sell_multiplier))
        
        return buy_multiplier, sell_multiplier
    
    def apply_inventory_aware_sizing(self, 
                                   doge_value: float,
                                   total_value: float,
                                   base_buy_size: float,
                                   base_sell_size: float) -> Tuple[float, float]:
        """
        应用库存感知尺寸调整
        
        Args:
            doge_value: DOGE资产价值
            total_value: 总资产价值
            base_buy_size: 基础买单尺寸
            base_sell_size: 基础卖单尺寸
            
        Returns:
            (adjusted_buy_size, adjusted_sell_size): 调整后的订单尺寸
        """
        # 计算库存误差
        error = self.calculate_inventory_error(doge_value, total_value)
        
        # 计算动态激进度
        alpha = self.calculate_dynamic_alpha(error)
        
        # 计算尺寸调整倍数
        buy_multiplier, sell_multiplier = self.calculate_proactive_size_adjustment(
            error, alpha, base_buy_size
        )
        
        # 应用调整
        adjusted_buy_size = base_buy_size * buy_multiplier
        adjusted_sell_size = base_sell_size * sell_multiplier
        
        # 记录收敛历史
        self._record_convergence_step(error, alpha, buy_multiplier, sell_multiplier)
        
        logger.info(f"✅ [Phase7.4-PIA] 尺寸调整完成:")
        logger.info(f"   买单: {base_buy_size:.6f} → {adjusted_buy_size:.6f} DOGE ({buy_multiplier:.3f}x)")
        logger.info(f"   卖单: {base_sell_size:.6f} → {adjusted_sell_size:.6f} DOGE ({sell_multiplier:.3f}x)")
        
        return adjusted_buy_size, adjusted_sell_size
    
    def _record_convergence_step(self, error: float, alpha: float, 
                               buy_mult: float, sell_mult: float) -> None:
        """记录收敛步骤"""
        step = {
            'timestamp': time.time(),
            'datetime': datetime.now().strftime('%H:%M:%S'),
            'inventory_error': error,
            'dynamic_alpha': alpha,
            'buy_multiplier': buy_mult,
            'sell_multiplier': sell_mult
        }
        
        self.convergence_history.append(step)
        
        # 保持历史记录不超过100条
        if len(self.convergence_history) > 100:
            self.convergence_history.pop(0)
        
        logger.debug(f"📝 [Phase7.4-PIA] 记录收敛步骤 #{len(self.convergence_history)}")
    
    def validate_convergence_trajectory(self) -> bool:
        """
        验证收敛轨迹是否符合要求
        
        Returns:
            True: 收敛轨迹正常 (|e_new| < 0.8*|e_old|)
            False: 收敛失败
        """
        if len(self.convergence_history) < 2:
            return False
            
        # 检查最近3个步骤的收敛情况
        recent_steps = self.convergence_history[-3:] if len(self.convergence_history) >= 3 else self.convergence_history[-2:]
        
        convergence_ok = True
        for i in range(1, len(recent_steps)):
            e_old = abs(recent_steps[i-1]['inventory_error'])
            e_new = abs(recent_steps[i]['inventory_error'])
            
            # 验证收敛条件：|e_new| < 0.8*|e_old|
            if e_old > 0.001:  # 避免除零
                convergence_rate = e_new / e_old
                if convergence_rate >= 0.8:
                    convergence_ok = False
                    logger.warning(f"⚠️ [Phase7.4-PIA] 收敛验证失败:")
                    logger.warning(f"   步骤{i}: |e_old|={e_old:.4f} → |e_new|={e_new:.4f}")
                    logger.warning(f"   收敛率: {convergence_rate:.3f} >= 0.8 (要求<0.8)")
                else:
                    logger.info(f"✅ [Phase7.4-PIA] 收敛验证通过:")
                    logger.info(f"   步骤{i}: |e_old|={e_old:.4f} → |e_new|={e_new:.4f}")
                    logger.info(f"   收敛率: {convergence_rate:.3f} < 0.8 ✓")
        
        return convergence_ok
    
    def get_convergence_report(self) -> Dict:
        """获取收敛报告"""
        if not self.convergence_history:
            return {'status': 'no_data', 'steps': 0}
        
        latest = self.convergence_history[-1]
        convergence_ok = self.validate_convergence_trajectory()
        
        report = {
            'status': 'converging' if convergence_ok else 'diverging',
            'steps': len(self.convergence_history),
            'current_error': latest['inventory_error'],
            'current_alpha': latest['dynamic_alpha'],
            'convergence_valid': convergence_ok,
            'latest_step': latest
        }
        
        return report


def create_proactive_inventory_allocator(**kwargs) -> ProactiveInventoryAllocator:
    """创建PIA实例的工厂函数"""
    return ProactiveInventoryAllocator(**kwargs)


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 测试PIA组件
    pia = create_proactive_inventory_allocator()
    
    # 模拟库存不平衡场景
    test_scenarios = [
        {'doge_value': 400, 'total_value': 1000, 'desc': 'DOGE不足(40%)'},
        {'doge_value': 600, 'total_value': 1000, 'desc': 'DOGE过多(60%)'},
        {'doge_value': 500, 'total_value': 1000, 'desc': 'DOGE平衡(50%)'}
    ]
    
    for scenario in test_scenarios:
        print(f"\n{'='*60}")
        print(f"测试场景: {scenario['desc']}")
        print(f"{'='*60}")
        
        buy_size, sell_size = pia.apply_inventory_aware_sizing(
            doge_value=scenario['doge_value'],
            total_value=scenario['total_value'],
            base_buy_size=100.0,
            base_sell_size=100.0
        )
        
        report = pia.get_convergence_report()
        print(f"收敛报告: {report}")