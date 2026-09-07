# -*- coding: utf-8 -*-
"""
测试执行器 - pytest + Allure 集成
"""
import os
import sys
import subprocess
import glob
import json
import shutil
import select
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

IOS_SAFARI_BUNDLE_IDS = {'com.apple.mobilesafari'}


class AppTestExecutor:
    """APP测试执行器，封装 pytest 执行逻辑"""

    RUNTIME_TEST_TARGET = (
        'apps/app_automation/tests/test_app_flow.py'
        '::AppFlowTests::test_execute_ui_flow'
    )
    
    def __init__(self, base_path: Optional[str] = None):
        """
        初始化测试执行器
        
        Args:
            base_path: 基础路径，默认使用 Django BASE_DIR
        """
        if base_path is None:
            self.base_path = settings.BASE_DIR
        else:
            self.base_path = base_path
        
        if not os.path.exists(self.base_path):
            raise ValueError(f"测试项目路径不存在: {self.base_path}")
        
        self._current_process: Optional[subprocess.Popen] = None
        
        logger.info(f"初始化 AppTestExecutor，基础路径: {self.base_path}")
    
    def run_tests(
        self,
        test_case_id: int,
        device_id: str,
        package_name: str,
        execution_id: Optional[int] = None,
        username: Optional[str] = None,
        platform: str = 'android',
        wda_url: str = '',
        wda_bundle_id: str = '',
    ) -> Dict[str, Any]:
        """
        运行APP测试用例并生成报告
        
        Args:
            test_case_id: 测试用例ID
            device_id: 设备ID
            package_name: 应用包名
            execution_id: 执行记录ID
            username: 执行用户名，用于日志目录分组
            platform: android 或 ios
            wda_url: iOS WebDriverAgent 地址
            wda_bundle_id: 自定义 WDA Bundle ID
            
        Returns:
            执行结果字典
        """
        logger.info(f"开始执行APP测试: test_case_id={test_case_id}, device={device_id}")
        
        original_cwd = os.getcwd()
        
        try:
            # 切换到项目根目录
            os.chdir(self.base_path)
            
            # 准备环境变量
            env = os.environ.copy()
            env['PYTHONPATH'] = self._build_pythonpath()
            env['DJANGO_SETTINGS_MODULE'] = 'backend.settings'
            # 强制子进程使用 UTF-8，避免 Windows 下 gbk 解码错误
            env['PYTHONUTF8'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            
            # 传递执行参数到 pytest
            env['APP_TEST_CASE_ID'] = str(test_case_id)
            env['APP_DEVICE_ID'] = device_id
            env['APP_PACKAGE_NAME'] = package_name
            env['APP_DEVICE_PLATFORM'] = str(platform or 'android')
            if wda_url:
                env['APP_IOS_WDA_URL'] = wda_url
            if wda_bundle_id:
                env['APP_IOS_WDA_BUNDLE_ID'] = wda_bundle_id
            is_ios_safari = str(package_name or '').strip().lower() in IOS_SAFARI_BUNDLE_IDS
            if str(platform or '').lower() == 'ios' and (not package_name or is_ios_safari):
                try:
                    from apps.app_automation.models import AppTestConfig
                    config = AppTestConfig.objects.first()
                    ios_browser_start_url = str(
                        os.environ.get('APP_IOS_BROWSER_START_URL')
                        or getattr(config, 'ios_browser_start_url', '')
                        or ''
                    ).strip()
                    if ios_browser_start_url:
                        env['APP_IOS_BROWSER_START_URL'] = ios_browser_start_url
                except Exception as exc:
                    logger.debug(f"读取 iOS 浏览器起始 URL 失败: {exc}")
            if execution_id:
                env['APP_EXECUTION_ID'] = str(execution_id)
            if username:
                env['APP_USERNAME'] = username
            
            # Allure 结果目录
            allure_results_dir = self._get_allure_results_dir(execution_id)
            self._prepare_allure_results_dir(allure_results_dir)
            
            # 只运行平台的 UI Flow 执行入口。不能收集整个 tests 目录，
            # 否则 Django/APITestCase 单元测试会切换到临时测试数据库，
            # 导致运行入口无法读取平台数据库中的真实用例。
            pytest_args = self._build_pytest_args(allure_results_dir)
            
            logger.info(f"执行命令: {' '.join(pytest_args)}")
            logger.info(f"工作目录: {os.getcwd()}")
            logger.info(f"PYTHONPATH: {env['PYTHONPATH']}")
            
            # 执行 pytest
            process = subprocess.Popen(
                pytest_args,
                cwd=self.base_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                bufsize=1,
                env=env,
                preexec_fn=os.setsid if os.name == 'posix' else None,
            )
            
            self._current_process = process
            
            # 准备日志文件
            log_file_path = self._get_log_file_path(username or 'unknown')
            
            # 收集输出并写入日志文件
            output_lines = []
            important_patterns = ['PASSED', 'FAILED', 'ERROR', 'SKIPPED', 'collected', 'passed', 'failed']
            stopped_by_request = False
            
            log_file = open(log_file_path, 'a', encoding='utf-8')
            try:
                log_file.write(f"\n{'='*80}\n")
                log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                              f"执行记录 ID: {execution_id}, 用例 ID: {test_case_id}, "
                              f"设备: {device_id}\n")
                log_file.write(f"{'='*80}\n")

                def write_output_line(raw_line: str) -> None:
                    line = raw_line.rstrip()
                    if not line:
                        return
                    output_lines.append(line)
                    log_file.write(line + '\n')
                    if any(pattern in line for pattern in important_patterns):
                        logger.info(f"[pytest] {line}")

                if process.stdout:
                    if os.name == 'posix':
                        while True:
                            if self._is_execution_stopped(execution_id):
                                stopped_by_request = True
                                log_file.write(
                                    "\n[用户已请求停止，等待 pytest 优雅退出以保留已执行步骤结果]\n"
                                )
                                log_file.flush()
                                break
                            ready, _, _ = select.select([process.stdout], [], [], 0.2)
                            if ready:
                                line = process.stdout.readline()
                                if line == '' and process.poll() is not None:
                                    break
                                write_output_line(line)
                            elif process.poll() is not None:
                                for line in process.stdout.readlines():
                                    write_output_line(line)
                                break
                    else:
                        for line in process.stdout:
                            write_output_line(line)

                # 停止宽限期：runner 每 0.2s 轮询 DB，发现 stopped 后抛
                # ExecutionStoppedError，pytest 正常 teardown 并写出 allure
                # result.json（整个 UI 流程是一个 pytest 测试，强杀 = 零结果）。
                # 宽限期内继续 drain stdout 防止管道阻塞；超时才强杀。
                if stopped_by_request:
                    grace_deadline = time.monotonic() + 15
                    while True:
                        if process.poll() is not None:
                            break
                        remaining = grace_deadline - time.monotonic()
                        if remaining <= 0:
                            log_file.write(
                                "\n[pytest 未在宽限期内退出，强制终止 pytest 进程组]\n"
                            )
                            log_file.flush()
                            self._terminate_process(process)
                            break
                        ready, _, _ = select.select([process.stdout], [], [], min(0.2, remaining))
                        if ready:
                            line = process.stdout.readline()
                            if line:
                                write_output_line(line)
                
                log_file.write(f"\n[执行完毕]\n")
            finally:
                log_file.close()
            
            logger.info(f"执行日志已保存: {log_file_path}")
            
            # 等待执行完成
            exit_code = process.wait()
            self._current_process = None
            
            logger.info(f"pytest 执行完成，退出码: {exit_code}")

            # 停止执行时仍需要解析已完成步骤的结果并生成报告，
            # 以便用户查看已执行部分的 Allure 报告（停止不等于无报告）。
            is_stopped = stopped_by_request or self._is_execution_stopped(execution_id)
            if is_stopped:
                logger.info(
                    f"检测到执行已停止，仍将基于已生成的 allure-results 解析结果并生成报告: "
                    f"execution_id={execution_id}"
                )

            # 解析测试结果
            expected_step_count = self._expected_ui_flow_step_count(
                test_case_id=test_case_id,
                platform=platform,
                package_name=package_name,
            )
            try:
                test_results = self._parse_allure_results(
                    allure_results_dir,
                    expected_step_count=expected_step_count,
                )
            except Exception as parse_error:
                logger.warning(
                    f"解析 allure-results 失败（停止场景允许继续）: "
                    f"execution_id={execution_id}, error={parse_error}"
                )
                test_results = {'total': 0, 'passed': 0, 'failed': 0, 'broken': 0, 'skipped': 0}

            # 生成 Allure 报告
            try:
                report_path = self._generate_allure_report(execution_id)
            except Exception as report_error:
                logger.warning(
                    f"生成 Allure 报告失败（停止场景允许继续）: "
                    f"execution_id={execution_id}, error={report_error}"
                )
                report_path = None

            result = {
                'success': (not is_stopped) and exit_code == 0,
                'exit_code': exit_code,
                'report_path': report_path,
                'test_results': test_results,
                'output': '\n'.join(output_lines[-50:]),  # 保留最后50行输出
            }
            if is_stopped:
                result['stopped'] = True
            return result
            
        except Exception as e:
            logger.error(f"执行测试失败: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
            }
        finally:
            os.chdir(original_cwd)

    @staticmethod
    def _is_execution_stopped(execution_id: Optional[int]) -> bool:
        if not execution_id:
            return False
        try:
            from ..models import AppTestExecution
            return AppTestExecution.objects.filter(
                id=execution_id,
                status='stopped',
            ).exists()
        except Exception as exc:
            logger.debug(f"检查执行停止状态失败: execution_id={execution_id}, error={exc}")
            return False

    def _build_pytest_args(self, allure_results_dir: str) -> list[str]:
        """构建仅包含 UI Flow 运行入口的 pytest 命令。"""
        return [
            sys.executable, '-m', 'pytest',
            self.RUNTIME_TEST_TARGET,
            '-s', '-v',
            '--alluredir', allure_results_dir,
            '--tb=short',
        ]

    @staticmethod
    def _prepare_allure_results_dir(allure_results_dir: str) -> None:
        """清理同一执行记录上次运行遗留的 Allure 原始结果。"""
        if os.path.isdir(allure_results_dir):
            shutil.rmtree(allure_results_dir)
        os.makedirs(allure_results_dir, exist_ok=True)
    
    def _get_log_file_path(self, username: str) -> str:
        """生成日志文件路径: logs/app_automation/{username}/{日期}.log"""
        today = datetime.now().strftime('%Y-%m-%d')
        log_dir = os.path.join(
            str(self.base_path), 'logs', 'app_automation', username
        )
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f'{today}.log')
    
    def _build_pythonpath(self) -> str:
        """构建 PYTHONPATH 环境变量"""
        python_path_parts = [
            str(self.base_path),
            os.path.join(str(self.base_path), 'apps'),
        ]
        
        # 添加 sys.path 中的路径
        for p in sys.path:
            if p and os.path.exists(str(p)) and str(p) not in python_path_parts:
                p_str = str(p)
                if 'site-packages' in p_str or not p_str.endswith('.exe'):
                    python_path_parts.append(p_str)
        
        return os.pathsep.join(python_path_parts)
    
    def _get_allure_results_dir(self, execution_id: Optional[int] = None) -> str:
        """获取 Allure 结果目录: media/app-automation/allure-results/"""
        base_dir = os.path.join(settings.MEDIA_ROOT, 'app-automation', 'allure-results')
        
        if execution_id:
            return os.path.join(base_dir, f'execution_{execution_id}')
        
        return base_dir
    
    def _get_allure_report_dir(self, execution_id: Optional[int] = None) -> str:
        """获取 Allure 报告目录: media/app-automation/allure-reports/"""
        base_dir = os.path.join(settings.MEDIA_ROOT, 'app-automation', 'allure-reports')
        
        if execution_id:
            return os.path.join(base_dir, f'execution_{execution_id}')
        
        return base_dir
    
    def _generate_allure_report(self, execution_id: Optional[int] = None) -> Optional[str]:
        """
        生成 Allure 报告
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            报告目录路径，失败返回 None
        """
        try:
            allure_results_dir = self._get_allure_results_dir(execution_id)
            report_dir = self._get_allure_report_dir(execution_id)
            
            # 确保目录存在
            os.makedirs(report_dir, exist_ok=True)
            
            # 查找 allure 命令
            allure_path = self._find_allure_command()
            
            if not allure_path:
                logger.warning("未找到 Allure 命令，跳过报告生成")
                return None
            
            # 生成报告
            cmd = [allure_path, 'generate', allure_results_dir, '-o', report_dir, '--clean']
            
            logger.info(f"生成 Allure 报告: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                logger.info(f"Allure 报告生成成功: {report_dir}")
                return report_dir
            else:
                logger.error(f"Allure 报告生成失败: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error("Allure 报告生成超时")
            return None
        except Exception as e:
            logger.error(f"生成 Allure 报告失败: {str(e)}", exc_info=True)
            return None
    
    def _find_allure_command(self) -> Optional[str]:
        """查找 Allure 命令。

        查找顺序：ALLURE_CLI 环境变量 → 项目内置 allure/bin → tools 挂载目录
        （docker-compose 将宿主机 ./tools 挂载到 /app/tools，内含 allure-2.x）。
        """
        import os
        import platform

        base_dir = Path(__file__).resolve().parent.parent.parent.parent

        candidates: List[Path] = []
        env_allure = os.environ.get('ALLURE_CLI')
        if env_allure:
            candidates.append(Path(env_allure))

        if platform.system() == 'Windows':
            candidates.append(base_dir / 'allure' / 'bin' / 'allure.bat')
        else:
            candidates.append(base_dir / 'allure' / 'bin' / 'allure')

        if platform.system() != 'Windows':
            tools_dir = base_dir / 'tools'
            if tools_dir.is_dir():
                for match in sorted(tools_dir.glob('allure-*/bin/allure')):
                    candidates.append(match)

        for candidate in candidates:
            if candidate.exists():
                logger.info(f"使用 Allure 命令: {candidate}")
                return str(candidate)

        logger.warning(
            "未找到 Allure 命令（已尝试 ALLURE_CLI 环境变量、allure/bin、tools/allure-*），"
            "跳过报告生成"
        )
        return None

    @staticmethod
    def _expected_ui_flow_step_count(
        test_case_id: int,
        platform: str = 'android',
        package_name: str = '',
    ) -> Optional[int]:
        """Return the number of steps that the runtime UI flow is expected to execute.

        Allure only writes steps that have started.  When a hard step failure
        stops the runner, the remaining steps are absent from the raw result;
        the immutable test case is therefore the source of truth for the
        missing tail.  Browser bootstrap steps are removed here with the same
        rule used by ``test_app_flow.py`` so setup is not counted twice.
        """
        try:
            from apps.app_automation.models import AppTestCase
            from apps.app_automation.utils.ios_flow import strip_ios_browser_bootstrap_steps

            test_case = AppTestCase.objects.get(id=test_case_id)
            raw_flow = test_case.ui_flow
            if isinstance(raw_flow, dict):
                steps = raw_flow.get('steps', [])
            elif isinstance(raw_flow, list):
                steps = raw_flow
            else:
                steps = []
            steps = list(steps) if isinstance(steps, list) else []

            browser_start_url = os.environ.get('APP_IOS_BROWSER_START_URL', '')
            if str(platform or '').lower() == 'ios' and browser_start_url:
                steps = strip_ios_browser_bootstrap_steps(
                    steps,
                    platform=platform,
                    package_name=package_name,
                    browser_start_url=browser_start_url,
                )
            return len(steps)
        except Exception as exc:
            logger.debug("读取 APP UI Flow 预期步骤数失败: %s", exc)
            return None

    @staticmethod
    def _allure_ui_flow_steps(data: Dict[str, Any]) -> Optional[list[Dict[str, Any]]]:
        """Extract the direct children of the runner's UI Flow Allure step."""
        for step in data.get('steps') or []:
            if not isinstance(step, dict):
                continue
            name = str(step.get('name') or '').strip()
            if name == '执行 UI Flow' or name.startswith('执行 UI Flow'):
                children = step.get('steps')
                return children if isinstance(children, list) else []
        return None
    
    def _parse_allure_results(
        self,
        results_dir: str,
        expected_step_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        解析 Allure 测试结果
        
        Args:
            results_dir: Allure 结果目录
            
        Returns:
            测试结果统计
        """
        try:
            # 查找所有测试结果文件
            result_files = glob.glob(os.path.join(results_dir, '*-result.json'))
            
            top_level_records = []
            ui_flow_step_records = []
            ui_flow_container_found = False

            logger.debug(f"解析Allure目录 {results_dir} ，找到文件: {result_files}")
            for result_file in result_files:
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        step_records = self._allure_ui_flow_steps(data)
                        if step_records is None:
                            top_level_records.append(data)
                        else:
                            ui_flow_container_found = True
                            ui_flow_step_records.extend(
                                item for item in step_records if isinstance(item, dict)
                            )

                except Exception as e:
                    logger.warning(f"解析结果文件失败: {result_file}, 错误: {e}")

            records = ui_flow_step_records if ui_flow_container_found else top_level_records
            total = len(records)
            passed = 0
            failed = 0
            skipped = 0
            broken = 0
            for record in records:
                status = str(record.get('status', '')).lower()
                if status == 'passed':
                    passed += 1
                elif status == 'broken':
                    broken += 1
                    failed += 1
                elif status == 'failed':
                    failed += 1
                elif status == 'skipped':
                    skipped += 1
                else:
                    failed += 1
                    logger.debug(
                        "未知测试状态 '%s' 视为失败: %s",
                        status,
                        record.get('name', '<unnamed>'),
                    )

            step_level = ui_flow_container_found
            if step_level and expected_step_count is not None:
                expected_step_count = max(0, int(expected_step_count))
                missing_steps = max(0, expected_step_count - total)
                skipped += missing_steps
                total = max(total, expected_step_count)
            
            logger.info(f"测试结果统计: 总数={total}, 通过={passed}, 失败={failed}, 跳过={skipped}, broken={broken}")
            
            return {
                'total': total,
                'passed': passed,
                'failed': failed,
                'skipped': skipped,
                'broken': broken,
                'step_level': step_level,
            }
            
        except Exception as e:
            logger.error(f"解析 Allure 结果失败: {str(e)}", exc_info=True)
            return {
                'total': 0,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
                'broken': 0,
            }
    
    def calculate_progress(self, execution_id: Optional[int] = None) -> int:
        """
        计算测试进度
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            进度百分比 (0-100)
        """
        try:
            results_dir = self._get_allure_results_dir(execution_id)
            
            if not os.path.exists(results_dir):
                return 0
            
            result_files = glob.glob(os.path.join(results_dir, '*-result.json'))
            
            if not result_files:
                return 0
            
            # 简单估算：每有一个结果文件，进度增加
            # 实际应用中可以根据总步骤数来计算
            file_count = len(result_files)
            progress = min(file_count * 10, 100)  # 假设有10个步骤
            
            return progress
            
        except Exception as e:
            logger.error(f"计算进度失败: {str(e)}")
            return 0
    
    def stop(self):
        """停止当前执行的测试"""
        if self._current_process:
            self._terminate_process(self._current_process)
            self._current_process = None

    def _terminate_process(self, process: subprocess.Popen) -> None:
        """Terminate a pytest process tree without killing the Celery worker."""
        try:
            if process.poll() is not None:
                return
            if os.name == 'posix':
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=5)
            logger.info("测试执行已停止")
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'posix':
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=3)
            except Exception as kill_exc:
                logger.error(f"强制终止测试失败: {kill_exc}")
            logger.warning("测试执行被强制终止")
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error(f"停止测试失败: {str(e)}")
