# -*- coding: utf-8 -*-
"""APP自动化配置管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
import logging
import json
import subprocess
import urllib.error
import urllib.request

from ..models import AppTestConfig
from ..serializers import AppTestConfigSerializer

logger = logging.getLogger(__name__)


DEFAULT_APP_CONFIG = {
    'adb_path': 'adb',
    'node_path': 'node',
    'appium_path': 'appium',
    'appium_server_url': 'http://127.0.0.1:4723',
    'appium_driver': 'uiautomator2',
    'appium_automation_name': 'UiAutomator2',
    'ios_wda_url': 'http://host.docker.internal:8100',
    'ios_wda_bundle_id': '',
    'ios_browser_start_url': '',
    'device_bridge_url': 'http://host.docker.internal:8765',
}


def _run_version_command(command, args, timeout=8):
    try:
        result = subprocess.run(
            [command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout or '').strip()
        return {
            'ok': result.returncode == 0,
            'version': output.splitlines()[0] if output else '',
            'detail': output,
        }
    except FileNotFoundError:
        return {'ok': False, 'version': '', 'detail': f'命令不存在: {command}'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'version': '', 'detail': f'命令超时: {command}'}
    except Exception as exc:
        return {'ok': False, 'version': '', 'detail': str(exc)}


def _check_appium_driver(appium_path, driver_name):
    result = _run_version_command(appium_path, ['driver', 'list', '--installed'], timeout=15)
    detail = result.get('detail', '')
    driver_key = (driver_name or 'uiautomator2').lower()
    return {
        'ok': result.get('ok') and driver_key in detail.lower(),
        'version': driver_name or 'uiautomator2',
        'detail': detail or result.get('detail', ''),
    }


def _check_appium_server(url):
    status_url = f"{(url or '').rstrip('/')}/status"
    try:
        with urllib.request.urlopen(status_url, timeout=5) as response:
            body = response.read().decode('utf-8', errors='ignore')
            data = json.loads(body) if body else {}
            return {
                'ok': 200 <= response.status < 300,
                'version': data.get('value', {}).get('build', {}).get('version', ''),
                'detail': body[:500],
            }
    except urllib.error.URLError as exc:
        return {'ok': False, 'version': '', 'detail': str(exc)}


def _check_ios_wda(url):
    status_url = f"{(url or '').rstrip('/')}/status"
    try:
        with urllib.request.urlopen(status_url, timeout=5) as response:
            body = response.read().decode('utf-8', errors='ignore')
            data = json.loads(body) if body else {}
            value = data.get('value') or data
            os_info = value.get('os') or {}
            version = os_info.get('version') or os_info.get('sdkVersion') or ''
            return {
                'ok': 200 <= response.status < 300,
                'version': str(version or 'WDA online'),
                'detail': body[:500],
                'optional': True,
            }
    except urllib.error.URLError as exc:
        return {'ok': False, 'version': '', 'detail': str(exc), 'optional': True}
    except Exception as exc:
        return {'ok': False, 'version': '', 'detail': str(exc), 'optional': True}


def _check_device_bridge(url):
    health_url = f"{(url or '').rstrip('/')}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as response:
            body = response.read().decode('utf-8', errors='ignore')
            data = json.loads(body) if body else {}
            return {
                'ok': 200 <= response.status < 300 and data.get('status') == 'ok',
                'version': data.get('service') or 'device bridge',
                'detail': body[:500],
            }
    except Exception as exc:
        return {'ok': False, 'version': '', 'detail': str(exc)}


class AppConfigViewSet(viewsets.ViewSet):
    """APP测试配置视图集"""
    permission_classes = [IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def current(self, request):
        """获取当前配置"""
        try:
            # 获取或创建配置（单例模式）
            config, created = AppTestConfig.objects.get_or_create(
                id=1,
                defaults=DEFAULT_APP_CONFIG
            )
            
            serializer = AppTestConfigSerializer(config)
            return Response({
                'success': True,
                'data': serializer.data
            })
        except Exception as e:
            logger.error(f"获取配置失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'获取配置失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def save(self, request):
        """保存配置"""
        try:
            # 获取或创建配置（单例模式）
            config, created = AppTestConfig.objects.get_or_create(
                id=1,
                defaults=DEFAULT_APP_CONFIG
            )
            
            serializer = AppTestConfigSerializer(config, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                logger.info(f"配置更新成功: {serializer.data}")
                return Response({
                    'success': True,
                    'message': '配置更新成功',
                    'data': serializer.data
                })
            else:
                return Response({
                    'success': False,
                    'message': '配置验证失败',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"更新配置失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'更新配置失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='runtime-status')
    def runtime_status(self, request):
        """检测 APP 自动化运行环境"""
        try:
            config, created = AppTestConfig.objects.get_or_create(
                id=1,
                defaults=DEFAULT_APP_CONFIG
            )
            checks = {
                'adb': _run_version_command(config.adb_path or 'adb', ['version']),
                'node': _run_version_command(config.node_path or 'node', ['--version']),
                'appium': _run_version_command(config.appium_path or 'appium', ['--version']),
                'uiautomator2': _check_appium_driver(config.appium_path or 'appium', config.appium_driver or 'uiautomator2'),
                'appium_server': _check_appium_server(config.appium_server_url),
                'ios_wda': _check_ios_wda(config.ios_wda_url),
                'device_bridge': _check_device_bridge(config.device_bridge_url),
            }
            required_checks = {
                key: value for key, value in checks.items()
                if not value.get('optional')
            }
            return Response({
                'success': True,
                'data': {
                    'config': AppTestConfigSerializer(config).data,
                    'checks': checks,
                    'ready': all(item.get('ok') for item in required_checks.values()),
                    'ios_ready': bool(checks['ios_wda'].get('ok')),
                }
            })
        except Exception as e:
            logger.error(f"检测运行环境失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'检测运行环境失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
