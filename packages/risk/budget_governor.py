# -*- coding: utf-8 -*-
"""
BudgetGovernor - 世界级做市商控制平面核心

核心目标：
  1) CQM：基于目标并发 N* 与平均TTL τ，反推出 10s 预算，使稳态并发≈N*
  2) Usage Governor：把 API usage 锁在目标带（默认 10%），偏差→缓调预算与TTL
  3) KPI 驱动：最小化 messages / Δonbook_USD，抑制无效 churn

输入（每 tick 更新一次）：
  - n_target: 目标在册并发(总笔数，含L0/L1/L2)
  - ttl_l0/l1/l2: 各层TTL秒，及各层目标并发 n_l0/n_l1/n_l2（用于加权算τ）
  - msg_10s: {'fill':x, 'reprice':y, 'cancel':z} 最近10s消息数
  - usage_pct: 最近10s权重使用率（0-100）
  - onbook_usd_now: 当前在册USD（买卖合计）
  - onbook_usd_10s_ago: 10秒前在册USD（用于计算Δonbook）
  
输出：
  - budgets: {fill_10s, reprice_10s, cancel_10s, burst_fill, burst_reprice, burst_cancel, ttl_scale}

安全边界：
  - usage_target=10%，安全上限=15%；PID样式平滑，单次调整≤±20%
  - 预算上下限：fill∈[2, 20]、reprice∈[2, 20]、cancel∈[20, 80]（可改）
  - 突发=预算（便于消除空窗）；WIP/在途限制建议在连接器层实施
"""

import time
import math
import logging
from collections import deque

logger = logging.getLogger(__name__)

def _ema(prev, x, alpha=0.2):
    """指数移动平均"""
    return (1 - alpha) * prev + alpha * x if prev is not None else x

