# -*- coding: utf-8 -*-
"""
Airtest 基础类 - 提供 Airtest 的基本设置和常用功能
"""
from airtest.core.api import (
    connect_device, ST, G, wait, click, snapshot, 
    init_device, start_app, stop_app, Template, sleep, home
)
from airtest.core.error import NoDeviceError, TargetNotFoundError
import os
import re
import shlex
import shutil
import subprocess
import time
import logging
import threading
import queue
from typing import Optional
from urllib.parse import urlsplit
from django.conf import settings

logger = logging.getLogger(__name__)

REMOTE_ADB_SERIAL_PATTERN = re.compile(r'^[^:\s]+:\d+$')


def _uses_remote_adb_server() -> bool:
    """Return True when adb commands are delegated to another host.

    Airtest streaming capture/touch methods open forwarded TCP sockets on the
    adb-server host. Those sockets are commonly unreachable from a Docker
    container, while screencap/input based methods remain reliable.
    """
    socket_value = str(os.environ.get('ADB_SERVER_SOCKET') or '').strip()
    if not socket_value.startswith('tcp:'):
        return False
    parts = socket_value.split(':')
    if len(parts) < 3:
        return False
    host = ':'.join(parts[1:-1]).strip('[]').lower()
    return host not in {'', 'localhost', '127.0.0.1', '::1'}


def _resolve_adb_path() -> str:
    """
    Resolve the adb binary used by Airtest.

    Airtest ships a bundled adb binary which may not match the container CPU
    architecture. Prefer the project config/PATH adb so Docker arm64 images use
    the Debian Android SDK adb installed in the image.
    """
    configured_path = 'adb'
    try:
        from apps.app_automation.models import AppTestConfig
        config = AppTestConfig.objects.first()
        if config and config.adb_path:
            configured_path = config.adb_path
    except Exception as e:
        logger.warning(f"读取 ADB 配置失败，使用默认 adb: {e}")

    configured_path = configured_path.strip() or 'adb'
    resolved_path = shutil.which(configured_path)
    if resolved_path:
        return resolved_path

    if os.path.exists(configured_path) and os.access(configured_path, os.X_OK):
        return configured_path

    fallback_path = shutil.which('adb') or '/usr/bin/adb'
    logger.warning(f"配置的 ADB 不可用: {configured_path}，尝试使用: {fallback_path}")
    return fallback_path


