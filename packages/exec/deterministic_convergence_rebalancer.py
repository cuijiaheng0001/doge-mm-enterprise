#!/usr/bin/env python3
"""
Phase 7.4 DCR: 确定性收敛再平衡器  
保证每轮库存偏差递减至少20%，3轮内达成50/50±5%
"""

import logging
import time
import json
from datetime import datetime
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)

class DeterministicConvergenceRebalancer:
    """确定性收敛再平衡器"""
    
    def __init__(self,
                 target_weight: float = 0.50,
                 tolerance: float = 0.05,
                 convergence_rate: float = 0.8,
                 max_rounds: int = 3,
                 urgency_threshold: float = 0.15):
        """
        初始化DCR再平衡器
        
        Args:
            target_weight: 目标DOGE权重 (50%)
            tolerance: 容忍误差范围 (±5%)
            convergence_rate: 每轮收敛率阈值 (<0.8 = 20%递减)
            max_rounds: 最大收敛轮数
            urgency_threshold: 高紧急度阈值 (>15%偏差)
        """
        self.target_weight = target_weight
        self.tolerance = tolerance
        self.convergence_rate = convergence_rate
        self.max_rounds = max_rounds
        self.urgency_threshold = urgency_threshold
        
        # 再平衡历史记录
        self.rebalance_history: List[Dict] = []
        
        logger.info(f"🎯 [Phase7.4-DCR] 初始化确定性收敛再平衡器")
        logger.info(f"   目标权重: {target_weight:.1%} ± {tolerance:.1%}")
        logger.info(f"   收敛要求: 每轮递减>{(1-convergence_rate)*100:.0f}%，{max_rounds}轮内完成")
        logger.info(f"   紧急阈值: >{urgency_threshold:.1%}")
    
    def calculate_inventory_error(self, doge_value: float, total_value: float) -> float:
        """
        计算库存误差
        
        Args:
            doge_value: DOGE资产价值 
            total_value: 总资产价值
            
        Returns:
            库存误差 e = w_target - w_current
        """
        if total_value <= 0:
            logger.warning(f"⚠️ [Phase7.4-DCR] 总资产价值异常: {total_value}")
            return 0.0
            
        w_current = doge_value / total_value
        error = self.target_weight - w_current
        
        logger.debug(f"📊 [Phase7.4-DCR] 库存误差计算:")
        logger.debug(f"   DOGE价值: {doge_value:.2f} USDT")
        logger.debug(f"   总价值: {total_value:.2f} USDT")
        logger.debug(f"   当前权重: {w_current:.4f}")
        logger.debug(f"   库存误差: {error:.4f}")
        
        return error
    
    def validate_convergence_trajectory(self, e_old: float, e_new: float) -> bool:
        """
        验证收敛轨迹：|e_new| < convergence_rate * |e_old|
        
        Args:
            e_old: 上轮库存误差
            e_new: 本轮库存误差
            
        Returns:
            True: 收敛正常, False: 收敛失败
        """
        if abs(e_old) < 0.001:  # 避免除零
            return True
            
        convergence_actual = abs(e_new) / abs(e_old)
        converged = convergence_actual < self.convergence_rate
        
        if converged:
            logger.info(f"✅ [Phase7.4-DCR] 收敛验证通过:")
            logger.info(f"   |e_old|={abs(e_old):.4f} → |e_new|={abs(e_new):.4f}")
            logger.info(f"   收敛率: {convergence_actual:.3f} < {self.convergence_rate} ✓")
        else:
            logger.warning(f"❌ [Phase7.4-DCR] 收敛验证失败:")
            logger.warning(f"   |e_old|={abs(e_old):.4f} → |e_new|={abs(e_new):.4f}")
            logger.warning(f"   收敛率: {convergence_actual:.3f} >= {self.convergence_rate} (要求<{self.convergence_rate})")
            
        return converged
        
    def generate_deterministic_rebalance_plan(self, 
                                            error: float,
                                            total_usdt: float, 
                                            doge_price: float) -> Optional[Dict]:
        """
        生成确定性再平衡方案
        
        Args:
            error: 库存误差
            total_usdt: 总资产价值(USDT)
            doge_price: DOGE价格
            
        Returns:
            再平衡计划字典或None
        """
        if abs(error) < self.tolerance:
            logger.debug(f"⚖️ [Phase7.4-DCR] 误差在容忍范围内: {error:.4f} < {self.tolerance}")
            return None
            
        # 计算再平衡价值和数量
        target_doge_value = total_usdt * self.target_weight
        current_doge_value = total_usdt * (self.target_weight - error)
        rebalance_value = target_doge_value - current_doge_value
        rebalance_quantity = abs(rebalance_value) / doge_price
        
        # 判断紧急度
        urgency = 'HIGH' if abs(error) > self.urgency_threshold else 'NORMAL'
        
        if error > self.tolerance:  # DOGE不足，需要买入
            plan = {
                'action': 'BUY_DOGE',
                'side': 'BUY', 
                'value': rebalance_value,
                'quantity': rebalance_quantity,
                'urgency': urgency,
                'price_reference': doge_price,
                'error_before': error,
                'expected_error_after': 0.0,  # 理论上应该达到平衡
                'timestamp': time.time(),
                'datetime': datetime.now().strftime('%H:%M:%S'),
                'convergence_round': len(self.rebalance_history) + 1
            }
            
            logger.info(f"📈 [Phase7.4-DCR] 生成买入再平衡计划:")
            logger.info(f"   DOGE不足: {error:.4f} > {self.tolerance}")
            logger.info(f"   买入价值: {rebalance_value:.2f} USDT")
            logger.info(f"   买入数量: {rebalance_quantity:.6f} DOGE")
            logger.info(f"   紧急度: {urgency}")
            
        else:  # DOGE过多，需要卖出
            plan = {
                'action': 'SELL_DOGE',
                'side': 'SELL',
                'value': abs(rebalance_value),
                'quantity': rebalance_quantity, 
                'urgency': urgency,
                'price_reference': doge_price,
                'error_before': error,
                'expected_error_after': 0.0,
                'timestamp': time.time(),
                'datetime': datetime.now().strftime('%H:%M:%S'),
                'convergence_round': len(self.rebalance_history) + 1
            }
            
            logger.info(f"📉 [Phase7.4-DCR] 生成卖出再平衡计划:")
            logger.info(f"   DOGE过多: {error:.4f} < {-self.tolerance}")
            logger.info(f"   卖出价值: {abs(rebalance_value):.2f} USDT")
            logger.info(f"   卖出数量: {rebalance_quantity:.6f} DOGE")
            logger.info(f"   紧急度: {urgency}")
        
        return plan
        
    def track_convergence_progress(self, plan: Dict, actual_error_after: float) -> bool:
        """
        追踪收敛进度
        
        Args:
            plan: 已执行的再平衡计划
            actual_error_after: 执行后的实际误差
            
        Returns:
            True: 收敛进度正常, False: 收敛失败
        """
        # 更新计划的实际结果
        plan_with_result = plan.copy()
        plan_with_result.update({
            'actual_error_after': actual_error_after,
            'execution_timestamp': time.time(),
            'execution_datetime': datetime.now().strftime('%H:%M:%S')
        })
        
        # 验证收敛轨迹
        convergence_ok = True
        if self.rebalance_history:
            last_plan = self.rebalance_history[-1]
            e_old = abs(last_plan['error_before'])
            e_new = abs(actual_error_after)
            
            convergence_ok = self.validate_convergence_trajectory(e_old, e_new)
        
        # 添加到历史记录
        plan_with_result['convergence_validated'] = convergence_ok
        self.rebalance_history.append(plan_with_result)
        
        # 限制历史记录长度 
        if len(self.rebalance_history) > 50:
            self.rebalance_history.pop(0)
            
        logger.info(f"📝 [Phase7.4-DCR] 记录再平衡轮次 #{len(self.rebalance_history)}")
        logger.info(f"   执行前误差: {plan['error_before']:.4f}")
        logger.info(f"   执行后误差: {actual_error_after:.4f}")
        logger.info(f"   收敛验证: {'✅通过' if convergence_ok else '❌失败'}")
        
        return convergence_ok
        
    def check_convergence_completion(self) -> Tuple[bool, Dict]:
        """
        检查收敛是否完成
        
        Returns:
            (是否完成, 完成报告)
        """
        if not self.rebalance_history:
            return False, {'status': 'no_data', 'reason': '没有再平衡记录'}
            
        latest = self.rebalance_history[-1]
        latest_error = abs(latest['actual_error_after'])
        
        # 检查是否在容忍范围内
        within_tolerance = latest_error < self.tolerance
        
        # 检查收敛轮数
        total_rounds = len(self.rebalance_history)
        within_max_rounds = total_rounds <= self.max_rounds
        
        # 检查最近几轮的收敛轨迹
        convergence_trajectory_ok = True
        failed_convergence_rounds = 0
        
        for plan in self.rebalance_history[-3:]:  # 检查最近3轮
            if not plan.get('convergence_validated', True):
                failed_convergence_rounds += 1
                convergence_trajectory_ok = False
        
        # 生成完成报告
        if within_tolerance and convergence_trajectory_ok:
            status = 'COMPLETED'
            reason = f'已达成目标: 误差{latest_error:.4f} < {self.tolerance}, {total_rounds}轮内完成'
        elif not within_tolerance and total_rounds >= self.max_rounds:
            status = 'FAILED_MAX_ROUNDS'
            reason = f'超过最大轮数: {total_rounds} >= {self.max_rounds}, 误差仍为{latest_error:.4f}'
        elif not convergence_trajectory_ok:
            status = 'FAILED_CONVERGENCE'
            reason = f'收敛轨迹失败: {failed_convergence_rounds}轮未达成收敛要求'
        else:
            status = 'IN_PROGRESS' 
            reason = f'进行中: {total_rounds}轮, 当前误差{latest_error:.4f}'
        
        completion_report = {
            'status': status,
            'reason': reason,
            'total_rounds': total_rounds,
            'latest_error': latest_error,
            'within_tolerance': within_tolerance,
            'within_max_rounds': within_max_rounds,
            'convergence_trajectory_ok': convergence_trajectory_ok,
            'failed_convergence_rounds': failed_convergence_rounds,
            'success_rate': (total_rounds - failed_convergence_rounds) / max(1, total_rounds)
        }
        
        is_completed = status in ['COMPLETED']
        
        if is_completed:
            logger.info(f"🎉 [Phase7.4-DCR] 收敛完成!")
            logger.info(f"   {reason}")
            logger.info(f"   成功率: {completion_report['success_rate']:.1%}")
        elif status.startswith('FAILED'):
            logger.warning(f"❌ [Phase7.4-DCR] 收敛失败!")
            logger.warning(f"   {reason}")
            logger.warning(f"   成功率: {completion_report['success_rate']:.1%}")
        else:
            logger.info(f"🔄 [Phase7.4-DCR] 收敛进行中...")
            logger.info(f"   {reason}")
        
        return is_completed, completion_report
        
    def get_rebalance_summary(self) -> Dict:
        """获取再平衡摘要"""
        if not self.rebalance_history:
            return {'status': 'no_data', 'rounds': 0}
            
        completed, report = self.check_convergence_completion()
        latest = self.rebalance_history[-1] 
        
        # 统计各类再平衡
        buy_plans = [p for p in self.rebalance_history if p['action'] == 'BUY_DOGE']
        sell_plans = [p for p in self.rebalance_history if p['action'] == 'SELL_DOGE']
        high_urgency = [p for p in self.rebalance_history if p['urgency'] == 'HIGH']
        
        summary = {
            'status': report['status'],
            'completed': completed,
            'total_rounds': len(self.rebalance_history),
            'latest_error': latest['actual_error_after'],
            'latest_action': latest['action'],
            'buy_plans': len(buy_plans),
            'sell_plans': len(sell_plans), 
            'high_urgency_plans': len(high_urgency),
            'success_rate': report['success_rate'],
            'completion_report': report
        }
        
        return summary


