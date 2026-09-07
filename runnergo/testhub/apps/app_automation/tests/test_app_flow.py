# -*- coding: utf-8 -*-
"""
APP UI Flow 测试
"""
import os
import logging
import pytest
import allure
import json
from apps.app_automation.models import AppTestCase, AppTestExecution
from apps.core.models import RuntimeDataSet, RuntimeDataTemplate, RuntimeExecutionSnapshot
from apps.core.data_assets import release_assets_for_execution
from apps.core.runtime_case import RuntimeCase
from apps.core.runtime_orchestration import RuntimeContext, perform_runtime_cleanup
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation.utils.ios_flow import (
    ios_flow_should_start_from_home,
    ios_should_open_browser_start_url,
    strip_ios_browser_bootstrap_steps,
)
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner

logger = logging.getLogger(__name__)

def _execution_has_field(execution, field_name):
    if not execution:
        return False
    return any(field.name == field_name for field in execution._meta.fields)


def _execution_field_value(execution, field_name, default):
    if not _execution_has_field(execution, field_name):
        return default
    value = getattr(execution, field_name, default)
    return default if value is None else value


def _make_progress_callback(execution_id):
    """
    创建进度回调函数，在 pytest 子进程中直接更新数据库和 WebSocket。
    
    进度映射：步骤执行占整体的 10%~90%，前后留给环境准备和报告生成。
    """
    if not execution_id:
        return None
    
    def _send_ws(eid, status, progress, message, step_info=None):
        """发送 WebSocket 通知（失败不影响主流程）"""
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer:
                payload = {
                    "type": "execution_update",
                    "execution_id": int(eid),
                    "status": status,
                    "progress": progress,
                    "message": message,
                    "report_path": None,
                    "finished_at": None,
                }
                if step_info:
                    payload.update(step_info)
                async_to_sync(channel_layer.group_send)(
                    f"app_execution_{eid}",
                    payload
                )
        except Exception as e:
            logger.debug(f"WebSocket 通知失败: {e}")
    
    def callback(current_step, total_steps, step_name, status):
        """
        进度回调：每完成一个步骤更新进度。
        
        进度计算：10 + (current_step / total_steps) * 80
        即步骤全部完成时进度为 90%，留 10% 给报告生成阶段。
        """
        if total_steps <= 0:
            return
        
        completed_step = current_step if status in ('passed', 'failed') else max(current_step - 1, 0)
        progress = int(10 + (completed_step / total_steps) * 80)
        progress = min(progress, 90)  # 上限 90%，留给报告阶段
        
        status_text = {'running': '执行中', 'passed': '通过', 'failed': '失败'}.get(status, status)
        message = f"步骤 {current_step}/{total_steps}: {step_name} - {status_text}"
        step_info = {
            'current_step': current_step,
            'total_steps': total_steps,
            'step_name': step_name,
            'step_status': status,
        }
        
        try:
            execution = AppTestExecution.objects.filter(
                id=execution_id,
                status__in=['pending', 'running'],
            ).first()
            if not execution:
                return
            runtime_context = dict(execution.runtime_context or {})
            runtime_context['execution_progress'] = {
                **step_info,
                'message': message,
            }
            updated = AppTestExecution.objects.filter(
                id=execution_id,
                status__in=['pending', 'running'],
            ).update(progress=progress, runtime_context=runtime_context)
        except Exception as e:
            logger.debug(f"更新执行进度失败: {e}")
            return

        # A stopped execution must never be pushed back to "running" by a late callback.
        if updated:
            _send_ws(execution_id, 'running', progress, message, step_info)
    
    return callback


