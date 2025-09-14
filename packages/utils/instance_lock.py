#!/usr/bin/env python3
"""
Single Instance Lock - 单实例锁
防止多个进程同时运行导致的权重叠加和资金冲突
"""

import os
import time
import fcntl
import atexit
import logging
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class InstanceLock:
    """单实例锁管理器"""
    
    def __init__(self, lock_file: str = None, timeout: float = 5.0, 
                 force_unlock: bool = False):
        """
        初始化单实例锁
        
        Args:
            lock_file: 锁文件路径，None则自动生成
            timeout: 获取锁超时时间（秒）
            force_unlock: 是否强制解锁（危险，仅测试使用）
        """
        # 默认锁文件路径
        if lock_file is None:
            lock_dir = Path("/tmp/doge_mm_locks")
            lock_dir.mkdir(exist_ok=True)
            lock_file = str(lock_dir / "main_instance.lock")
            
        self.lock_file = lock_file
        self.timeout = timeout
        self.force_unlock = force_unlock
        self.lock_fd = None
        self.locked = False
        
        # 注册清理函数
        atexit.register(self.release)
        
    def acquire(self) -> bool:
        """
        获取锁
        
        Returns:
            是否获取成功
        """
        try:
            # 强制解锁模式（测试用）
            if self.force_unlock and os.path.exists(self.lock_file):
                os.remove(self.lock_file)
                logger.warning("[InstanceLock] 强制删除锁文件")
                
            # 打开锁文件
            self.lock_fd = open(self.lock_file, 'w')
            
            # 尝试获取文件锁
            start_time = time.time()
            
            while time.time() - start_time < self.timeout:
                try:
                    # 非阻塞锁
                    fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    
                    # 写入进程信息
                    lock_info = {
                        'pid': os.getpid(),
                        'ppid': os.getppid(),
                        'start_time': time.time(),
                        'hostname': os.uname().nodename,
                        'cmdline': ' '.join(os.sys.argv)
                    }
                    
                    self.lock_fd.write(f"LOCKED\n")
                    self.lock_fd.write(f"PID: {lock_info['pid']}\n")
                    self.lock_fd.write(f"PPID: {lock_info['ppid']}\n")
                    self.lock_fd.write(f"START: {lock_info['start_time']}\n")
                    self.lock_fd.write(f"HOST: {lock_info['hostname']}\n")
                    self.lock_fd.write(f"CMD: {lock_info['cmdline']}\n")
                    self.lock_fd.flush()
                    
                    self.locked = True
                    logger.info(f"[InstanceLock] 获取锁成功: {self.lock_file} (PID: {os.getpid()})")
                    return True
                    
                except (IOError, OSError):
                    # 锁被占用，稍等再试
                    time.sleep(0.1)
                    continue
                    
            # 超时
            logger.error(f"[InstanceLock] 获取锁超时: {self.lock_file}")
            
            # 尝试读取锁文件信息
            try:
                self._log_lock_holder()
            except Exception as e:
                logger.error(f"[InstanceLock] 读取锁信息失败: {e}")
                
            return False
            
        except Exception as e:
            logger.error(f"[InstanceLock] 获取锁异常: {e}")
            return False
            
    def release(self):
        """释放锁"""
        if not self.locked or self.lock_fd is None:
            return
            
        try:
            # 释放文件锁
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            self.lock_fd.close()
            self.lock_fd = None
            
            # 删除锁文件
            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)
                
            self.locked = False
            logger.info(f"[InstanceLock] 释放锁成功: {self.lock_file}")
            
        except Exception as e:
            logger.error(f"[InstanceLock] 释放锁异常: {e}")
            
    def _log_lock_holder(self):
        """记录锁持有者信息"""
        try:
            if os.path.exists(self.lock_file):
                with open(self.lock_file, 'r') as f:
                    content = f.read().strip()
                    
                logger.error(f"[InstanceLock] 锁文件被占用:\n{content}")
                
                # 解析PID
                for line in content.split('\n'):
                    if line.startswith('PID:'):
                        pid_str = line.split(':', 1)[1].strip()
                        try:
                            pid = int(pid_str)
                            if self._is_process_alive(pid):
                                logger.error(f"[InstanceLock] 进程 {pid} 仍在运行")
                            else:
                                logger.warning(f"[InstanceLock] 进程 {pid} 已死亡，可能是僵尸锁")
                        except ValueError:
                            pass
                        break
                        
        except Exception as e:
            logger.error(f"[InstanceLock] 检查锁持有者失败: {e}")
            
    def _is_process_alive(self, pid: int) -> bool:
        """检查进程是否存活"""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
            
    def is_locked(self) -> bool:
        """检查是否已锁定"""
        return self.locked
        
    def get_lock_info(self) -> Optional[Dict]:
        """获取锁信息"""
        try:
            if not os.path.exists(self.lock_file):
                return None
                
            info = {}
            with open(self.lock_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line:
                        key, value = line.split(':', 1)
                        info[key.strip()] = value.strip()
                        
            return info
            
        except Exception as e:
            logger.error(f"[InstanceLock] 获取锁信息失败: {e}")
            return None
            
    def __enter__(self):
        """上下文管理器入口"""
        if not self.acquire():
            raise RuntimeError(f"无法获取实例锁: {self.lock_file}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.release()


def ensure_single_instance(lock_file: str = None, timeout: float = 5.0, 
                          force_unlock: bool = False) -> InstanceLock:
    """
    确保单实例运行
    
    Args:
        lock_file: 锁文件路径
        timeout: 超时时间
        force_unlock: 是否强制解锁
        
    Returns:
        InstanceLock实例
        
    Raises:
        RuntimeError: 无法获取锁时抛出
    """
    lock = InstanceLock(lock_file, timeout, force_unlock)
    
    if not lock.acquire():
        # 获取锁失败
        lock_info = lock.get_lock_info()
        
        error_msg = f"另一个实例正在运行，无法启动"
        
        if lock_info:
            pid = lock_info.get('PID', 'unknown')
            start_time = lock_info.get('START', 'unknown')
            cmd = lock_info.get('CMD', 'unknown')
            
            if start_time != 'unknown':
                try:
                    start_ts = float(start_time)
                    running_time = time.time() - start_ts
                    error_msg += f"\n运行中进程: PID={pid}, 运行时间={running_time:.0f}秒"
                except ValueError:
                    error_msg += f"\n运行中进程: PID={pid}"
                    
            error_msg += f"\n命令行: {cmd}"
            
        error_msg += f"\n锁文件: {lock.lock_file}"
        error_msg += f"\n如确认无其他实例运行，可删除锁文件或使用 --force-unlock"
        
        raise RuntimeError(error_msg)
        
    return lock


def check_instance_status(lock_file: str = None) -> Dict:
    """
    检查实例状态
    
    Args:
        lock_file: 锁文件路径
        
    Returns:
        状态字典
    """
    if lock_file is None:
        lock_dir = Path("/tmp/doge_mm_locks") 
        lock_file = str(lock_dir / "main_instance.lock")
        
    result = {
        'lock_file': lock_file,
        'locked': False,
        'lock_info': None,
        'process_alive': False
    }
    
    if os.path.exists(lock_file):
        result['locked'] = True
        
        try:
            with open(lock_file, 'r') as f:
                content = f.read().strip()
                
            # 解析锁信息
            info = {}
            for line in content.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    info[key.strip()] = value.strip()
                    
            result['lock_info'] = info
            
            # 检查进程是否存活
            if 'PID' in info:
                try:
                    pid = int(info['PID'])
                    os.kill(pid, 0)
                    result['process_alive'] = True
                except (ValueError, OSError, ProcessLookupError):
                    result['process_alive'] = False
                    
        except Exception as e:
            logger.error(f"检查锁状态失败: {e}")
            
    return result


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == 'status':
        # 状态检查模式
        status = check_instance_status()
        print(f"锁文件: {status['lock_file']}")
        print(f"锁定状态: {status['locked']}")
        
        if status['locked']:
            info = status['lock_info']
            if info:
                print(f"进程PID: {info.get('PID', 'unknown')}")
                print(f"进程存活: {status['process_alive']}")
                print(f"启动时间: {info.get('START', 'unknown')}")
                print(f"命令行: {info.get('CMD', 'unknown')}")
        sys.exit(0)
        
    # 测试模式
    try:
        with ensure_single_instance() as lock:
            print(f"获取锁成功，PID: {os.getpid()}")
            print("按Ctrl+C退出...")
            
            # 模拟运行
            while True:
                time.sleep(1)
                
    except RuntimeError as e:
        print(f"错误: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("用户中断，正常退出")
        sys.exit(0)