def create_deterministic_convergence_rebalancer(**kwargs) -> DeterministicConvergenceRebalancer:
    """创建DCR实例的工厂函数"""
    return DeterministicConvergenceRebalancer(**kwargs)


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 测试DCR组件
    dcr = create_deterministic_convergence_rebalancer()
    
    # 模拟收敛场景
    test_scenarios = [
        {'doge_val': 300, 'total_val': 1000, 'price': 0.26, 'desc': '严重DOGE不足(30%)'},
        {'doge_val': 450, 'total_val': 1000, 'price': 0.26, 'desc': '中等DOGE不足(45%)'},  
        {'doge_val': 520, 'total_val': 1000, 'price': 0.26, 'desc': '轻微DOGE平衡(52%)'}
    ]
    
    for i, scenario in enumerate(test_scenarios):
        print(f"\n{'='*70}")
        print(f"测试场景 {i+1}: {scenario['desc']}")
        print(f"{'='*70}")
        
        # 计算误差
        error = dcr.calculate_inventory_error(scenario['doge_val'], scenario['total_val'])
        
        # 生成再平衡计划
        plan = dcr.generate_deterministic_rebalance_plan(
            error=error,
            total_usdt=scenario['total_val'],
            doge_price=scenario['price']
        )
        
        if plan:
            print(f"再平衡计划: {plan['action']}")
            print(f"数量: {plan['quantity']:.2f} DOGE")
            print(f"价值: {plan['value']:.2f} USDT")
            print(f"紧急度: {plan['urgency']}")
            
            # 模拟执行结果（假设执行后误差减半）
            simulated_error_after = error * 0.5
            convergence_ok = dcr.track_convergence_progress(plan, simulated_error_after)
            
            print(f"收敛验证: {'✅' if convergence_ok else '❌'}")
        else:
            print("无需再平衡 - 误差在容忍范围内")
            
    # 检查最终收敛状态
    completed, report = dcr.check_convergence_completion()
    print(f"\n{'='*70}")
    print(f"最终收敛报告:")
    print(f"完成状态: {report['status']}")
    print(f"总轮数: {report['total_rounds']}")
    print(f"成功率: {report['success_rate']:.1%}")
    print(f"原因: {report['reason']}")
    
    summary = dcr.get_rebalance_summary()
    print(f"\n再平衡摘要: {json.dumps(summary, indent=2, ensure_ascii=False)}")