@allure.feature("APP自动化测试")
class AppFlowTests:
    """APP UI Flow 测试类"""
    
    @pytest.fixture(scope="class")
    def airtest(
        self,
        device_id,
        device_platform,
        ios_wda_url,
        ios_wda_bundle_id,
        username,
        django_db_blocker,
    ):
        """Airtest 基础环境"""
        with django_db_blocker.unblock():
            airtest_base = AirtestBase(
                device_id=device_id,
                username=username,
                platform=device_platform,
                wda_url=ios_wda_url,
                wda_bundle_id=ios_wda_bundle_id,
            )

            # 设置环境
            if not airtest_base.setup_airtest():
                pytest.fail("Airtest 环境设置失败")
        
        yield airtest_base
        
        # 清理
        airtest_base.teardown_airtest()
    
    @allure.story("执行UI Flow")
    def test_execute_ui_flow(
        self,
        test_case_id,
        package_name,
        execution_id,
        airtest,
        username,
        ios_browser_start_url,
    ):
        """执行 UI Flow 测试"""
        # 获取测试用例
        test_case = AppTestCase.objects.get(id=test_case_id)
        execution = AppTestExecution.objects.filter(id=execution_id).first() if execution_id else None
        allure.dynamic.title(f"用例名称: {test_case.name}")
        allure.dynamic.suite("APP自动化测试")

        runner = UiFlowRunner(
            username=username,
            execution_id=execution_id,
            device_id=airtest.device_id or '',
            package_name=package_name or '',
            platform=airtest.platform,
        )
        runner.ensure_not_stopped(force=True)

        execution_runtime_context = _execution_field_value(execution, 'runtime_context', {})
        runtime_context = RuntimeContext(execution_runtime_context)
        runtime_case = RuntimeCase(
            test_case.ui_flow,
            _execution_field_value(execution, 'runtime_override', []),
        ).build(context=runtime_context)
        if execution and _execution_has_field(execution, 'runtime_data'):
            execution.runtime_data = runtime_case.execution_data
            execution.save(update_fields=['runtime_data', 'updated_at'])

        allure.attach(
            json.dumps(runtime_case.execution_data, ensure_ascii=False, indent=2),
            name="实际执行数据",
            attachment_type=allure.attachment_type.JSON,
        )

        if isinstance(runtime_case.case_data, list):
            ui_flow = runtime_case.case_data
        elif isinstance(runtime_case.case_data, dict):
            ui_flow = runtime_case.case_data.get('steps', [])
        else:
            ui_flow = []
        variables = test_case.variables or []

        browser_start_url = str(ios_browser_start_url or '').strip()
        should_open_ios_browser_url = ios_should_open_browser_start_url(
            airtest.platform,
            package_name,
            ui_flow,
            browser_start_url,
        )

        if should_open_ios_browser_url:
            with allure.step(f"打开 iOS 浏览器起始页: {browser_start_url}"):
                assert airtest.open_url(browser_start_url), f"iOS 打开浏览器起始页失败: {browser_start_url}"
                runner.ensure_not_stopped(force=True)
            ui_flow = strip_ios_browser_bootstrap_steps(
                ui_flow,
                platform=airtest.platform,
                package_name=package_name,
                browser_start_url=browser_start_url,
            )
        elif package_name:
            with allure.step(f"启动应用: {package_name}"):
                assert airtest.open_app(package_name), f"应用启动失败: {package_name}"
                runner.ensure_not_stopped(force=True)
        else:
            allure.attach(
                "未配置应用包名，跳过启动应用步骤",
                name="启动应用",
                attachment_type=allure.attachment_type.TEXT
            )
            if (
                str(airtest.platform or '').lower() == 'ios'
                and ios_flow_should_start_from_home(ui_flow)
            ):
                with allure.step("回到 iOS 主屏幕"):
                    assert airtest.go_home(), "iOS 回到主屏幕失败"
                    runner.ensure_not_stopped(force=True)
        
        # 创建进度回调
        progress_callback = _make_progress_callback(execution_id)
        
        try:
            with allure.step("执行 UI Flow"):
                result = runner.run(
                    ui_flow=ui_flow,
                    variables=variables,
                    runtime={
                        'stop_on_error': True,
                        'runtime_context': execution_runtime_context,
                    },
                    progress_callback=progress_callback
                )
        finally:
            runner.close()

        if execution and _execution_has_field(execution, 'runtime_context'):
            execution.runtime_context = result.get('context') or execution.runtime_context or {}
            runtime_template_id = _execution_field_value(execution, 'runtime_template_id', None)
            runtime_dataset_id = _execution_field_value(execution, 'runtime_dataset_id', None)
            snapshot = RuntimeExecutionSnapshot.objects.create(
                target_type='app_automation',
                target_case_id=test_case.id,
                target_case_name=test_case.name,
                execution_type='app_automation',
                execution_id=str(execution.id),
                template=RuntimeDataTemplate.objects.filter(id=runtime_template_id).first()
                if runtime_template_id else None,
                dataset=RuntimeDataSet.objects.filter(id=runtime_dataset_id).first()
                if runtime_dataset_id else None,
                dataset_row_index=_execution_field_value(execution, 'runtime_dataset_row_index', None),
                runtime_override=_execution_field_value(execution, 'runtime_override', []),
                runtime_data=_execution_field_value(execution, 'runtime_data', []),
                runtime_context=execution.runtime_context or {},
                cleanup_config=_execution_field_value(execution, 'cleanup_config', {}),
                created_by=execution.user,
            )
            cleaned_context, cleanup_result = perform_runtime_cleanup(
                execution.runtime_context,
                _execution_field_value(execution, 'cleanup_config', {}),
            )
            cleanup_result['dataAssets'] = release_assets_for_execution(
                'app_automation',
                execution.id,
                cleanup_result=cleanup_result,
            )
            execution.runtime_context = cleaned_context
            execution.save(update_fields=['runtime_context', 'updated_at'])
            snapshot.cleanup_result = cleanup_result
            snapshot.save(update_fields=['cleanup_result'])
        
        # 验证结果
        with allure.step("验证执行结果"):
            assert result['failed'] == 0, f"UI Flow 执行失败，失败步骤: {result['failed']}"
            assert result['passed'] > 0, "没有执行任何步骤"
        
        allure.attach(
            f"总步骤: {result['total']}\n"
            f"通过: {result['passed']}\n"
            f"失败: {result['failed']}",
            name="执行统计",
            attachment_type=allure.attachment_type.TEXT
        )