class BudgetGovernor:
    """
    世界级做市商控制平面核心：CQM + Usage闭环 + KPI驱动
    """
    
    def __init__(self,
                 usage_target_pct=10.0,
                 usage_safe_pct=15.0,
                 min_budgets=(2, 2, 20),
                 max_budgets=(20, 20, 80)):
        """
        初始化预算总督
        
        Args:
            usage_target_pct: 目标usage百分比
            usage_safe_pct: 安全上限usage百分比
            min_budgets: (fill_min, reprice_min, cancel_min)
            max_budgets: (fill_max, reprice_max, cancel_max)
        """
        self.usage_target = usage_target_pct
        self.usage_safe = usage_safe_pct
        self.min_fill, self.min_reprice, self.min_cancel = min_budgets
        self.max_fill, self.max_reprice, self.max_cancel = max_budgets

        self.last_onbook = None
        self.last_apply_ts = 0.0
        self.err_int = 0.0     # 积分项（秒）
        self.ema_mpd = None    # KPI: messages per Δonbook_USD 的 EMA
        self.ema_usage = None  # usage EMA
        self.prev = None       # 上次预算（用于限幅）
        self.history = deque(maxlen=60)  # 保留60s历史用于分析
        
        logger.info(f"[GOV] BudgetGovernor初始化: target={usage_target_pct}%, safe={usage_safe_pct}%, "
                   f"budgets=[{min_budgets[0]}-{max_budgets[0]}, {min_budgets[1]}-{max_budgets[1]}, {min_budgets[2]}-{max_budgets[2]}]")

    @staticmethod
    def _clamp(x, lo, hi):
        """限制在[lo, hi]范围内"""
        return max(lo, min(hi, x))

    def _weighted_tau(self, ttl_l0, ttl_l1, ttl_l2, n_l0, n_l1, n_l2):
        """
        计算加权平均TTL
        τ = (ttl_l0*n_l0 + ttl_l1*n_l1 + ttl_l2*n_l2) / (n_l0+n_l1+n_l2)
        """
        n_total = max(1, n_l0 + n_l1 + n_l2)
        weighted_tau = (ttl_l0 * n_l0 + ttl_l1 * n_l1 + ttl_l2 * n_l2) / n_total
        return weighted_tau

    def _cqm_budgets(self, n_target, tau_sec, msg_10s):
        """
        CQM核心推导：
          λ_need = N* / τ (每秒)
          10s 需要 ≈ 10 * λ_need 份 "新+重价+撤单"
          p_reprice ≈ reprice_per_new，p_cancel ≈ cancel_per_new （用最近10s比率估）
        """
        new_10s = msg_10s.get('fill', 0)
        rep_10s = msg_10s.get('reprice', 0)
        can_10s = msg_10s.get('cancel', 0)

        # 观测到的结构比；没有新单时用保守先验
        if new_10s <= 2:
            p_rep = 1.0  # 初期假设 1:1
            p_can = 2.0  # TTL 较多时 cancel 通常更高
        else:
            p_rep = rep_10s / max(1.0, new_10s)
            p_can = can_10s / max(1.0, new_10s)
            # 裁剪到合理范围
            p_rep = self._clamp(p_rep, 0.3, 2.0)
            p_can = self._clamp(p_can, 0.5, 4.0)

        tau_sec = max(3.0, float(tau_sec))  # 给底
        lam_need = n_target / tau_sec
        base_new_10s = 10.0 * lam_need

        fill_10s   = math.ceil(base_new_10s)
        reprice_10s= math.ceil(base_new_10s * p_rep)
        cancel_10s = math.ceil(base_new_10s * p_can)

        return fill_10s, reprice_10s, cancel_10s

    def _kpi_penalty(self, msg_10s, onbook_now, onbook_ago):
        """
        KPI驱动：计算messages/Δonbook效率，给出奖惩系数
        """
        msgs = sum(msg_10s.values())
        delta_onbook = max(1e-6, abs(float(onbook_now - onbook_ago)))
        mpd = msgs / delta_onbook  # messages per Δonbook USD
        self.ema_mpd = _ema(self.ema_mpd, mpd, 0.2)
        
        # 经验带：高于阈值认为低效，降低 5~20%
        if self.ema_mpd is None:
            return 1.0, mpd
        if self.ema_mpd <= 0.15:   # 很高效
            return 1.05, mpd
        if self.ema_mpd <= 0.30:   # 正常
            return 1.0, mpd
        if self.ema_mpd <= 0.60:   # 偏低效
            return 0.9, mpd
        return 0.8, mpd            # 低效

    def _usage_govern(self, usage_pct, dt):
        """
        H2: PID Governor强化版 - 让usage_pct紧密贴近目标10-12%
        增强积分项Ki，改善跟踪性能
        """
        self.ema_usage = _ema(self.ema_usage, usage_pct, 0.3)
        e = (self.ema_usage or usage_pct) - self.usage_target
        self.err_int += e * dt
        
        # H2强化参数：提高Ki增强积分控制，改善稳态跟踪
        Kp, Ki = 0.06, 0.015  # 原值：0.04, 0.005
        adj = -(Kp * e + Ki * self.err_int)
        scale = 1.0 + self._clamp(adj, -0.25, 0.25)  # 增大调整幅度到±25%
        
        # 积分抗饱和：防止积分项过大导致震荡
        max_int_err = 50.0  # 积分项最大累积误差
        self.err_int = self._clamp(self.err_int, -max_int_err, max_int_err)
        
        # 安全墙：超安全上限 → 强制缩 20%
        if usage_pct >= self.usage_safe:
            scale = min(scale, 0.8)
        
        # H2指标：记录tracking_error用于计算MSE
        if not hasattr(self, '_h2_tracking_errors'):
            self._h2_tracking_errors = deque(maxlen=600)  # 1分钟采样
        self._h2_tracking_errors.append(e * e)  # 平方误差
        
        # 每分钟计算一次MSE
        if len(self._h2_tracking_errors) >= 600:
            mse = sum(self._h2_tracking_errors) / len(self._h2_tracking_errors)
            logger.info(f"[H2-PID] usage_tracking_mse={mse:.4f} target_range=[10,12]%")
            
        return self._clamp(scale, 0.5, 1.5)  # 扩大调整范围

    def step(self, now_ts,
             n_l0, n_l1, n_l2,
             ttl_l0, ttl_l1, ttl_l2,
             msg_10s: dict,
             usage_pct: float,
             onbook_usd_now: float,
             onbook_usd_10s_ago: float,
             n_star_override: int=None,
             inv_err: float=0.0):  # Phase 6 M1: 添加库存偏移参数
        """
        控制平面核心步骤：输入当前状态，输出建议预算与突发，以及 TTL 缩放
        
        Phase 6 M1增强：支持双边预算分水
        
        Returns:
            dict: {
                'fill_10s': int, 'reprice_10s': int, 'cancel_10s': int,
                'burst_fill': int, 'burst_reprice': int, 'burst_cancel': int,
                'ttl_scale': float,
                # Phase 6 M1: 新增双边预算
                'fill_10s_buy': int, 'fill_10s_sell': int,
                'burst_fill_buy': int, 'burst_fill_sell': int
            }
        """
        try:
            dt = max(1e-3, now_ts - self.last_apply_ts) if self.last_apply_ts else 1.0
            n_target = int(n_star_override if n_star_override is not None else (n_l0 + n_l1 + n_l2))
            tau = self._weighted_tau(ttl_l0, ttl_l1, ttl_l2, n_l0, n_l1, n_l2)

            # 1) CQM 基础预算
            fill, rep, can = self._cqm_budgets(n_target, tau, msg_10s)

            # 2) usage 闭环：统一乘一个缩放
            usage_scale = self._usage_govern(usage_pct, dt)
            fill = int(round(fill * usage_scale))
            rep  = int(round(rep  * usage_scale))
            can  = int(round(can  * usage_scale))

            # 3) KPI 惩奖：再乘一次（侧重 Fill/Reprice）
            kpi_scale, mpd = self._kpi_penalty(msg_10s, onbook_usd_now, onbook_usd_10s_ago)
            fill = int(round(fill * kpi_scale))
            rep  = int(round(rep  * kpi_scale))

            # 4) 限幅 + 平滑（避免抖动）
            prev = self.prev or (fill, rep, can)
            def _ramp(cur, old, step=3):
                if cur > old + step: return old + step
                if cur < old - step: return old - step
                return cur

            fill = _ramp(fill, prev[0])
            rep = _ramp(rep, prev[1])
            can = _ramp(can, prev[2])
            
            fill = self._clamp(fill, self.min_fill, self.max_fill)
            rep  = self._clamp(rep , self.min_reprice, self.max_reprice)
            can  = self._clamp(can , self.min_cancel, self.max_cancel)

            # 5) 突发 = 预算（默认）；TTL 缩放反向跟随 usage（usage 高 → TTL 变长）
            ttl_scale = self._clamp(1.0 + 0.5 * ((self.usage_target - usage_pct)/max(1.0, self.usage_target)), 0.8, 1.3)

            # Phase 6 Fix 3: 更平衡的双边预算分水
            # 使用更温和的sigmoid，确保两侧都有足够配额
            gamma = 1.0  # 降低斜率，让分配更平衡
            alpha = 1.0 / (1.0 + math.exp(-gamma * inv_err))  # sigmoid函数
            # Phase 6 Fix 3: 扩大范围到35%-65%，确保双侧都有合理配额
            alpha = self._clamp(alpha, 0.35, 0.65)  # 最差也是35:65分配
            
            # 按边分配预算
            fill_10s_buy = int(alpha * fill)
            fill_10s_sell = int((1.0 - alpha) * fill)
            burst_fill_buy = int(alpha * fill)  # 突发配额也按比例分
            burst_fill_sell = int((1.0 - alpha) * fill)
            
            # 确保最小值
            fill_10s_buy = max(1, fill_10s_buy)
            fill_10s_sell = max(1, fill_10s_sell)
            burst_fill_buy = max(1, burst_fill_buy)
            burst_fill_sell = max(1, burst_fill_sell)
            
            # 日志输出双边预算
            logger.info(f"[CQM] side_budget fill=buy/sell={fill_10s_buy}/{fill_10s_sell} "
                       f"burst={burst_fill_buy}/{burst_fill_sell} inv_err={inv_err:.3f} α={alpha:.2f}")

            self.prev = (fill, rep, can)
            self.last_apply_ts = now_ts
            
            # 记录历史用于分析
            self.history.append({
                'ts': now_ts, 'n_target': n_target, 'tau': tau,
                'budgets': (fill, rep, can), 'burst': (fill, rep, can),
                'usage': usage_pct, 'mpd': mpd, 'scales': (usage_scale, kpi_scale), 'ttl_scale': ttl_scale,
                'side_split': {'alpha': alpha, 'buy': fill_10s_buy, 'sell': fill_10s_sell}  # Phase 6 M1
            })
            
            return {
                'fill_10s': fill, 'reprice_10s': rep, 'cancel_10s': can,
                'burst_fill': fill, 'burst_reprice': rep, 'burst_cancel': can,
                'ttl_scale': ttl_scale,
                # Phase 6 M1: 新增双边预算
                'fill_10s_buy': fill_10s_buy, 'fill_10s_sell': fill_10s_sell,
                'burst_fill_buy': burst_fill_buy, 'burst_fill_sell': burst_fill_sell,
                'alpha': alpha  # 返回alpha便于调试
            }
            
        except Exception as e:
            logger.error(f"[GOV] BudgetGovernor.step异常: {e}")
            # 故障回退：返回上次预算或保守默认值
            if self.prev:
                fill, rep, can = self.prev
                # Phase 6 M1: 故障时均分
                fill_half = fill // 2
                return {
                    'fill_10s': fill, 'reprice_10s': rep, 'cancel_10s': can,
                    'burst_fill': fill, 'burst_reprice': rep, 'burst_cancel': can,
                    'ttl_scale': 1.0,
                    'fill_10s_buy': fill_half, 'fill_10s_sell': fill_half,
                    'burst_fill_buy': fill_half, 'burst_fill_sell': fill_half,
                    'alpha': 0.5
                }
            else:
                return {
                    'fill_10s': 6, 'reprice_10s': 6, 'cancel_10s': 40,
                    'burst_fill': 6, 'burst_reprice': 6, 'burst_cancel': 40,
                    'ttl_scale': 1.0,
                    'fill_10s_buy': 3, 'fill_10s_sell': 3,
                    'burst_fill_buy': 3, 'burst_fill_sell': 3,
                    'alpha': 0.5
                }

    def get_stats(self):
        """获取控制器统计信息，用于调试和监控"""
        if not self.history:
            return {}
            
        recent = list(self.history)[-10:]  # 最近10s
        
        return {
            'samples': len(recent),
            'avg_usage': sum(h['usage'] for h in recent) / len(recent),
            'max_usage': max(h['usage'] for h in recent),
            'avg_mpd': sum(h['mpd'] for h in recent) / len(recent) if all('mpd' in h for h in recent) else 0,
            'current_budgets': recent[-1]['budgets'] if recent else None,
            'current_ttl_scale': recent[-1]['ttl_scale'] if recent else 1.0
        }