class AirtestBase:
    """Airtest基础类，提供Airtest的基本设置和常用功能"""
    
    # 默认配置
    DEFAULT_CONFIG = {
        'RETRY_COUNT': 3,
        'RETRY_INTERVAL': 5,
        'DEVICE_CONNECT_TIMEOUT': 30,
        'FIND_TIMEOUT': 10,
        'CLICK_DELAY': 0.5,
        'CAP_METHOD': 'JAVACAP',
        'TOUCH_METHOD': 'MAXTOUCH',
    }
    
    def __init__(
        self,
        device_id: Optional[str] = None,
        screenshots_dir: Optional[str] = None,
        username: Optional[str] = None,
        platform: str = 'android',
        wda_url: str = '',
        wda_bundle_id: str = '',
    ):
        """
        初始化AirtestBase实例
        
        Args:
            device_id: Android ADB 序列号或 iPhone UDID
            screenshots_dir: 截图目录，如果未提供则使用默认目录
            username: 执行用户名，用于截图目录分组
            platform: android 或 ios
            wda_url: iOS WebDriverAgent 地址
            wda_bundle_id: 自定义签名后的 WDA Bundle ID
        """
        self.device_id = device_id
        self.platform = str(platform or 'android').strip().lower()
        if self.platform not in {'android', 'ios'}:
            raise ValueError(f'不支持的设备平台: {platform}')
        self.wda_url = str(wda_url or '').strip().rstrip('/')
        self.wda_bundle_id = str(wda_bundle_id or '').strip()
        self.is_connected = False
        self.adb_path = _resolve_adb_path()
        
        # 设置截图目录: media/app-automation/screenshots/{username}/
        if screenshots_dir:
            self.screenshots_dir = screenshots_dir
        else:
            self.screenshots_dir = os.path.join(
                settings.MEDIA_ROOT, 'app-automation', 'screenshots', username or 'unknown'
            )
        
        # 确保截图目录存在
        os.makedirs(self.screenshots_dir, exist_ok=True)
        logger.info(
            f"初始化AirtestBase实例，平台: {self.platform}，设备ID: {self.device_id}，"
            f"ADB: {self.adb_path}，WDA: {self.wda_url or '-'}，截图目录: {self.screenshots_dir}"
        )
    
    def setup_airtest(self, config: Optional[dict] = None) -> bool:
        """
        设置Airtest环境，连接设备
        
        Args:
            config: 配置字典，可选参数包括 RETRY_COUNT, RETRY_INTERVAL, DEVICE_CONNECT_TIMEOUT 等
            
        Returns:
            是否设置成功
        """
        # 合并配置
        cfg = {**self.DEFAULT_CONFIG}
        if config:
            cfg.update(config)
        
        retry_count = cfg['RETRY_COUNT']
        retry_interval = cfg['RETRY_INTERVAL']
        timeout = cfg['DEVICE_CONNECT_TIMEOUT']
        cap_method = str(cfg.get('CAP_METHOD') or 'JAVACAP').upper()
        touch_method = str(cfg.get('TOUCH_METHOD') or 'MAXTOUCH').upper()
        if self.platform == 'android' and _uses_remote_adb_server():
            if cap_method in {'JAVACAP', 'MINICAP'}:
                cap_method = 'ADBCAP'
            if touch_method in {'MAXTOUCH', 'MINITOUCH'}:
                touch_method = 'ADBTOUCH'
            logger.info(
                '检测到远程 ADB Server，使用稳定后端: cap=%s, touch=%s',
                cap_method,
                touch_method,
            )
        
        for attempt in range(retry_count):
            try:
                if self.platform == 'ios':
                    if not self.wda_url:
                        raise RuntimeError('iOS 设备缺少 WDA 地址')
                    logger.info(
                        f"尝试连接 iOS 设备: {self.device_id or '-'} via {self.wda_url} "
                        f"(尝试 {attempt+1}/{retry_count})"
                    )
                    success = self._init_device_with_timeout(
                        platform='IOS',
                        uuid=self.wda_url,
                        udid=self.device_id or None,
                        wda_bundle_id=self.wda_bundle_id or None,
                        timeout=timeout,
                    )
                elif self.device_id:
                    logger.info(f"尝试连接到设备: {self.device_id} (尝试 {attempt+1}/{retry_count})")
                    success = self._init_device_with_timeout(
                        platform='Android',
                        uuid=self.device_id,
                        cap_method=cap_method,
                        touch_method=touch_method,
                        timeout=timeout
                    )
                else:
                    logger.info(f"尝试连接到默认设备 (尝试 {attempt+1}/{retry_count})")
                    success = self._init_device_with_timeout(
                        platform='Android',
                        cap_method=cap_method,
                        touch_method=touch_method,
                        timeout=timeout
                    )
                
                if not success:
                    raise RuntimeError("设备连接失败")
                
                # 设置全局超时时间
                ST.FIND_TIMEOUT = cfg['FIND_TIMEOUT']
                if hasattr(ST, 'CLICK_DELAY'):
                    ST.CLICK_DELAY = cfg['CLICK_DELAY']
                
                self.is_connected = True
                logger.info("Airtest环境设置完成，设备已连接")
                return True
                
            except Exception as e:
                logger.error(f"设置Airtest环境时出错: {str(e)}", exc_info=True)
                
                if attempt < retry_count - 1:
                    logger.info(f"{retry_interval}秒后重试连接...")
                    time.sleep(retry_interval)
                else:
                    logger.error(f"经过 {retry_count} 次尝试后，无法连接设备")
                    return False
        
        return False
    
    def _init_device_with_timeout(
        self,
        platform: str,
        uuid: Optional[str] = None,
        timeout: int = 30,
        **device_kwargs
    ) -> bool:
        """
        使用超时机制初始化设备
        
        Args:
            platform: 平台类型，如 'Android'
            uuid: 设备UUID（可选）
            timeout: 超时时间（秒）
            
        Returns:
            是否初始化成功
        """
        result_queue = queue.Queue()
        exception_queue = queue.Queue()

        if platform.lower() == 'android' and uuid and REMOTE_ADB_SERIAL_PATTERN.match(uuid):
            self._ensure_remote_device_connected(uuid)
        
        def call_init_device():
            try:
                logger.info(f"线程中开始调用 init_device()，平台: {platform}，ADB: {self.adb_path}")
                if platform.lower() == 'ios':
                    init_device(platform=platform, uuid=uuid, **device_kwargs)
                elif uuid:
                    init_device(platform=platform, uuid=uuid, adb_path=self.adb_path, **device_kwargs)
                else:
                    init_device(platform=platform, adb_path=self.adb_path, **device_kwargs)
                logger.info(f"线程中 init_device() 调用完成")
                result_queue.put(True)
            except Exception as e:
                logger.error(f"线程中 init_device() 调用异常: {type(e).__name__}: {e}", exc_info=True)
                exception_queue.put(e)
        
        thread = threading.Thread(target=call_init_device, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        
        if thread.is_alive():
            logger.error(f"init_device() 调用超时（{timeout}秒），设备可能无法连接")
            return False
        
        if not exception_queue.empty():
            raise exception_queue.get()
        
        if not result_queue.empty():
            logger.info(f"init_device() 调用完成，设备已连接")
            return True
        else:
            logger.warning(f"init_device() 调用完成，但未收到结果")
            return False

    def _ensure_remote_device_connected(self, serial: str) -> None:
        """For TCP/IP devices, make sure adb has an active connection first."""
        try:
            state = subprocess.run(
                [self.adb_path, '-s', serial, 'get-state'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5
            )
            if state.returncode == 0 and state.stdout.strip() == 'device':
                return

            connect_result = subprocess.run(
                [self.adb_path, 'connect', serial],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=20
            )
            detail = (connect_result.stdout or connect_result.stderr or '').strip()
            logger.info(f"ADB 连接远程设备 {serial}: {detail}")
        except Exception as e:
            logger.warning(f"ADB 预连接远程设备失败 {serial}: {e}")
    
    def teardown_airtest(self) -> None:
        """清理Airtest环境，断开设备连接"""
        try:
            self._disconnect_device()
            self.is_connected = False
            logger.info("Airtest环境已清理")
        except Exception as e:
            logger.error(f"清理Airtest环境时出错: {str(e)}", exc_info=True)
    
    def _disconnect_device(self) -> None:
        """断开与设备的连接"""
        try:
            if G.DEVICE:
                G.DEVICE.disconnect()
                self.is_connected = False
                logger.info("设备连接已断开")
        except NoDeviceError:
            logger.info("没有设备连接，无需断开")
        except Exception as e:
            logger.error(f"断开设备连接时出错: {str(e)}", exc_info=True)
    
    def is_device_connected(self) -> bool:
        """
        检查设备是否已连接
        
        Returns:
            设备连接状态
        """
        device_connected = hasattr(G, 'DEVICE') and G.DEVICE is not None
        
        if device_connected != self.is_connected:
            self.is_connected = device_connected
            logger.info(f"设备连接状态更新: {'已连接' if device_connected else '已断开'}")
        
        return device_connected
    
    def screenshot(self, name: str) -> str:
        """
        截取屏幕并保存
        
        Args:
            name: 截图名称
            
        Returns:
            截图路径，失败返回空字符串
        """
        if not self.is_device_connected():
            logger.warning("设备未连接，无法截图")
            return ""
        
        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(self.screenshots_dir, f'{name}_{timestamp}.png')
            snapshot(filename=screenshot_path)
            logger.info(f"截图已保存: {screenshot_path}")
            return screenshot_path
        except Exception as e:
            logger.error(f"截图失败: {str(e)}", exc_info=True)
            return ""
    
    def open_app(
        self,
        package_name: str,
        retry_count: int = 3,
        retry_interval: int = 5,
        reset_app: bool = True
    ) -> bool:
        """
        启动指定包名的应用，包含智能重试机制
        
        Args:
            package_name: 应用包名
            retry_count: 重试次数
            retry_interval: 重试间隔（秒）
            reset_app: 启动前是否先关闭应用，默认开启以保证用例入口稳定
            
        Returns:
            是否启动成功
        """
        if not self.is_connected:
            logger.error("设备未连接，无法启动应用")
            return False
        
        for attempt in range(retry_count):
            try:
                logger.info(f"尝试启动应用: {package_name} (尝试 {attempt+1}/{retry_count})")
                if reset_app:
                    try:
                        stop_app(package_name)
                        time.sleep(0.5)
                    except Exception as stop_err:
                        logger.warning(f"启动前关闭应用失败，继续启动: {stop_err}")
                start_app(package_name)
                
                # 等待应用启动
                time.sleep(2)
                
                # 验证应用是否真正启动成功
                if self.is_app_running(package_name):
                    logger.info(f"成功启动应用: {package_name}")
                    return True
                else:
                    logger.warning(f"应用 {package_name} 启动后未检测到运行状态")
                
                if attempt < retry_count - 1:
                    logger.info(f"{retry_interval}秒后重试启动应用...")
                    time.sleep(retry_interval)
                    
            except Exception as e:
                logger.error(f"启动应用失败: {str(e)}", exc_info=True)
                if attempt < retry_count - 1:
                    logger.info(f"{retry_interval}秒后重试启动应用...")
                    time.sleep(retry_interval)
        
        logger.error(f"经过 {retry_count} 次尝试后，启动应用 {package_name} 失败")
        return False

    def go_home(self, settle_seconds: float = 1.0) -> bool:
        """
        回到设备主屏幕。

        iOS 无包名用例通常把“点击 Safari/浏览器图标”录成第一个步骤；
        若上一次执行失败后设备还停在 Safari 内页，必须先回桌面，否则首步会误点当前页面。
        """
        if not self.is_connected:
            logger.error("设备未连接，无法回到主屏幕")
            return False

        try:
            logger.info("尝试回到设备主屏幕")
            home()
            time.sleep(max(0.0, float(settle_seconds)))
            logger.info("已回到设备主屏幕")
            return True
        except Exception as e:
            logger.error(f"回到设备主屏幕失败: {str(e)}", exc_info=True)
            return False

    def open_url(self, url: str, settle_seconds: float = 3.0) -> bool:
        """
        通过设备自动化打开 URL。

        当前主要用于 iOS Safari/H5 用例：无包名执行时直接打开稳定入口，
        避免 Safari 恢复上一次失败时停留的弹窗或中间页面。
        """
        target_url = str(url or '').strip()
        if not target_url:
            logger.error("URL 为空，无法打开")
            return False
        parsed_url = urlsplit(target_url)
        if parsed_url.scheme not in {'http', 'https'} or not parsed_url.netloc:
            logger.error("仅允许打开有效的 HTTP/HTTPS URL")
            return False
        if not self.is_connected:
            logger.error("设备未连接，无法打开 URL")
            return False

        try:
            logger.info(f"尝试打开 URL: {target_url}")
            if self.platform == 'ios':
                driver = getattr(G.DEVICE, 'driver', None)
                if not driver or not hasattr(driver, 'open_url'):
                    raise RuntimeError('当前 iOS 设备驱动不支持 open_url')
                driver.open_url(target_url)
            else:
                G.DEVICE.shell(
                    'am start -a android.intent.action.VIEW -d '
                    f'{shlex.quote(target_url)}'
                )
            time.sleep(max(0.0, float(settle_seconds)))
            logger.info(f"URL 已打开: {target_url}")
            return True
        except Exception as e:
            logger.error(f"打开 URL 失败: {str(e)}", exc_info=True)
            return False
    
    def close_app(self, package_name: str) -> bool:
        """
        关闭指定包名的应用
        
        Args:
            package_name: 应用包名
            
        Returns:
            是否关闭成功
        """
        if not self.is_connected:
            logger.warning("设备未连接，无需关闭应用")
            return True
        
        try:
            logger.info(f"尝试关闭应用: {package_name}")
            stop_app(package_name)
            logger.info(f"成功关闭应用: {package_name}")
            return True
        except Exception as e:
            logger.error(f"关闭应用失败: {str(e)}", exc_info=True)
            return False
    
    def is_app_installed(self, package_name: str) -> bool:
        """
        检查指定包名的应用是否已安装
        
        Args:
            package_name: 应用包名
            
        Returns:
            是否已安装
        """
        if not self.is_connected:
            logger.warning("设备未连接，无法检查应用安装状态")
            return False
        
        try:
            if self.platform == 'ios':
                state = G.DEVICE.app_state(package_name)
                value = state.get('value') if isinstance(state, dict) else state
                return value is not None
            result = G.DEVICE.shell(f"pm list packages | grep {package_name}")
            installed = package_name in result
            logger.info(f"应用 {package_name} {'已安装' if installed else '未安装'}")
            return installed
        except Exception as e:
            logger.error(f"检查应用安装状态时出错: {str(e)}", exc_info=True)
            return False
    
    def is_app_running(self, package_name: str) -> bool:
        """
        检查指定包名的应用是否正在运行
        
        Args:
            package_name: 应用包名
            
        Returns:
            是否正在运行
        """
        if not self.is_connected:
            logger.warning("设备未连接，无法检查应用运行状态")
            return False
        
        try:
            if self.platform == 'ios':
                state = G.DEVICE.app_state(package_name)
                value = state.get('value') if isinstance(state, dict) else state
                running = value in {2, 3, 4, '2', '3', '4'}
                logger.info(f"iOS 应用 {package_name} {'正在运行' if running else '未运行'}")
                return running
            # 尝试多种方法检查应用运行状态
            methods = [
                f"pidof {package_name}",
                f"ps | grep {package_name}",
                f"dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp' | grep {package_name}"
            ]
            
            for method in methods:
                try:
                    result = G.DEVICE.shell(method)
                    if result.strip():
                        logger.info(f"应用 {package_name} 正在运行")
                        return True
                except Exception:
                    continue
            
            logger.info(f"应用 {package_name} 未运行")
            return False
        except Exception as e:
            logger.error(f"检查应用运行状态时出错: {str(e)}", exc_info=True)
            return False
