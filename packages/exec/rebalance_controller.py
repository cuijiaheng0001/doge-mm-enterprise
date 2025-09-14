"""
Phase 9 B Fix: 分级再平衡通道控制器 - 对标顶级做市商
四阶段再平衡：关闭→Maker移动→Maker加强→极小Taker-POV
"""
import os
import time
import math
import logging
from typing import Dict, Any, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class RebalanceStage(Enum):
    """再平衡阶段 - Phase4增加紧急模式"""
    CLOSED = 0      # 关闭：|err| ≤ band_soft
    MAKER_MOVE = 1  # Maker移动：偏差方向内移、缩TTL、加size  
    MAKER_INSIDE = 2 # Maker加强：SKEW_MAX提升、贴边抢队列
    TAKER_POV = 3   # 极小Taker：POV限制下的极小预算Taker
    EMERGENCY = 4   # Phase4紧急模式：极端失衡，激进再平衡


class RebalanceController:
    """分级再平衡通道控制器 - Phase 9 B Fix"""
    
    def __init__(self):
        # 阈值配置（可配置）- Phase4紧急优化: 放宽阈值
        self.band_soft = float(os.getenv('RB_BAND_SOFT', '0.08'))      # Phase4: 10%→8% 更敏感
        self.band_hard = float(os.getenv('RB_BAND_HARD', '0.15'))      # Phase4: 20%→15% 更敏感
        self.band_emergency = float(os.getenv('RB_BAND_EMERGENCY', '0.25'))  # Phase4: 新增紧急阈值25%
        
        # 时间持续要求 - Phase4紧急优化: 缩短等待时间
        self.persist_stage2 = float(os.getenv('RB_PERSIST_STAGE2', '90'))   # Phase4: 180s→90s 更快响应
        self.persist_stage3 = float(os.getenv('RB_PERSIST_STAGE3', '180'))  # Phase4: 300s→180s 更快响应
        self.persist_emergency = float(os.getenv('RB_PERSIST_EMERGENCY', '60')) # Phase4: 新增紧急模式60s
        
        # POV配置
        self.pov_rate_min = float(os.getenv('RB_POV_RATE_MIN', '0.01'))     # 1%市场量
        self.pov_rate_max = float(os.getenv('RB_POV_RATE_MAX', '0.03'))     # 3%市场量
        self.pov_notional_max = float(os.getenv('RB_POV_NOTIONAL_MAX', '20')) # 20 USD/min
        
        # 状态跟踪
        self.current_stage = RebalanceStage.CLOSED
        self.current_direction = None  # 'BUY' or 'SELL'
        self.stage_start_time = time.time()
        self.persist_start_time = None
        self.last_err = 0.0
        self.last_log_time = 0
        
        # Phase 9 B Fix: 反向护仓窗配置
        self.guard_config = {
            'widen_bps_min': float(os.getenv('GUARD_WIDEN_BPS_MIN', '4')),  # 4bps
            'widen_bps_max': float(os.getenv('GUARD_WIDEN_BPS_MAX', '8')),  # 8bps
            'slot_cap_factor': float(os.getenv('GUARD_SLOT_CAP_FACTOR', '0.6')),  # 60%
            'ttl_factor_min': float(os.getenv('GUARD_TTL_FACTOR_MIN', '0.7')),    # 70%
            'ttl_factor_max': float(os.getenv('GUARD_TTL_FACTOR_MAX', '0.8')),    # 80%
            'stable_duration': float(os.getenv('GUARD_STABLE_DURATION', '90')),   # 90秒
        }
        
        # 护仓窗状态跟踪
        self.guard_active = False
        self.guard_direction = None      # 再平衡方向 ('BUY'/'SELL')  
        self.opposite_direction = None   # 反向护仓方向 ('SELL'/'BUY')
        self.stable_start_time = None    # 开始稳定的时间
        
        # 统计
        self.stats = {
            'stage_transitions': 0,
            'taker_executions': 0,
            'total_pov_notional': 0.0,
            'guard_activations': 0,
            'guard_total_duration': 0.0
        }
        
        logger.info("[RB Controller] Phase4分级再平衡控制器初始化完成: "
                   f"band_soft=±{self.band_soft:.2f}, band_hard=±{self.band_hard:.2f}, "
                   f"band_emergency=±{self.band_emergency:.2f} "
                   f"persist_times=[{self.persist_stage2:.0f}s,{self.persist_stage3:.0f}s,{self.persist_emergency:.0f}s]")
    
    def evaluate_stage(self, metrics: Dict[str, float], awg_healthy: bool) -> Tuple[RebalanceStage, Optional[str]]:
        """评估当前应该处于的再平衡阶段"""
        err = metrics['err']
        tox = metrics.get('tox', 0.0)
        err_abs = abs(err)
        
        now = time.time()
        direction = 'SELL' if err > 0 else 'BUY' if err < 0 else None
        
        # 检查方向是否变化（重置持续时间）
        if direction != self.current_direction:
            self.persist_start_time = now
            self.current_direction = direction
        
        # 计算持续时间
        persist_time = now - (self.persist_start_time or now)
        
        # Stage 0: 关闭条件
        if err_abs <= self.band_soft:
            return RebalanceStage.CLOSED, None
        
        # Stage 1: Maker移动（基础条件）
        if err_abs > self.band_soft:
            target_stage = RebalanceStage.MAKER_MOVE
            
            # Stage 2: Maker加强（持续时间 + AWG健康）
            if persist_time >= self.persist_stage2 and awg_healthy:
                target_stage = RebalanceStage.MAKER_INSIDE
                
                # Stage 3: 极小Taker-POV（硬带宽 + 持续时间 + 低毒性）
                if (err_abs > self.band_hard and 
                    persist_time >= self.persist_stage3 and 
                    tox < 0.5):
                    target_stage = RebalanceStage.TAKER_POV
                    
                    # Phase4新增Stage 4: 紧急模式（极端失衡）
                    if (err_abs > self.band_emergency and 
                        persist_time >= self.persist_emergency):
                        target_stage = RebalanceStage.EMERGENCY
                        logger.warning(f"[RB-Phase4] 触发紧急平衡模式: err={err:+.3f} "
                                     f"(>{self.band_emergency:.2f}) persist={persist_time:.0f}s")
            
            return target_stage, direction
    
    def update_with_guard_check(self, metrics: Dict[str, float], awg_healthy: bool) -> Tuple[RebalanceStage, Optional[str]]:
        """Phase 9 B Fix: 综合更新（包含护仓窗检查）"""
        # 评估并更新阶段
        target_stage, direction = self.evaluate_stage(metrics, awg_healthy)
        self.update_stage(target_stage, direction)
        
        # 更新护仓窗状态
        self._update_guard_window(metrics)
        
        return target_stage, direction
    
    def update_stage(self, new_stage: RebalanceStage, direction: Optional[str]):
        """更新再平衡阶段"""
        if new_stage != self.current_stage:
            old_stage = self.current_stage
            self.current_stage = new_stage
            self.current_direction = direction
            self.stage_start_time = time.time()
            self.stats['stage_transitions'] += 1
            
            logger.info(f"[RB] 阶段转换: {old_stage.name} → {new_stage.name} "
                       f"direction={direction or 'NONE'}")
    
    def _update_guard_window(self, metrics: Dict[str, float]):
        """Phase 9 B Fix: 更新反向护仓窗状态"""
        err_abs = abs(metrics['err'])
        now = time.time()
        
        # 检查是否应该激活护仓窗
        should_activate = (self.current_stage != RebalanceStage.CLOSED and 
                          self.current_direction is not None)
        
        if should_activate and not self.guard_active:
            # 激活护仓窗
            self.guard_active = True
            self.guard_direction = self.current_direction
            self.opposite_direction = 'SELL' if self.current_direction == 'BUY' else 'BUY'
            self.stable_start_time = None
            self.stats['guard_activations'] += 1
            
            logger.info(f"[Guard] 激活反向护仓窗: RB_direction={self.guard_direction} "
                       f"opposite={self.opposite_direction}")
        
        elif self.guard_active:
            # 检查是否应该停用护仓窗
            if err_abs <= self.band_soft:
                # 进入稳定区间
                if self.stable_start_time is None:
                    self.stable_start_time = now
                    
                # 检查稳定持续时间
                stable_duration = now - self.stable_start_time
                if stable_duration >= self.guard_config['stable_duration']:
                    # 停用护仓窗
                    guard_duration = now - self.stage_start_time
                    self.stats['guard_total_duration'] += guard_duration
                    
                    logger.info(f"[Guard] 停用反向护仓窗: 稳定{stable_duration:.0f}s "
                               f"总持续{guard_duration:.0f}s")
                    
                    self.guard_active = False
                    self.guard_direction = None
                    self.opposite_direction = None
                    self.stable_start_time = None
            else:
                # 重新进入不稳定区间，重置稳定计时
                self.stable_start_time = None
            
            # 如果再平衡完全关闭，强制停用护仓窗
            if self.current_stage == RebalanceStage.CLOSED:
                self.guard_active = False
                self.guard_direction = None
                self.opposite_direction = None
                self.stable_start_time = None
    
    def get_guard_adjustments(self, side: str) -> Dict[str, float]:
        """Phase 9 B Fix: 获取护仓窗调整参数"""
        if not self.guard_active or side != self.opposite_direction.lower():
            return {'widen_bps': 0, 'slot_cap_factor': 1.0, 'ttl_factor': 1.0}
        
        # 根据再平衡阶段确定护仓强度
        intensity = 0.5  # 基础强度
        if self.current_stage == RebalanceStage.MAKER_INSIDE:
            intensity = 0.7
        elif self.current_stage == RebalanceStage.TAKER_POV:
            intensity = 1.0
        
        # 计算调整参数
        widen_bps = (self.guard_config['widen_bps_min'] + 
                    (self.guard_config['widen_bps_max'] - self.guard_config['widen_bps_min']) * intensity)
        
        ttl_factor = (self.guard_config['ttl_factor_max'] - 
                     (self.guard_config['ttl_factor_max'] - self.guard_config['ttl_factor_min']) * intensity)
        
        return {
            'widen_bps': widen_bps,
            'slot_cap_factor': self.guard_config['slot_cap_factor'],
            'ttl_factor': ttl_factor
        }
    
    def get_stage_adjustments(self, metrics: Dict[str, float]) -> Dict[str, Any]:
        """获取当前阶段的调整参数"""
        if self.current_stage == RebalanceStage.CLOSED:
            return {
                'skew_max_adjustment': 0,
                'tick_inside': 0,
                'ttl_bias': 1.0,
                'size_bias': 1.0,
                'enable_taker': False,
                'pov_rate': 0.0
            }
        
        elif self.current_stage == RebalanceStage.MAKER_MOVE:
            return {
                'skew_max_adjustment': 0,      # 保持基础SKEW_MAX
                'tick_inside': 1,              # 内移1tick
                'ttl_bias': 0.8,              # 缩短TTL到80%
                'size_bias': 1.2,             # 增加size到120%
                'enable_taker': False,
                'pov_rate': 0.0
            }
        
        elif self.current_stage == RebalanceStage.MAKER_INSIDE:
            return {
                'skew_max_adjustment': 40,     # SKEW_MAX提升至40-60bps
                'tick_inside': 2,              # 内移2tick，更激进
                'ttl_bias': 0.6,              # 大幅缩短TTL到60%
                'size_bias': 1.5,             # 大幅增加size到150%
                'enable_taker': False,
                'pov_rate': 0.0
            }
        
        elif self.current_stage == RebalanceStage.TAKER_POV:
            # 计算POV比例（基于市场量的1-3%，但不超过20 USD/min）
            pov_rate = min(self.pov_rate_max, 
                          self.pov_notional_max / max(1000, metrics.get('market_vol_usd', 1000)))
            
            return {
                'skew_max_adjustment': 60,     # 最大SKEW_MAX
                'tick_inside': 2,              # 继续内移
                'ttl_bias': 0.5,              # 最短TTL
                'size_bias': 1.8,             # 最大size
                'enable_taker': True,          # 启用极小Taker
                'pov_rate': pov_rate
            }
        
        elif self.current_stage == RebalanceStage.EMERGENCY:
            # Phase4紧急模式: 最激进的再平衡配置
            emergency_pov_rate = min(0.05, self.pov_notional_max * 2 / max(1000, metrics.get('market_vol_usd', 1000)))
            
            return {
                'skew_max_adjustment': 100,    # Phase4: 极大SKEW_MAX (120-140bps)
                'tick_inside': 3,              # Phase4: 最激进内移3tick
                'ttl_bias': 0.3,              # Phase4: 极短TTL (30%)
                'size_bias': 2.5,             # Phase4: 极大订单size (250%)
                'enable_taker': True,          # 启用Taker
                'pov_rate': emergency_pov_rate, # Phase4: 增加POV到5%或双倍notional
                'emergency_mode': True,        # Phase4: 紧急模式标记
                'bypass_twap_constraints': True, # Phase4: 绕过正常TWAP限制
                'bypass_awg_soft_limits': True   # Phase4: 绕过AWG软限制
            }
        
        return {}
    
    def can_execute_taker(self, notional_usd: float) -> bool:
        """检查是否可以执行Taker订单 - Phase4支持紧急模式"""
        if self.current_stage not in [RebalanceStage.TAKER_POV, RebalanceStage.EMERGENCY]:
            return False
            
        # 检查POV限制
        now = time.time()
        
        # 清理1分钟前的记录（这里应该与AWG Pro的POV检查集成）
        recent_notional = 0.0  # 这个应该从AWG Pro获取
        
        return recent_notional + notional_usd <= self.pov_notional_max
    
    def log_rebalance_status(self, metrics: Dict[str, float], awg_healthy: bool):
        """输出再平衡状态线（符合验收标准）"""
        now = time.time()
        if now - self.last_log_time < 10:  # 每10秒输出一次
            return
        self.last_log_time = now
        
        if self.current_stage == RebalanceStage.CLOSED:
            return  # 关闭状态不输出
        
        err = metrics['err']
        persist_time = now - (self.persist_start_time or now)
        adjustments = self.get_stage_adjustments(metrics)
        
        # 符合验收标准的日志格式
        logger.info(
            f"[RB] stage={self.current_stage.value} dir={self.current_direction} "
            f"err={err:+.3f} persist={persist_time:.0f}s "
            f"skew_max={20 + adjustments['skew_max_adjustment']}bps "
            f"tick_inside={adjustments['tick_inside']} "
            f"ttl_bias={adjustments['ttl_bias']:.1f}x "
            f"size_bias={adjustments['size_bias']:.1f}x "
            f"awg_healthy={awg_healthy} "
            f"reason={'hard_band' if abs(err) > self.band_hard else 'soft_band'}|persist"
            + (f"|tox={metrics.get('tox', 0):.2f}" if self.current_stage == RebalanceStage.TAKER_POV else "")
        )
        
        # Phase 9 B Fix: 输出Guard状态线（符合验收标准）
        if self.guard_active and self.opposite_direction:
            guard_adj = self.get_guard_adjustments(self.opposite_direction.lower())
            slot_cap_before = 6  # 假设基础槽位为6
            slot_cap_after = int(slot_cap_before * guard_adj['slot_cap_factor'])
            
            logger.info(
                f"[Guard] opp_side widen={guard_adj['widen_bps']:+.0f}bps "
                f"slot_cap: {slot_cap_before}->{slot_cap_after} "
                f"ttl: {1.0:.1f}->{guard_adj['ttl_factor']:.1f} "
                f"reason=RB_active dir={self.guard_direction}"
            )
    
    def should_trigger_twap_rebalance(self, metrics: Dict[str, float]) -> Tuple[bool, Optional[str], float]:
        """判断是否应该触发TWAP再平衡"""
        if self.current_stage == RebalanceStage.CLOSED:
            return False, None, 0.0
        
        err = abs(metrics['err'])
        direction = self.current_direction
        
        # 根据阶段确定再平衡强度
        if self.current_stage == RebalanceStage.MAKER_MOVE:
            intensity = min(err / self.band_soft, 1.0)  # [0,1]
        elif self.current_stage == RebalanceStage.MAKER_INSIDE:
            intensity = min(err / self.band_soft, 1.5)  # [0,1.5]
        elif self.current_stage == RebalanceStage.TAKER_POV:
            intensity = min(err / self.band_hard, 2.0)  # [0,2.0]
        elif self.current_stage == RebalanceStage.EMERGENCY:
            # Phase4紧急模式: 最高强度再平衡
            intensity = min(err / self.band_emergency, 3.0)  # [0,3.0] 最高强度
        else:
            intensity = 0.0
        
        return True, direction, intensity