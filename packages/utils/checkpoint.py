#!/usr/bin/env python3
"""
Checkpoint Manager - 检查点快速恢复系统
"""

import time
import json
import hashlib
import logging
import threading
from typing import Dict, List, Optional, Any
from pathlib import Path
import pickle
import gzip

logger = logging.getLogger(__name__)


class CheckpointManager:
    """检查点管理器"""
    
    def __init__(self, checkpoint_dir: str = "/tmp/doge_mm_checkpoints",
                 interval_sec: int = 60, max_checkpoints: int = 10):
        """
        初始化检查点管理器
        
        Args:
            checkpoint_dir: 检查点目录
            interval_sec: 保存间隔
            max_checkpoints: 最大检查点数量
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(exist_ok=True)
        
        self.interval = interval_sec
        self.max_checkpoints = max_checkpoints
        self.last_save_time = 0
        
        # 状态提供者
        self.state_providers = {}  # name -> callable
        
        # 元数据
        self.checkpoint_metadata = {
            'version': '1.0',
            'created_by': 'doge_mm_v78',
            'format': 'json_gzip'
        }
        
        self.lock = threading.RLock()
        
    def register_provider(self, name: str, provider: callable):
        """注册状态提供者"""
        with self.lock:
            self.state_providers[name] = provider
            
    def _get_checkpoint_path(self, timestamp: float = None) -> Path:
        """获取检查点文件路径"""
        if timestamp is None:
            timestamp = time.time()
        filename = f"checkpoint_{int(timestamp)}.json.gz"
        return self.checkpoint_dir / filename
        
    def _collect_state(self) -> Dict[str, Any]:
        """收集所有状态"""
        state = {}
        
        for name, provider in self.state_providers.items():
            try:
                provider_state = provider()
                if provider_state is not None:
                    state[name] = provider_state
            except Exception as e:
                logger.error(f"[Checkpoint] 状态收集失败 {name}: {e}")
                state[name] = {'error': str(e)}
                
        return state
        
    def save_checkpoint(self, force: bool = False) -> Optional[str]:
        """
        保存检查点
        
        Args:
            force: 强制保存，忽略时间间隔
            
        Returns:
            检查点文件路径
        """
        now = time.time()
        
        if not force and now - self.last_save_time < self.interval:
            return None
            
        with self.lock:
            try:
                # 收集状态
                state = self._collect_state()
                
                # 生成检查点
                checkpoint = {
                    'metadata': self.checkpoint_metadata.copy(),
                    'timestamp': now,
                    'hostname': __import__('os').uname().nodename,
                    'state': state
                }
                
                # 计算哈希
                state_str = json.dumps(state, sort_keys=True)
                checkpoint['state_hash'] = hashlib.md5(state_str.encode()).hexdigest()
                
                # 保存到文件
                checkpoint_path = self._get_checkpoint_path(now)
                temp_path = checkpoint_path.with_suffix('.tmp')
                
                with gzip.open(temp_path, 'wt') as f:
                    json.dump(checkpoint, f, indent=2)
                    
                # 原子移动
                temp_path.rename(checkpoint_path)
                
                self.last_save_time = now
                
                # 清理旧检查点
                self._cleanup_old_checkpoints()
                
                logger.info(f"[Checkpoint] 保存成功: {checkpoint_path.name}")
                return str(checkpoint_path)
                
            except Exception as e:
                logger.error(f"[Checkpoint] 保存失败: {e}")
                return None
                
    def _cleanup_old_checkpoints(self):
        """清理旧检查点"""
        try:
            # 获取所有检查点文件
            checkpoint_files = list(self.checkpoint_dir.glob("checkpoint_*.json.gz"))
            
            # 按时间排序（最新的在前）
            checkpoint_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            # 删除多余的
            for old_file in checkpoint_files[self.max_checkpoints:]:
                old_file.unlink()
                logger.debug(f"[Checkpoint] 删除旧文件: {old_file.name}")
                
        except Exception as e:
            logger.error(f"[Checkpoint] 清理失败: {e}")
            
    def get_latest_checkpoint(self) -> Optional[str]:
        """获取最新检查点文件"""
        try:
            checkpoint_files = list(self.checkpoint_dir.glob("checkpoint_*.json.gz"))
            if not checkpoint_files:
                return None
                
            # 找到最新的
            latest_file = max(checkpoint_files, key=lambda x: x.stat().st_mtime)
            return str(latest_file)
            
        except Exception as e:
            logger.error(f"[Checkpoint] 获取最新检查点失败: {e}")
            return None
            
    def load_checkpoint(self, checkpoint_path: str = None) -> Optional[Dict]:
        """
        加载检查点
        
        Args:
            checkpoint_path: 检查点文件路径，None则加载最新的
            
        Returns:
            检查点数据
        """
        if checkpoint_path is None:
            checkpoint_path = self.get_latest_checkpoint()
            
        if not checkpoint_path or not Path(checkpoint_path).exists():
            logger.warning("[Checkpoint] 无可用检查点")
            return None
            
        try:
            with gzip.open(checkpoint_path, 'rt') as f:
                checkpoint = json.load(f)
                
            # 验证检查点
            if not self._validate_checkpoint(checkpoint):
                logger.error(f"[Checkpoint] 检查点验证失败: {checkpoint_path}")
                return None
                
            logger.info(
                f"[Checkpoint] 加载成功: {Path(checkpoint_path).name} "
                f"时间={time.time() - checkpoint['timestamp']:.0f}s前"
            )
            
            return checkpoint
            
        except Exception as e:
            logger.error(f"[Checkpoint] 加载失败 {checkpoint_path}: {e}")
            return None
            
    def _validate_checkpoint(self, checkpoint: Dict) -> bool:
        """验证检查点完整性"""
        try:
            # 检查必需字段
            required_fields = ['metadata', 'timestamp', 'state', 'state_hash']
            for field in required_fields:
                if field not in checkpoint:
                    logger.error(f"[Checkpoint] 缺少字段: {field}")
                    return False
                    
            # 验证哈希
            state_str = json.dumps(checkpoint['state'], sort_keys=True)
            calculated_hash = hashlib.md5(state_str.encode()).hexdigest()
            
            if calculated_hash != checkpoint['state_hash']:
                logger.error("[Checkpoint] 哈希校验失败")
                return False
                
            # 检查时间（不能太旧）
            age = time.time() - checkpoint['timestamp']
            if age > 3600:  # 1小时
                logger.warning(f"[Checkpoint] 检查点较旧: {age:.0f}s")
                
            return True
            
        except Exception as e:
            logger.error(f"[Checkpoint] 验证异常: {e}")
            return False
            
    def restore_state(self, checkpoint: Dict, target_objects: Dict[str, Any]) -> bool:
        """
        恢复状态到目标对象
        
        Args:
            checkpoint: 检查点数据
            target_objects: 目标对象映射 {name: object}
            
        Returns:
            恢复是否成功
        """
        try:
            state = checkpoint['state']
            restored_count = 0
            
            for name, obj in target_objects.items():
                if name in state and hasattr(obj, 'restore_from_checkpoint'):
                    try:
                        obj.restore_from_checkpoint(state[name])
                        restored_count += 1
                        logger.debug(f"[Checkpoint] 恢复状态: {name}")
                    except Exception as e:
                        logger.error(f"[Checkpoint] 恢复状态失败 {name}: {e}")
                        
            logger.info(f"[Checkpoint] 恢复完成: {restored_count} 个对象")
            return restored_count > 0
            
        except Exception as e:
            logger.error(f"[Checkpoint] 状态恢复异常: {e}")
            return False
            
    def get_status(self) -> Dict:
        """获取状态"""
        try:
            checkpoint_files = list(self.checkpoint_dir.glob("checkpoint_*.json.gz"))
            
            return {
                'checkpoint_count': len(checkpoint_files),
                'latest_checkpoint': self.get_latest_checkpoint(),
                'last_save_time': self.last_save_time,
                'time_since_save': time.time() - self.last_save_time,
                'providers_count': len(self.state_providers),
                'directory': str(self.checkpoint_dir)
            }
        except Exception as e:
            logger.error(f"[Checkpoint] 获取状态失败: {e}")
            return {'error': str(e)}
            
    async def auto_save_loop(self):
        """自动保存循环"""
        import asyncio
        
        while True:
            try:
                await asyncio.sleep(self.interval)
                self.save_checkpoint()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Checkpoint] 自动保存异常: {e}")


# 全局实例
_checkpoint_manager = None


def get_checkpoint_manager(checkpoint_dir: str = None, 
                          interval_sec: int = 60) -> CheckpointManager:
    """获取全局检查点管理器"""
    global _checkpoint_manager
    
    if _checkpoint_manager is None:
        _checkpoint_manager = CheckpointManager(checkpoint_dir, interval_sec)
        
    return _checkpoint_manager


if __name__ == "__main__":
    # 测试
    import asyncio
    
    class TestObject:
        def __init__(self):
            self.counter = 0
            self.data = {'test': 'value'}
            
        def get_state(self):
            return {
                'counter': self.counter,
                'data': self.data
            }
            
        def restore_from_checkpoint(self, state):
            self.counter = state['counter']
            self.data = state['data']
            print(f"恢复状态: counter={self.counter}")
            
    async def test():
        # 创建测试对象
        obj = TestObject()
        obj.counter = 42
        
        # 创建检查点管理器
        cm = CheckpointManager("/tmp/test_checkpoints")
        cm.register_provider('test_obj', obj.get_state)
        
        # 保存检查点
        checkpoint_path = cm.save_checkpoint(force=True)
        print(f"保存检查点: {checkpoint_path}")
        
        # 修改状态
        obj.counter = 99
        
        # 加载检查点
        checkpoint = cm.load_checkpoint()
        if checkpoint:
            # 恢复状态
            success = cm.restore_state(checkpoint, {'test_obj': obj})
            print(f"恢复成功: {success}")
            
        print(f"最终状态: {cm.get_status()}")
        
    asyncio.run(test())