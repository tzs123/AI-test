# -*- coding: utf-8 -*-
"""
UI Flow 执行器 - 将 UI Flow JSON 转换为 Airtest 动作并执行
"""
import os
import time
import logging
import json
import re
import copy
import random
import math
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from django.conf import settings
from apps.core.runtime_orchestration import RuntimeContext

from airtest.core.api import (
    Template,
    touch,
    swipe,
    snapshot,
    exists,
    double_click,
    G,
    ST,
    text as airtest_text,
)

from ..utils.locator_helpers import (
    airtest_record_position,
    fingerprint_is_replay_safe,
    normalize_locator_strategies,
    parse_resolution,
    parse_ui_hierarchy,
    region_center,
    scale_point,
    scale_region,
    select_best_ui_node,
    strategy_fingerprint,
)
from ..utils.popup_selector import select_popup_text_path

# 导入 OCR 工具
try:
    from ..utils.ocr_helper import get_ocr_helper
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import allure
    ALLURE_AVAILABLE = True
except ImportError:
    ALLURE_AVAILABLE = False

logger = logging.getLogger(__name__)

CHROME_PACKAGE = 'com.android.chrome'
CHROME_TOOLBAR_RESOURCE_IDS = {
    'com.android.chrome:id/home_button',
    'com.android.chrome:id/url_bar',
    'com.android.chrome:id/optional_toolbar_button',
    'com.android.chrome:id/tab_switcher_button',
    'com.android.chrome:id/menu_button',
}

IOS_PHOTO_PICKER_DONE_RESOURCE_IDS = {
    'PUOneUpBarButtonItemIdentifierAssetExplorerReviewScreenDone',
}
IOS_PHOTO_UPLOAD_LOADING_KEYWORDS = (
    '加载中', '上传中', '正在上传', '识别中', '处理中',
)
IOS_SAFARI_ADDRESS_FIELD_RESOURCE_IDS = {
    'TabBarItemTitle',
}
DEFAULT_TERMINAL_WEBVIEW_PATHS = ('/result',)
APP_ASSERTION_ACTIONS = frozenset({
    'assert',
    'assert_exists',
    'assert_text',
    'assert_image',
    'assert_page',
    'assert_activity',
    'foreach_assert',
    'visual_assert',
})
FORM_SELECT_PLACEHOLDER_LABELS = (
    '申请地区', '贷款用途', '婚姻状况', '学历', '居住情况',
    '职业类型', '单位地址',
)
FORM_SELECT_ACTION_LABELS = {'确认', '取消', '确定', '完成', 'Done', 'Cancel'}

LICENSE_PLATE_PROVINCE_LAYOUT = (
    ('京', '沪', '粤', '津', '冀', '豫', '云', '辽', '黑', '湘'),
    ('皖', '鲁', '苏', '浙', '赣', '鄂', '桂', '甘', '晋', '陕'),
    ('蒙', '吉', '闽', '贵', '渝', '川', '青', '琼', '宁', '挂'),
)
LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS = {
    '藏': 2,
    '港': 3,
    '澳': 4,
    '新': 5,
    '使': 6,
    '学': 7,
}
LICENSE_PLATE_ALPHANUMERIC_LAYOUT = (
    ('1234567890', 0.0),
    ('QWERTYUIOP', 0.0),
    ('ASDFGHJKL', 0.5),
    ('ZXCVBNM', 1.5),
)
LICENSE_PLATE_SPECIAL_TAIL = frozenset('挂学警港澳使领')

DEFAULT_SEMANTIC_PAGE_RULES = (
    {
        'name': 'security_verify',
        'label': '安全验证页',
        'keywords': [
            '滑块验证', '安全验证', '请完成验证', '向右滑动',
            'captcha', 'robot verification', 'Yoda',
        ],
        'activities': ['YodaRouterTransparentActivity'],
        'min_score': 2,
    },
    {
        'name': 'login',
        'label': '登录页',
        'keywords': ['登录', '手机号', '账号', '密码', '验证码', 'sign in', 'log in'],
        'min_score': 2,
    },
    {
        'name': 'payment',
        'label': '支付页',
        'keywords': ['支付', '付款', '支付密码', '确认支付', '收银台', 'pay', 'checkout'],
        'min_score': 2,
    },
    {
        'name': 'home',
        'label': '首页',
        'keywords': ['首页', '主页', '推荐', '消息', '我的', 'home'],
        'min_score': 2,
    },
)


class ExecutionStoppedError(RuntimeError):
    """Raised when an APP automation execution is cancelled by the user."""


class UiFlowRunner:
    """将 ui_flow 转换为 Airtest 动作并执行"""
    
    def __init__(self, image_base_dir: Optional[str] = None, username: Optional[str] = None,
                 execution_id: Optional[int] = None, device_id: str = '',
                 package_name: str = '', platform: str = 'android'):
        """
        初始化 UiFlowRunner
        
        Args:
            image_base_dir: 图片元素基础目录
            username: 执行用户名，用于截图目录分组
            execution_id: 执行记录 ID，用于将截图归档到本次执行目录
        """
        if image_base_dir:
            self.image_base_dir = image_base_dir
        else:
            # 使用统一的 Template 目录作为图片基础目录
            self.image_base_dir = os.path.join(settings.BASE_DIR, 'apps', 'app_automation', 'Template')
        
        # 截图保存目录: media/app-automation/screenshots/{username}/
        screenshot_parts = [
            settings.MEDIA_ROOT, 'app-automation', 'screenshots', username or 'unknown'
        ]
        if execution_id:
            screenshot_parts.append(f'execution_{execution_id}')
        self.screenshots_dir = os.path.join(*screenshot_parts)
        
        os.makedirs(self.image_base_dir, exist_ok=True)
        os.makedirs(self.screenshots_dir, exist_ok=True)
        
        # 上下文变量
        self.context: Dict[str, Any] = {
            'global': {},
            'local': {},
            'outputs': {},
            'assertions': [],
        }
        
        # 运行时配置
        self.runtime: Dict[str, Any] = {
            'retry_times': 3,
            'retry_interval': 0.5,
            'locator_timeout': 5,
            'wait_for_stable': True,
            'page_stable_timeout': 1.2,
            'page_stable_interval': 0.2,
            'page_stable_threshold': 0.018,
            'page_stable_samples': 2,
            'semantic_locator_timeout': 0.6,
            'photo_upload_settle_timeout': 12.0,
            'photo_upload_initial_delay': 0.6,
            'photo_upload_poll_interval': 0.2,
            'stop_on_terminal_webview_path': True,
            'terminal_webview_paths': list(DEFAULT_TERMINAL_WEBVIEW_PATHS),
            'terminal_webview_check_timeout': 1.0,
        }
        
        # OCR 工具（延迟初始化）
        self._ocr_helper = None
        self._last_locator_diagnostics: Dict[str, Any] = {}
        self._last_webview_diagnostics: Dict[str, Any] = {}
        self._counted_element_ids = set()
        self.execution_id = execution_id
        self.device_id = str(device_id or '').strip()
        self.package_name = str(package_name or '').strip()
        self.platform = str(platform or 'android').strip().lower()
        self._appium_webview = None
        self._last_ios_recorded_input_target_config: Optional[Dict[str, Any]] = None
        self._ios_pending_photo_upload_slot = ''
        self._last_page_nodes: List[Dict[str, Any]] = []
        self._last_stop_check_at = 0.0
        self._stop_check_interval = 0.2
        self._nested_assertion_failures = 0
        
        logger.info(f"初始化UiFlowRunner，图片目录: {self.image_base_dir}")

    def ensure_not_stopped(self, force: bool = False) -> None:
        """Abort promptly when the execution has been marked as stopped."""
        if not self.execution_id:
            return

        now = time.monotonic()
        if not force and now - self._last_stop_check_at < self._stop_check_interval:
            return
        self._last_stop_check_at = now

        try:
            from ..models import AppTestExecution
            status_value = (
                AppTestExecution.objects.filter(id=self.execution_id)
                .values_list('status', flat=True)
                .first()
            )
        except Exception as exc:
            # A transient database read failure must not turn a test into a false cancellation.
            logger.debug(f"检查执行停止状态失败: execution_id={self.execution_id}, error={exc}")
            return

        if status_value == 'stopped':
            raise ExecutionStoppedError(f"执行 #{self.execution_id} 已被用户停止")

    def _sleep_interruptibly(self, seconds: Any) -> None:
        """Sleep in short slices so a stop request is observed without waiting for the full delay."""
        try:
            duration = max(0.0, float(seconds))
        except (TypeError, ValueError):
            duration = 0.0

        deadline = time.monotonic() + duration
        self.ensure_not_stopped(force=True)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self._stop_check_interval, remaining))
            self.ensure_not_stopped(force=True)
    
    def run(
        self,
        ui_flow: List[Dict[str, Any]],
        variables: Optional[List[Dict[str, Any]]] = None,
        runtime: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        执行 UI Flow
        
        Args:
            ui_flow: UI Flow 配置列表
            variables: 变量列表
            runtime: 运行时配置
            progress_callback: 进度回调函数，签名为 callback(current_step, total_steps, step_name, status)
                其中 status 为 'running' | 'passed' | 'failed'
            
        Returns:
            执行结果字典
        """
        if not isinstance(ui_flow, list):
            raise ValueError("ui_flow 必须是列表")

        self.ensure_not_stopped(force=True)
        
        # 初始化上下文
        self._init_context(variables, runtime)
        
        # 执行所有步骤
        total_steps = len(ui_flow)
        passed_steps = 0
        failed_steps = 0
        self._nested_assertion_failures = 0
        
        logger.info(f"开始执行 UI Flow，共 {total_steps} 个步骤")
        
        for idx, step in enumerate(ui_flow, 1):
            self.ensure_not_stopped(force=True)
            step_name = step.get('name', step.get('type', 'unknown'))
            
            # 通知：步骤开始执行
            if progress_callback:
                try:
                    progress_callback(idx, total_steps, step_name, 'running')
                except Exception as cb_err:
                    logger.debug(f"进度回调失败: {cb_err}")
            
            try:
                logger.info(f"执行步骤 {idx}/{total_steps}: {step_name}")
                step_title = f"步骤{idx}-{step_name}"
                is_assertion = self._is_assertion_step(step)
                if ALLURE_AVAILABLE:
                    with allure.step(step_title):
                        try:
                            self._execute_step(step)
                            if is_assertion:
                                self._record_assertion_result(step, True, step_index=idx)
                            self._attach_allure(f"{step_title}-passed", step)
                        except ExecutionStoppedError:
                            raise
                        except Exception:
                            self._attach_allure(f"{step_title}-failed", step)
                            raise
                else:
                    self._execute_step(step)
                    if is_assertion:
                        self._record_assertion_result(step, True, step_index=idx)
                passed_steps += 1
                
                # 通知：步骤执行成功
                if progress_callback:
                    try:
                        progress_callback(idx, total_steps, step_name, 'passed')
                    except Exception as cb_err:
                        logger.debug(f"进度回调失败: {cb_err}")

                terminal_state = self._terminal_webview_page_state()
                if terminal_state.get('matched'):
                    failed_steps += self._nested_assertion_failures
                    assertion_summary = self._assertion_summary()
                    skipped_steps = total_steps - idx
                    self.context['outputs']['terminal_page'] = terminal_state
                    logger.info(
                        "检测到终止结果页，停止后续 UI Flow 步骤: step=%s/%s, skipped=%s, state=%s",
                        idx,
                        total_steps,
                        skipped_steps,
                        terminal_state,
                    )
                    return {
                        'total': total_steps,
                        'passed': passed_steps,
                        'failed': failed_steps,
                        'skipped': skipped_steps,
                        'terminated': True,
                        'terminal_page': terminal_state,
                        'outputs': self.context.get('outputs', {}),
                        'context': copy.deepcopy(self.context),
                        'assertions': copy.deepcopy(self.context.get('assertions', [])),
                        **assertion_summary,
                    }
                        
            except ExecutionStoppedError:
                logger.info(f"执行已停止: execution_id={self.execution_id}")
                raise
            except Exception as e:
                logger.error(f"步骤 {idx} 执行失败: {str(e)}", exc_info=True)
                failed_steps += 1
                if is_assertion:
                    self._record_assertion_result(step, False, error=e, step_index=idx)
                self._save_locator_diagnostics(idx, step_name)
                if not ALLURE_AVAILABLE:
                    self._capture_screenshot(
                        f"步骤{idx}-{step.get('name', step.get('type', 'unknown'))}-failed"
                    )
                
                # 通知：步骤执行失败
                if progress_callback:
                    try:
                        progress_callback(idx, total_steps, step_name, 'failed')
                    except Exception as cb_err:
                        logger.debug(f"进度回调失败: {cb_err}")
                
                # 断言只记录结果，不阻断后续步骤；普通动作仍遵守 stop_on_error。
                if not is_assertion and runtime and runtime.get('stop_on_error', False):
                    raise
        
        failed_steps += self._nested_assertion_failures
        assertion_summary = self._assertion_summary()
        logger.info(f"UI Flow 执行完成，通过: {passed_steps}，失败: {failed_steps}")
        
        return {
            'total': total_steps,
            'passed': passed_steps,
            'failed': failed_steps,
            'skipped': 0,
            'terminated': False,
            'outputs': self.context.get('outputs', {}),
            'context': copy.deepcopy(self.context),
            'assertions': copy.deepcopy(self.context.get('assertions', [])),
            **assertion_summary,
        }

    @staticmethod
    def _is_assertion_step(step: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(step, dict):
            return False
        action = str(step.get('type') or step.get('action') or '').strip().lower()
        return action in APP_ASSERTION_ACTIONS

    def _record_assertion_result(
        self,
        step: Dict[str, Any],
        matched: bool,
        error: Optional[Exception] = None,
        step_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Persist an assertion result without changing the source UI flow."""
        action = str(step.get('type') or step.get('action') or '').strip().lower()
        record = {
            'step_id': str(step.get('id') or ''),
            'step_index': step_index,
            'name': str(step.get('name') or action or '断言'),
            'action': action,
            'assertion_type': str(step.get('assert_type') or action),
            'matched': bool(matched),
            'assertion_matched': bool(matched),
            'status': 'passed' if matched else 'failed',
            'message': '' if matched else str(error or '断言未命中'),
        }
        assertions = self.context.setdefault('assertions', [])
        assertions.append(record)
        self.context.setdefault('outputs', {})['last_assertion'] = record
        self.context['outputs']['assertion_summary'] = self._assertion_summary()
        return record

    def _assertion_summary(self) -> Dict[str, int]:
        assertions = self.context.get('assertions') or []
        return {
            'assertion_total': len(assertions),
            'assertion_passed': sum(1 for item in assertions if item.get('matched') is True),
            'assertion_failed': sum(1 for item in assertions if item.get('matched') is False),
        }
    
    def _init_context(self, variables: Optional[List[Dict[str, Any]]] = None, runtime: Optional[Dict[str, Any]] = None):
        """初始化上下文"""
        self.context = {
            'global': {},
            'local': {},
            'outputs': {},
            'assertions': [],
            'data': {},
            'dataAssets': {},
            'smartData': {},
        }
        
        if runtime:
            self.runtime.update(runtime)
            runtime_context = runtime.get('context') or runtime.get('runtime_context') or {}
            if isinstance(runtime_context, dict):
                for scope in ('global', 'local', 'outputs', 'data', 'dataAssets', 'smartData'):
                    if isinstance(runtime_context.get(scope), dict):
                        self.context[scope].update(runtime_context[scope])
                if isinstance(runtime_context.get('variables'), dict):
                    self.context['local'].update(runtime_context['variables'])
        
        if variables:
            self._load_variables(variables)
    
    def _load_variables(self, variables: List[Dict[str, Any]]):
        """加载变量到上下文"""
        for item in variables:
            if not isinstance(item, dict):
                continue
            
            name = item.get('name')
            if not name:
                continue
            
            scope = str(item.get('scope', 'local')).lower()
            value = item.get('value')
            
            self._set_variable(name, value, scope)
    
    def _set_variable(self, name: str, value: Any, scope: str = 'local'):
        """设置变量"""
        if scope not in self.context:
            scope = 'local'
        self.context[scope][name] = value
    
    def _get_variable(self, name: str, scope: Optional[str] = None) -> Any:
        """获取变量"""
        if scope:
            return self.context.get(scope, {}).get(name)
        
        # 按优先级查找：local -> data -> dataAssets -> smartData -> global -> outputs
        for s in ['local', 'data', 'dataAssets', 'smartData', 'global', 'outputs']:
            if name in self.context.get(s, {}):
                return self.context[s][name]
        
        return None
    
    def _render_value(self, value: Any) -> Any:
        """渲染变量值"""
        if isinstance(value, str):
            context = RuntimeContext(self.context)
            value = context.resolve(value)
            pattern = re.compile(r'\{\{\s*([^}]+)\s*\}\}')
            
            def replace_var(match):
                var_name = match.group(1)
                var_value = self._get_variable(var_name.strip())
                return str(var_value) if var_value is not None else ''
            
            return pattern.sub(replace_var, value)
        
        elif isinstance(value, dict):
            return {k: self._render_value(v) for k, v in value.items()}
        
        elif isinstance(value, list):
            return [self._render_value(item) for item in value]
        
        return value
    
    def _execute_step(self, step: Dict[str, Any]):
        """执行单个步骤，支持基础组件和自定义组件"""
        self.ensure_not_stopped(force=True)
        self._last_locator_diagnostics = {}
        self._last_webview_diagnostics = {}
        # 使用 type 字段获取步骤类型
        action_type = step.get('type', '')
        action = action_type.lower() if action_type else ''
        
        # 渲染步骤参数
        step = self._render_value(step)
        
        # 将 config 中的字段合并到顶层，便于各动作方法直接读取
        # 前端保存的数据结构为 { type, name, config: { selector_type, selector, ... } }
        config = step.get('config')
        if isinstance(config, dict):
            for key, value in config.items():
                if key not in step:
                    step[key] = value
        
        # ---- 步骤级重试机制 ----
        retry_times = int(step.get('retry_times', 0))
        retry_interval = float(step.get('retry_interval', self.runtime.get('retry_interval', 0.5)))
        
        if retry_times > 0:
            last_error = None
            for attempt in range(1 + retry_times):
                try:
                    self._dispatch_step(step, action)
                    return  # 成功则直接返回
                except Exception as e:
                    last_error = e
                    if attempt < retry_times:
                        logger.warning(
                            f"步骤 '{step.get('name', action)}' 第 {attempt + 1} 次失败，"
                            f"{retry_interval}s 后重试 (剩余 {retry_times - attempt - 1} 次): {e}"
                        )
                        self._sleep_interruptibly(retry_interval)
            raise last_error
        else:
            self._dispatch_step(step, action)

    def _dispatch_step(self, step: Dict[str, Any], action: str):
        """分发步骤到对应的 handler（基础组件）或展开执行（自定义组件）"""
        
        # 自定义组件展开执行
        if step.get('kind') == 'custom':
            self._execute_custom_component(step)
            return
        
        # 根据动作类型执行
        action_map = {
            # 页面控制
            'launch_app': self._action_launch_app,
            'close_app': self._action_close_app,
            'restart_app': self._action_restart_app,
            'clear_data': self._action_clear_data,
            'back': self._action_back,
            'home': self._action_home,
            'press_enter': self._action_press_enter,
            'enter': self._action_press_enter,
            'enter_key': self._action_press_enter,
            'wait_page': self._action_wait_page,
            'wait_element': self._action_wait_element,

            # 基础动作
            'click': self._action_click,
            'touch': self._action_click,
            'input': self._action_input,
            'fill': self._action_input,
            'type': self._action_input,
            'send_keys': self._action_input,
            'input_text': self._action_input,
            'clear_text': self._action_clear_text,
            'smart_click': self._action_smart_click,
            'smart_input': self._action_smart_input,
            'checkbox_toggle': self._action_checkbox_toggle,
            'element_probe': self._action_element_probe,
            'swipe': self._action_swipe,
            'pull_refresh': self._action_pull_refresh,
            'license_plate_input': self._action_license_plate_input,
            'bank_card_input': self._action_bank_card_input,
            'money_input': self._action_money_input,
            'short_video_feed': self._action_short_video_feed,
            'video_feed': self._action_short_video_feed,
            'wait_video_play': self._action_wait_video_play,
            'watch_video': self._action_watch_video,
            'random_action': self._action_random_action,
            'double_click': self._action_double_click,
            'long_press': self._action_long_press,
            'drag': self._action_drag,
            'swipe_to': self._action_swipe_to,
            
            # 条件动作
            'image_exists_click': self._action_image_exists_click,
            'image_exists_click_chain': self._action_image_exists_click_chain,
            
            # 工具类
            'set_variable': self._action_set_variable,
            'unset_variable': self._action_unset_variable,
            'extract_output': self._action_extract_output,
            'screenshot': self._action_screenshot,
            'record_video': self._action_record_video,
            'api_request': self._action_api_request,

            # H5 / WebView
            'switch_native': self._action_switch_native,
            'switch_webview': self._action_switch_webview,
            'click_web_element': self._action_click_web_element,
            'input_web_text': self._action_input_web_text,
            'execute_js': self._action_execute_js,
            'scroll_web_page': self._action_scroll_web_page,

            # AI 感知
            'page_detect': self._action_page_detect,
            'visual_locate': self._action_visual_locate,
            'visual_click': self._action_visual_click,
            'visual_assert': self._action_visual_assert,
            'self_heal_click': self._action_self_heal_click,
            'self_heal_input': self._action_self_heal_input,

            # 页面交互复合组件
            'login': self._action_login,
            'fill_form': self._action_fill_form,
            'close_popup': self._action_close_popup,
            'popup_select_text': self._action_popup_select_text,
            'popup_pick': self._action_popup_select_text,
            'popup_select': self._action_popup_select_text,
            'scroll_list': self._action_scroll_list,

            # 电商复合组件
            'browse_product': self._action_browse_product,
            'search_product': self._action_search_product,
            'open_product_detail': self._action_open_product_detail,
            'add_cart': self._action_add_cart,
            'create_order': self._action_create_order,
            'payment_flow': self._action_payment_flow,

            # 金融复合组件
            'verify_code': self._action_verify_code,
            'identity_verify': self._action_identity_verify,
            'risk_popup_handle': self._action_risk_popup_handle,

            # 短视频互动组件
            'like_video': self._action_like_video,
            'favorite_video': self._action_favorite_video,
            'comment_video': self._action_comment_video,
            'follow_user': self._action_follow_user,
            'upload_video': self._action_upload_video,
            'live_room': self._action_live_room,

            # 社交复合组件
            'send_message': self._action_send_message,
            'search_user': self._action_search_user,
            'publish_post': self._action_publish_post,
            'upload_image': self._action_upload_image,
            'share_content': self._action_share_content,
            'comment': self._action_comment,
            
            # 控制流
            'wait': self._action_wait,
            'sleep': self._action_wait,
            'if': self._action_if,
            'loop': self._action_loop,
            'sequence': self._action_sequence,
            'try': self._action_try,
            
            # 断言
            'assert': self._action_assert,
            'assert_exists': self._action_assert_exists,
            'assert_text': self._action_assert_text,
            'assert_image': self._action_assert_image,
            'assert_page': self._action_assert_page,
            'assert_activity': self._action_assert_activity,
            'foreach_assert': self._action_foreach_assert,
        }
        
        handler = action_map.get(action)
        if handler:
            handler(step)
        else:
            # action_map 中找不到，尝试作为自定义组件查找
            if self._try_execute_as_custom(step, action):
                return
            logger.warning(f"未知的动作类型: {action} (步骤: {step.get('name', 'unknown')})")

    # ---------- 自定义组件展开 ----------

    def _load_custom_component_defs(self) -> Dict[str, Any]:
        """从数据库加载所有启用的自定义组件定义，缓存到实例上"""
        if not hasattr(self, '_custom_defs_cache'):
            try:
                from ..models import AppCustomComponent
                defs = {}
                for comp in AppCustomComponent.objects.filter(enabled=True):
                    defs[comp.type] = {
                        'name': comp.name,
                        'steps': comp.steps or [],
                        'schema': comp.schema or {},
                        'default_config': comp.default_config or {},
                    }
                self._custom_defs_cache = defs
                logger.debug(f"已加载 {len(defs)} 个自定义组件定义")
            except Exception as e:
                logger.warning(f"加载自定义组件定义失败: {e}")
                self._custom_defs_cache = {}
        return self._custom_defs_cache

    def _execute_custom_component(self, step: Dict[str, Any]):
        """
        展开并执行自定义组件。
        自定义组件的 steps 是基础组件步骤列表，逐个执行。
        步骤中的参数可通过 config 覆盖默认值。
        """
        comp_type = step.get('type', '')
        comp_name = step.get('name', comp_type)
        
        # 优先从步骤自带的 steps 字段获取（前端可能直接带了）
        sub_steps = step.get('steps')
        
        if not sub_steps:
            # 从数据库加载
            defs = self._load_custom_component_defs()
            comp_def = defs.get(comp_type)
            if not comp_def:
                raise ValueError(f"自定义组件 '{comp_type}' 未找到，请检查是否已创建并启用")
            sub_steps = comp_def.get('steps', [])
        
        if not sub_steps or not isinstance(sub_steps, list):
            logger.warning(f"自定义组件 '{comp_name}' 没有步骤，跳过")
            return
        
        # 深拷贝，避免修改原始定义
        sub_steps = copy.deepcopy(sub_steps)
        
        # 将自定义组件的 config 参数注入到子步骤中（作为变量可渲染）
        comp_config = step.get('config', {})
        if isinstance(comp_config, dict):
            for key, value in comp_config.items():
                if key not in ('type', 'name', 'kind', 'steps'):
                    self._set_variable(key, value, 'local')
        
        logger.info(f"展开自定义组件 '{comp_name}' ({comp_type})，共 {len(sub_steps)} 个子步骤")
        
        for sub_idx, sub_step in enumerate(sub_steps, 1):
            sub_name = sub_step.get('name', sub_step.get('type', 'unknown'))
            logger.info(f"  自定义组件子步骤 {sub_idx}/{len(sub_steps)}: {sub_name}")
            try:
                self._execute_step(sub_step)
            except ExecutionStoppedError:
                raise
            except Exception as exc:
                if not self._is_assertion_step(sub_step):
                    raise
                self._record_assertion_result(sub_step, False, error=exc)
                self._nested_assertion_failures += 1
                logger.error(
                    "自定义组件子步骤断言未命中，继续后续子步骤: %s: %s",
                    sub_name,
                    exc,
                )
                continue
            if self._is_assertion_step(sub_step):
                self._record_assertion_result(sub_step, True)

    def _try_execute_as_custom(self, step: Dict[str, Any], action: str) -> bool:
        """尝试将未知的 action_type 作为自定义组件执行，成功返回 True"""
        defs = self._load_custom_component_defs()
        if action in defs:
            step['kind'] = 'custom'
            self._execute_custom_component(step)
            return True
        return False

    def _safe_filename(self, text: str) -> str:
        safe = re.sub(r'[^0-9a-zA-Z_\-]+', '_', str(text))
        return safe.strip('_') or "step"

    def _capture_screenshot(self, name_prefix: str) -> Optional[str]:
        try:
            filename = f"{self._safe_filename(name_prefix)}_{time.time_ns()}.png"
            full_path = os.path.join(self.screenshots_dir, filename)
            result = snapshot(filename=full_path)
            if isinstance(result, dict):
                path = result.get("screen")
                if path and os.path.exists(path):
                    return path
            if os.path.exists(full_path):
                return full_path
        except Exception:
            logger.exception("截图失败")
        return None

    def _save_locator_diagnostics(self, step_index: int, step_name: str) -> Optional[str]:
        if not self._last_locator_diagnostics:
            return None
        try:
            filename = f"locator_{step_index}_{self._safe_filename(step_name)}_{time.time_ns()}.json"
            full_path = os.path.join(self.screenshots_dir, filename)
            with open(full_path, 'w', encoding='utf-8') as diagnostics_file:
                json.dump(self._last_locator_diagnostics, diagnostics_file, ensure_ascii=False, indent=2)
            logger.info(f"定位诊断已保存: {full_path}")
            return full_path
        except Exception:
            logger.exception("保存定位诊断失败")
            return None

    def _attach_allure(self, name: str, step: Optional[Dict[str, Any]] = None) -> None:
        if not ALLURE_AVAILABLE:
            return
        if step is not None:
            try:
                allure.attach(
                    json.dumps(step, ensure_ascii=False, indent=2),
                    name=f"{name}-step",
                    attachment_type=allure.attachment_type.JSON
                )
            except Exception:
                logger.exception("Allure JSON 附件写入失败")
        if self._last_locator_diagnostics:
            try:
                allure.attach(
                    json.dumps(self._last_locator_diagnostics, ensure_ascii=False, indent=2),
                    name=f"{name}-locator-diagnostics",
                    attachment_type=allure.attachment_type.JSON
                )
            except Exception:
                logger.exception("Allure 定位诊断附件写入失败")
        path = self._capture_screenshot(name)
        if path:
            try:
                allure.attach.file(path, name=name, attachment_type=allure.attachment_type.PNG)
            except Exception:
                logger.exception("Allure 截图附件写入失败: %s", path)

    def _get_current_resolution(self) -> tuple:
        try:
            resolution = G.DEVICE.get_current_resolution()
            return parse_resolution(resolution)
        except Exception as exc:
            logger.debug(f"读取当前设备分辨率失败: {exc}")
            return 0, 0

    def _create_template(self, image_path: str, config: Dict[str, Any]) -> Any:
        """创建可跨分辨率匹配的 Airtest 模板。"""
        kwargs = {
            'threshold': float(config.get('image_threshold', 0.85)),
            'rgb': bool(config.get('rgb', False)),
        }
        source_resolution = parse_resolution(
            config.get('source_resolution') or config.get('resolution')
        )
        if source_resolution != (0, 0):
            kwargs['resolution'] = source_resolution
        record_pos = config.get('record_pos')
        if not record_pos:
            record_pos = airtest_record_position(
                config.get('template_bounds') or config.get('bounds'),
                source_resolution,
            )
        if record_pos:
            kwargs['record_pos'] = tuple(record_pos)
        try:
            return Template(image_path, **kwargs)
        except TypeError:
            # 兼容较旧 Airtest 版本不支持的可选参数。
            kwargs.pop('rgb', None)
            return Template(image_path, **kwargs)

    def _load_element(self, element_id: int):
        try:
            from apps.app_automation.models import AppElement
            element = AppElement.objects.filter(id=element_id, is_active=True).first()
            if element and element_id not in self._counted_element_ids:
                element.increment_usage()
                self._counted_element_ids.add(element_id)
            return element
        except Exception as exc:
            logger.error(f"加载元素失败: element_id={element_id}, 错误: {exc}", exc_info=True)
            return None

    def _get_appium_webview_client(self):
        """按需创建 Appium WebDriver 客户端；纯原生用例不会产生额外会话。"""
        if self._appium_webview is not None:
            return self._appium_webview
        if self.platform not in ('android', 'ios') or not self.device_id:
            return None
        try:
            from ..models import AppDevice, AppTestConfig
            from ..utils.appium_webview import AppiumWebViewClient

            config = AppTestConfig.objects.first()
            device = AppDevice.objects.filter(device_id=self.device_id).first()
            platform_name = 'iOS' if self.platform == 'ios' else 'Android'
            automation_name = (
                'XCUITest'
                if self.platform == 'ios'
                else (config.appium_automation_name if config else 'UiAutomator2')
            )
            package_name = (
                self.package_name
                or (getattr(device, 'default_bundle_id', '') if device else '')
            )
            wda_url = ''
            if self.platform == 'ios':
                wda_url = (
                    (getattr(device, 'wda_url', '') if device else '')
                    or (config.ios_wda_url if config else '')
                )
            self._appium_webview = AppiumWebViewClient(
                server_url=(config.appium_server_url if config else 'http://127.0.0.1:4723'),
                device_id=self.device_id,
                automation_name=automation_name,
                package_name=package_name,
                platform_name=platform_name,
                web_driver_agent_url=wda_url,
            )
            return self._appium_webview
        except Exception as exc:
            self._last_webview_diagnostics = {
                'attempted': True,
                'matched': False,
                'stage': 'create-client',
                'error': str(exc),
            }
            logger.warning('初始化 Appium WebView 客户端失败: %s', exc)
            return None

    def _webview_step_config(self, step: Dict[str, Any]) -> tuple:
        config: Dict[str, Any] = {}
        element_type = ''
        element_id = step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if element:
                config.update(element.config or {})
                element_type = str(element.element_type or '').lower()
        config.update({
            key: value for key, value in step.items()
            if key in {
                'webview_css', 'css_selector', 'webview_xpath', 'webview_text',
                'resource_id', 'accessibility_id', 'content_desc', 'text', 'xpath',
                'webview_context', 'context_preference', 'webview_enabled',
            } and value not in (None, '')
        })
        if not element_type and any(
            config.get(key) for key in (
                'webview_css', 'css_selector', 'webview_xpath', 'resource_id',
                'accessibility_id', 'content_desc', 'text', 'xpath',
            )
        ):
            element_type = 'appium'
        return config, element_type

    def _should_try_webview(self, step: Dict[str, Any], config: Dict[str, Any], element_type: str) -> bool:
        if self.platform not in ('android', 'ios') or not self.device_id:
            return False
        if step.get('webview_enabled', config.get('webview_enabled', True)) is False:
            return False
        preference = str(
            step.get('context_preference')
            or config.get('context_preference')
            or 'auto'
        ).strip().lower()
        if preference == 'native':
            return False
        if element_type != 'appium' and not any(
            config.get(key) for key in ('webview_css', 'css_selector', 'webview_xpath')
        ):
            return False
        if preference == 'webview' or any(
            config.get(key) for key in ('webview_css', 'css_selector', 'webview_xpath')
        ):
            return True
        return self._detect_page_context().get('type') == 'webview'

    def _try_webview_action(
        self,
        step: Dict[str, Any],
        action: str,
        value: Any = None,
        send_enter: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """在混合页面中优先通过 Appium WEBVIEW DOM 执行动作，失败返回 None。"""
        config, element_type = self._webview_step_config(step)
        if not self._should_try_webview(step, config, element_type):
            return None
        client = self._get_appium_webview_client()
        if client is None:
            return None

        diagnostics: Dict[str, Any] = {
            'attempted': True,
            'matched': False,
            'action': action,
        }
        try:
            selected_context, context_detail = client.switch_to_webview(
                preferred_context=(step.get('webview_context') or config.get('webview_context') or ''),
                timeout=float(step.get('webview_timeout', 5)),
            )
            diagnostics.update({
                'context': selected_context,
                'context_detail': context_detail,
            })
            if action == 'click':
                action_detail = client.click(config)
            elif action == 'input':
                action_detail = client.input_text(
                    config,
                    value,
                    clear_first=step.get('clear_first', True),
                    send_enter=send_enter,
                )
            elif action == 'checkbox':
                action_detail = client.set_checkbox(
                    config,
                    desired_state=str(value or step.get('desired_state') or 'checked'),
                )
            else:
                raise ValueError(f'不支持的 WebView 动作: {action}')
            diagnostics.update({
                'matched': True,
                'action_detail': action_detail,
            })
            heal_detail = self._record_self_heal(
                step,
                action_detail.get('selected_strategy'),
                action_detail.get('attempts') or [],
            )
            if heal_detail:
                diagnostics['self_heal'] = heal_detail
            logger.info(
                'WebView DOM 动作成功: action=%s, context=%s, strategy=%s',
                action,
                selected_context,
                action_detail.get('selected_strategy'),
            )
            return diagnostics
        except Exception as exc:
            diagnostics['error'] = str(exc)
            logger.warning('WebView DOM 动作失败，回落智能定位链路: %s', exc)
            return None
        finally:
            if step.get('restore_native_context', True):
                try:
                    diagnostics['restored_context'] = client.switch_to_native()
                except Exception as exc:
                    diagnostics['restore_error'] = str(exc)
            self._last_webview_diagnostics = diagnostics

    def close(self) -> None:
        """释放按需创建的 Appium WebDriver 会话。"""
        if self._appium_webview is None:
            return
        try:
            self._appium_webview.close()
        finally:
            self._appium_webview = None

    @staticmethod
    def _normalize_terminal_webview_paths(value: Any) -> List[str]:
        if value in (None, False):
            return []
        raw_items = value
        if isinstance(value, str):
            raw_items = [item.strip() for item in value.split(',')]
        if not isinstance(raw_items, (list, tuple, set)):
            raw_items = [raw_items]

        paths = []
        for item in raw_items:
            path = str(item or '').strip()
            if not path:
                continue
            parsed = urlparse(path)
            normalized = parsed.path or path
            if not normalized.startswith('/'):
                normalized = f'/{normalized}'
            normalized = normalized.rstrip('/') or '/'
            if normalized not in paths:
                paths.append(normalized)
        return paths

    @classmethod
    def _webview_url_matches_terminal_path(
        cls,
        location: Dict[str, Any],
        terminal_paths: List[str],
    ) -> bool:
        if not terminal_paths:
            return False
        normalized_terminals = {
            (path.rstrip('/') or '/') for path in terminal_paths
        }
        candidates = []
        for key in ('pathname', 'path', 'href', 'url'):
            value = str(location.get(key) or '').strip()
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.path:
                candidates.append(parsed.path)
            elif value.startswith('/'):
                candidates.append(value.split('?', 1)[0].split('#', 1)[0])
            if parsed.fragment:
                fragment = parsed.fragment
                if fragment.startswith('/'):
                    candidates.append(urlparse(fragment).path or fragment)
                elif fragment.startswith('!/'):
                    candidates.append(urlparse(fragment[1:]).path or fragment[1:])

        for candidate in candidates:
            normalized = candidate.rstrip('/') or '/'
            if normalized in normalized_terminals:
                return True
        return False

    def _current_webview_location_state(self, timeout: float = 1.0) -> Dict[str, Any]:
        client = self._get_appium_webview_client()
        if client is None:
            return {'matched': False, 'reason': 'webview-client-unavailable'}
        diagnostics: Dict[str, Any] = {
            'matched': False,
            'method': 'webview-location',
        }
        try:
            context, context_detail = client.switch_to_webview(timeout=timeout)
            location = client.execute_script(
                """
return {
  href: window.location.href,
  pathname: window.location.pathname,
  search: window.location.search,
  hash: window.location.hash,
  title: document.title
};
""",
                [],
            )
            diagnostics.update({
                'matched': isinstance(location, dict),
                'context': context,
                'context_detail': context_detail,
                'location': location if isinstance(location, dict) else {},
            })
        except Exception as exc:
            diagnostics.update({
                'reason': 'webview-location-unavailable',
                'error': str(exc),
            })
        finally:
            try:
                diagnostics['restored_context'] = client.switch_to_native()
            except Exception as exc:
                diagnostics['restore_error'] = str(exc)
            self._last_webview_diagnostics = diagnostics
        return diagnostics

    def _terminal_webview_page_state(self) -> Dict[str, Any]:
        if not self.runtime.get('stop_on_terminal_webview_path', True):
            return {'matched': False, 'reason': 'disabled'}
        terminal_paths = self._normalize_terminal_webview_paths(
            self.runtime.get('terminal_webview_paths', DEFAULT_TERMINAL_WEBVIEW_PATHS)
        )
        if not terminal_paths:
            return {'matched': False, 'reason': 'no-terminal-paths'}

        location_state = self._current_webview_location_state(
            timeout=float(self.runtime.get('terminal_webview_check_timeout', 1.0))
        )
        if not location_state.get('matched'):
            return location_state

        location = location_state.get('location') or {}
        matched = self._webview_url_matches_terminal_path(location, terminal_paths)
        return {
            **location_state,
            'matched': matched,
            'terminal_paths': terminal_paths,
            'reason': '' if matched else 'terminal-path-not-matched',
        }

    def _take_stability_frame(self):
        try:
            import cv2
            screen = G.DEVICE.snapshot()
            if screen is None:
                return None
            gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if len(screen.shape) == 3 else screen
            return cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
        except Exception as exc:
            logger.debug(f"页面稳定性截图失败: {exc}")
            return None

    @staticmethod
    def _frame_difference(previous, current) -> float:
        try:
            import numpy as np
            return float(np.mean(np.abs(current.astype('float32') - previous.astype('float32'))) / 255.0)
        except Exception:
            return 1.0

    def _wait_for_page_stable(self, step: Dict[str, Any]) -> None:
        enabled = step.get('wait_for_stable', self.runtime.get('wait_for_stable', True))
        if not enabled:
            return
        timeout = max(0.0, float(step.get('page_stable_timeout', self.runtime.get('page_stable_timeout', 1.2))))
        if timeout <= 0:
            return
        interval = max(0.05, float(self.runtime.get('page_stable_interval', 0.2)))
        threshold = float(step.get('page_stable_threshold', self.runtime.get('page_stable_threshold', 0.018)))
        required_samples = max(1, int(self.runtime.get('page_stable_samples', 2)))
        deadline = time.monotonic() + timeout
        previous = self._take_stability_frame()
        if previous is None:
            return
        stable_samples = 0
        differences = []
        while time.monotonic() < deadline:
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))
            current = self._take_stability_frame()
            if current is None:
                return
            difference = self._frame_difference(previous, current)
            differences.append(round(difference, 4))
            if difference <= threshold:
                stable_samples += 1
                if stable_samples >= required_samples:
                    logger.debug(f"页面已稳定，帧差: {differences[-required_samples:]}")
                    return
            else:
                stable_samples = 0
            previous = current
        logger.info(f"页面稳定等待超时，继续定位，最近帧差: {differences[-3:]}")

    def _dump_current_ui_nodes(self) -> List[Dict[str, Any]]:
        try:
            device_class = G.DEVICE.__class__.__name__.lower()
            if device_class == 'ios' and hasattr(G.DEVICE, 'driver'):
                output = G.DEVICE.driver.source(format='xml')
            else:
                output = G.DEVICE.shell(
                    'uiautomator dump --compressed /data/local/tmp/runnergo-window.xml >/dev/null 2>&1; '
                    'cat /data/local/tmp/runnergo-window.xml; '
                    'rm -f /data/local/tmp/runnergo-window.xml'
                )
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            return parse_ui_hierarchy(str(output or ''), self._get_current_resolution())
        except Exception as exc:
            logger.debug(f"运行时 UI 树抓取失败: {exc}")
            return []

    def _semantic_target(self, fingerprint: Dict[str, Any], minimum_score: float = 36.0):
        nodes = self._dump_current_ui_nodes()
        node, diagnostics = select_best_ui_node(fingerprint, nodes, minimum_score=minimum_score)
        if not node:
            return None, diagnostics
        return region_center(node.get('bounds')), diagnostics

    @staticmethod
    def _input_locator_text(value: Any) -> str:
        return re.sub(r'\s+', '', str(value or '')).strip().casefold()

    @staticmethod
    def _is_generated_recording_input_name(value: Any) -> bool:
        text = str(value or '').strip()
        return bool(re.fullmatch(r'(?:iOS\s*)?录制输入\s*\d+', text, flags=re.IGNORECASE))

    @staticmethod
    def _is_generated_recording_click_name(value: Any) -> bool:
        text = str(value or '').strip()
        return bool(re.fullmatch(r'(?:iOS\s*)?录制点击\s*\d+', text, flags=re.IGNORECASE))

    def _should_inherit_ios_recorded_input_target(self, step: Dict[str, Any]) -> bool:
        if self.platform != 'ios':
            return False
        if step.get('element_id'):
            return False
        if step.get('selector') not in (None, ''):
            return False
        return (
            self._is_generated_recording_input_name(step.get('name'))
            or (
                self._is_input_action_step(step)
                and self._last_ios_recorded_input_target_config is not None
            )
        )

    def _remember_ios_recorded_input_target(self, step: Dict[str, Any], target: Any) -> None:
        if self.platform != 'ios':
            return
        if not self._step_targets_text_input(step):
            if self._is_generated_recording_click_name(step.get('name')):
                logger.debug(
                    "保留 iOS 录制输入目标，当前点击不是输入控件: %s",
                    step.get('name'),
                )
                return
            self._last_ios_recorded_input_target_config = None
            return

        config = self._step_element_config(step)
        target_config: Dict[str, Any] = {}
        for key in (
            'element_id', 'selector_type', 'selector', 'source_resolution',
            'normalized_position', 'coordinate_mode', 'fingerprint',
            'fingerprint_version', 'locator_strategies', 'semantic_min_score',
            'resource_id', 'accessibility_id', 'content_desc', 'text',
            'placeholder', 'class_name', 'timeout', 'wait_for_stable',
            'guard_security_challenge', 'image_scope', 'image_threshold',
        ):
            value = config.get(key)
            if value not in (None, '', {}):
                target_config[key] = copy.deepcopy(value)

        fingerprint = target_config.get('fingerprint') or {}
        if isinstance(fingerprint, dict):
            for source_key, target_key in (
                ('resource_id', 'resource_id'),
                ('content_desc', 'content_desc'),
                ('text', 'text'),
                ('placeholder', 'placeholder'),
                ('class_name', 'class_name'),
            ):
                if target_config.get(target_key) in (None, '') and fingerprint.get(source_key):
                    target_config[target_key] = fingerprint.get(source_key)

        if isinstance(target, (list, tuple)) and len(target) >= 2:
            target_config['_resolved_target'] = [target[0], target[1]]
        if not any(target_config.get(key) for key in (
            'element_id', 'selector', 'fingerprint', 'locator_strategies',
            'resource_id', 'accessibility_id', 'content_desc', 'text',
        )):
            self._last_ios_recorded_input_target_config = None
            return
        self._last_ios_recorded_input_target_config = target_config

    def _inherit_ios_recorded_input_target(self, step: Dict[str, Any]) -> Dict[str, Any]:
        if not self._should_inherit_ios_recorded_input_target(step):
            return step
        remembered = self._last_ios_recorded_input_target_config
        if not remembered:
            return step

        inherited = copy.deepcopy(step)
        for key, value in remembered.items():
            if key == '_resolved_target':
                continue
            if value in (None, '', {}):
                continue
            if key in ('selector', 'selector_type'):
                if inherited.get('selector') in (None, ''):
                    inherited[key] = copy.deepcopy(value)
                continue
            if inherited.get(key) in (None, '', {}):
                inherited[key] = copy.deepcopy(value)

        inherited['_inherited_ios_recording_input_target'] = {
            key: copy.deepcopy(value)
            for key, value in remembered.items()
            if key != '_resolved_target'
        }
        if remembered.get('_resolved_target'):
            inherited['_inherited_ios_recording_input_target']['resolved_target'] = (
                remembered.get('_resolved_target')
            )
        return inherited

    @classmethod
    def _input_text_match_score(cls, expected: Any, actual: Any) -> int:
        expected_text = cls._input_locator_text(expected)
        actual_text = cls._input_locator_text(actual)
        if not expected_text or not actual_text:
            return 0
        if expected_text == actual_text:
            return 120
        if (
            actual_text == '地址'
            and expected_text.endswith('地址')
            and expected_text != actual_text
            and expected_text != '详细地址'
        ):
            return 0
        shorter = min(len(expected_text), len(actual_text))
        if shorter >= 2 and (expected_text in actual_text or actual_text in expected_text):
            return 82
        return 0

    @staticmethod
    def _node_bounds_tuple(node: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
        bounds = node.get('bounds') or {}
        try:
            return (
                float(bounds.get('x1')),
                float(bounds.get('y1')),
                float(bounds.get('x2')),
                float(bounds.get('y2')),
            )
        except (AttributeError, TypeError, ValueError):
            return None

    @classmethod
    def _node_center_tuple(cls, node: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        center = node.get('center') or {}
        try:
            return float(center.get('x')), float(center.get('y'))
        except (AttributeError, TypeError, ValueError):
            bounds = cls._node_bounds_tuple(node)
            if not bounds:
                return None
            x1, y1, x2, y2 = bounds
            return (x1 + x2) / 2, (y1 + y2) / 2

    @classmethod
    def _node_text_values(cls, node: Dict[str, Any]) -> List[str]:
        values = []
        for key in ('resource_id', 'text', 'content_desc', 'placeholder', 'value'):
            value = str(node.get(key) or '').strip()
            if value and value not in values:
                values.append(value)
        return values

    def _step_element_config(self, step: Dict[str, Any]) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        element_id = step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if element:
                config.update(element.config or {})
        nested_config = step.get('config')
        if isinstance(nested_config, dict):
            config.update(nested_config)
        for key, value in step.items():
            if key != 'config' and value not in (None, '', {}):
                config[key] = value
        return config

    def _input_locator_hints(self, step: Dict[str, Any]) -> List[str]:
        config = self._step_element_config(step)
        hints: List[str] = []

        def add(value: Any) -> None:
            text = str(value or '').strip()
            if not text:
                return
            if text in hints:
                return
            hints.append(text)

        for key in (
            'resource_id', 'accessibility_id', 'content_desc', 'text',
            'ocr_text', 'webview_text', 'placeholder', 'name', 'description',
        ):
            if key == 'name' and self._is_generated_recording_input_name(config.get(key)):
                continue
            add(config.get(key))
        fingerprint = config.get('fingerprint') or {}
        if isinstance(fingerprint, dict):
            for key in ('resource_id', 'text', 'content_desc', 'placeholder'):
                add(fingerprint.get(key))
        for strategy in config.get('locator_strategies') or []:
            if not isinstance(strategy, dict) or strategy.get('enabled') is False:
                continue
            strategy_type = str(strategy.get('type') or '').strip().lower()
            if strategy_type in {'resource_id', 'accessibility', 'text', 'ocr', 'xpath'}:
                add(strategy.get('value'))
        return hints

    def _is_input_action_step(self, step: Dict[str, Any]) -> bool:
        action_type = str(step.get('type') or step.get('action') or '').strip().lower()
        return action_type in {
            'input', 'input_text', 'smart_input', 'clear_text',
            'bank_card_input', 'money_input',
        }

    @staticmethod
    def _text_input_class_name(value: Any) -> bool:
        class_name = str(value or '')
        return any(token in class_name for token in (
            'EditText',
            'TextField',
            'SecureTextField',
            'SearchField',
            'XCUIElementTypeTextView',
        ))

    @classmethod
    def _looks_like_text_input_placeholder(cls, value: Any) -> bool:
        text = cls._input_locator_text(value)
        if not text or '请选择' in text:
            return False
        return any(marker in text for marker in (
            '请填写',
            '请输入',
            '填写',
            '输入',
        ))

    def _step_targets_text_input(self, step: Dict[str, Any]) -> bool:
        config = self._step_element_config(step)
        if self._text_input_class_name(config.get('class_name')):
            return True
        for key in ('resource_id', 'accessibility_id', 'content_desc', 'text', 'placeholder'):
            if self._looks_like_text_input_placeholder(config.get(key)):
                return True
        fingerprint = config.get('fingerprint') or {}
        if not isinstance(fingerprint, dict):
            return False
        if self._text_input_class_name(fingerprint.get('class_name')):
            return True
        return any(
            self._looks_like_text_input_placeholder(fingerprint.get(key))
            for key in ('resource_id', 'content_desc', 'text', 'placeholder')
        )

    def _input_relation_score(
        self,
        label_node: Dict[str, Any],
        input_node: Dict[str, Any],
    ) -> int:
        label_bounds = self._node_bounds_tuple(label_node)
        input_bounds = self._node_bounds_tuple(input_node)
        label_center = self._node_center_tuple(label_node)
        input_center = self._node_center_tuple(input_node)
        if not label_bounds or not input_bounds or not label_center or not input_center:
            return 0

        lx1, ly1, lx2, ly2 = label_bounds
        ix1, iy1, ix2, iy2 = input_bounds
        lcx, lcy = label_center
        icx, icy = input_center
        label_height = max(1.0, ly2 - ly1)
        input_height = max(1.0, iy2 - iy1)
        vertical_overlap = max(0.0, min(ly2, iy2) - max(ly1, iy1))
        same_row = vertical_overlap >= min(label_height, input_height) * 0.35
        score = 0

        if same_row and icx >= lcx:
            score += 80
            score += max(0, int(30 - abs(icy - lcy) / 4))
        elif iy1 >= ly2 and iy1 - ly2 <= label_height * 3:
            score += 45
            horizontal_overlap = max(0.0, min(lx2, ix2) - max(lx1, ix1))
            if horizontal_overlap > 0:
                score += 20

        if label_node.get('parent') and label_node.get('parent') == input_node.get('parent'):
            score += 18
        return score

    def _input_control_target(
        self,
        step: Dict[str, Any],
        fallback_target: Any = None,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        nodes = self._dump_current_ui_nodes()
        visible_inputs = [
            node for node in nodes
            if self._is_text_input_node(node)
            and node.get('enabled') is not False
            and node.get('visible') is not False
        ]
        excluded_browser_inputs = [
            node for node in visible_inputs
            if self.platform == 'ios' and self._is_ios_browser_address_input(node)
        ]
        inputs = [
            node for node in visible_inputs
            if node not in excluded_browser_inputs
        ]
        hints = self._input_locator_hints(step)
        detail: Dict[str, Any] = {
            'matched': False,
            'method': 'input_control',
            'hint_count': len(hints),
            'input_count': len(inputs),
            'excluded_browser_input_count': len(excluded_browser_inputs),
        }
        if not inputs:
            detail['reason'] = 'no-visible-input-controls'
            return None, detail

        fallback_point = None
        if isinstance(fallback_target, (list, tuple)) and len(fallback_target) >= 2:
            try:
                fallback_point = (float(fallback_target[0]), float(fallback_target[1]))
            except (TypeError, ValueError):
                fallback_point = None
        if fallback_point:
            px, py = fallback_point
            containing = []
            for node in inputs:
                bounds = self._node_bounds_tuple(node)
                if not bounds:
                    continue
                x1, y1, x2, y2 = bounds
                if x1 <= px <= x2 and y1 <= py <= y2:
                    containing.append(node)
            if containing:
                selected = min(
                    containing,
                    key=lambda item: (
                        (self._node_bounds_tuple(item) or (0, 0, 0, 0))[2]
                        - (self._node_bounds_tuple(item) or (0, 0, 0, 0))[0]
                    ) * (
                        (self._node_bounds_tuple(item) or (0, 0, 0, 0))[3]
                        - (self._node_bounds_tuple(item) or (0, 0, 0, 0))[1]
                    ),
                )
                identity_score = 0
                matched_hint = ''
                for hint in hints:
                    for actual in self._node_text_values(selected):
                        match_score = self._input_text_match_score(hint, actual)
                        if match_score > identity_score:
                            identity_score = match_score
                            matched_hint = hint
                target = region_center(selected.get('bounds')) if (not hints or identity_score) else None
                if target:
                    detail.update({
                        'matched': True,
                        'method': 'coordinate_inside_input',
                        'score': 260,
                        'target': list(target),
                        'class_name': selected.get('class_name'),
                        'resource_id': selected.get('resource_id'),
                        'text': selected.get('text'),
                        'placeholder': selected.get('placeholder'),
                        'matched_hint': matched_hint,
                        'identity_score': identity_score,
                    })
                    return target, detail

        ranked = []
        for node in inputs:
            score = 0
            matched_hint = ''
            for hint in hints:
                for actual in self._node_text_values(node):
                    match_score = self._input_text_match_score(hint, actual)
                    if match_score > score:
                        score = match_score
                        matched_hint = hint
            if score:
                ranked.append((score, node, {
                    'method': 'input_identity',
                    'matched_hint': matched_hint,
                }))

        labels = [node for node in nodes if node not in inputs]
        for label in labels:
            label_score = 0
            matched_hint = ''
            for hint in hints:
                for actual in self._node_text_values(label):
                    match_score = self._input_text_match_score(hint, actual)
                    if match_score > label_score:
                        label_score = match_score
                        matched_hint = hint
            if not label_score:
                continue
            for input_node in inputs:
                relation_score = self._input_relation_score(label, input_node)
                if relation_score:
                    ranked.append((label_score + relation_score, input_node, {
                        'method': 'label_association',
                        'matched_hint': matched_hint,
                        'label_text': label.get('text') or label.get('content_desc') or label.get('resource_id'),
                        'relation_score': relation_score,
                    }))

        if fallback_point:
            px, py = fallback_point
            if px is not None and py is not None:
                for node in inputs:
                    bounds = self._node_bounds_tuple(node)
                    center = self._node_center_tuple(node)
                    if not bounds or not center:
                        continue
                    x1, y1, x2, y2 = bounds
                    cx, cy = center
                    distance = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                    if hints and distance <= max(120.0, (x2 - x1 + y2 - y1) * 0.25):
                        ranked.append((55 - int(distance / 8), node, {
                            'method': 'coordinate_near_input',
                            'distance': round(distance, 1),
                        }))

        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked and not hints:
            focused = [node for node in inputs if node.get('focused')]
            if focused:
                target = region_center(focused[0].get('bounds'))
                if target:
                    detail.update({
                        'matched': True,
                        'method': 'focused_input',
                        'class_name': focused[0].get('class_name'),
                        'resource_id': focused[0].get('resource_id'),
                    })
                    return target, detail
        if not ranked or ranked[0][0] <= 0:
            detail['reason'] = 'no-input-control-matched'
            return None, detail
        score, selected, selected_detail = ranked[0]
        target = region_center(selected.get('bounds'))
        if not target:
            detail['reason'] = 'matched-input-has-no-bounds'
            return None, detail
        detail.update({
            **selected_detail,
            'matched': True,
            'score': score,
            'target': list(target),
            'class_name': selected.get('class_name'),
            'resource_id': selected.get('resource_id'),
            'text': selected.get('text'),
            'placeholder': selected.get('placeholder'),
        })
        return target, detail

    def _scroll_to_input_control_target(
        self,
        step: Dict[str, Any],
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        width, height = parse_resolution(self._get_current_resolution())
        detail: Dict[str, Any] = {
            'matched': False,
            'method': 'scroll_to_input_control',
            'attempts': [],
        }
        if width <= 0 or height <= 0:
            detail['reason'] = 'invalid-resolution'
            return None, detail

        max_swipes = int(step.get(
            'input_scroll_attempts',
            self.runtime.get('input_scroll_attempts', 3),
        ) or 0)
        max_swipes = max(0, min(5, max_swipes))
        if max_swipes <= 0:
            detail['reason'] = 'scroll-disabled'
            return None, detail

        start = (
            int(round(width * 0.55)),
            int(round(height * 0.76)),
        )
        end = (
            int(round(width * 0.55)),
            int(round(height * 0.36)),
        )
        primary_target, primary_detail = self._ios_primary_transition_target(step)
        if primary_target and primary_detail.get('bounds'):
            try:
                _x1, y1, _x2, _y2 = [
                    float(value) for value in primary_detail.get('bounds') or []
                ]
            except (TypeError, ValueError):
                y1 = 0.0
            if y1 > height * 0.45:
                start = (
                    int(round(width * 0.55)),
                    int(round(max(height * 0.46, y1 - height * 0.12))),
                )
                end = (
                    int(round(width * 0.55)),
                    int(round(height * 0.22)),
                )
                detail['fixed_primary_scroll_avoidance'] = primary_detail
        duration = float(step.get(
            'input_scroll_duration',
            self.runtime.get('input_scroll_duration', 0.35),
        ))
        if primary_target and primary_detail.get('bounds'):
            duration = max(duration, 0.55)
        wait_after = float(step.get(
            'input_scroll_wait_after',
            self.runtime.get('input_scroll_wait_after', 0.45),
        ))

        for index in range(max_swipes):
            self.ensure_not_stopped()
            try:
                swipe(start, end, duration=duration)
            except Exception as exc:
                detail.update({
                    'reason': 'scroll-action-failed',
                    'error': str(exc),
                })
                return None, detail
            self._sleep_interruptibly(wait_after)
            target, input_detail = self._input_control_target(step)
            item = {
                'index': index + 1,
                'matched': bool(target),
                'detail': input_detail,
            }
            detail['attempts'].append(item)
            if target:
                detail.update({
                    'matched': True,
                    'target': list(target),
                    'scroll_count': index + 1,
                    'input_detail': input_detail,
                })
                return target, detail

        detail['reason'] = 'input-control-not-found-after-scroll'
        web_target, web_detail = self._scroll_webview_to_input_hint(step)
        detail['webview_scroll'] = web_detail
        if web_detail.get('matched'):
            self._sleep_interruptibly(float(step.get(
                'webview_scroll_wait_after',
                self.runtime.get('webview_scroll_wait_after', 0.6),
            )))
            target, input_detail = self._input_control_target(step)
            detail['webview_scroll_input_detail'] = input_detail
            if target:
                detail.update({
                    'matched': True,
                    'target': list(target),
                    'input_detail': input_detail,
                    'webview_scroll_target': web_target,
                })
                return target, detail
        return None, detail

    def _scroll_webview_to_input_hint(
        self,
        step: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        if self.platform not in ('android', 'ios'):
            return None, {'matched': False, 'reason': 'unsupported-platform'}
        hints = self._input_locator_hints(step)
        if not hints:
            return None, {'matched': False, 'reason': 'no-input-hints'}
        client = self._get_appium_webview_client()
        if client is None:
            return None, {'matched': False, 'reason': 'webview-client-unavailable'}

        diagnostics: Dict[str, Any] = {
            'matched': False,
            'method': 'webview-scroll-to-input-hint',
            'hints': hints,
        }
        script = r"""
const hints = (arguments[0] || []).map(String).filter(Boolean);
const norm = (value) => String(value || '').replace(/\s+/g, '').toLowerCase();
const normalizedHints = hints.map(norm).filter(Boolean);
const textOf = (el) => {
  if (!el) return '';
  const attrs = [
    'aria-label', 'name', 'placeholder', 'id', 'data-label',
    'data-name', 'title', 'value'
  ];
  let parts = attrs.map((name) => el.getAttribute && el.getAttribute(name)).filter(Boolean);
  if (el.labels) {
    parts = parts.concat(Array.from(el.labels).map((label) => label.innerText || label.textContent || ''));
  }
  let parent = el.parentElement;
  for (let depth = 0; parent && depth < 3; depth += 1, parent = parent.parentElement) {
    parts.push(parent.innerText || parent.textContent || '');
  }
  parts.push(el.innerText || el.textContent || '');
  return norm(parts.join(' '));
};
const matches = (el) => {
  const text = textOf(el);
  return normalizedHints.some((hint) => text.includes(hint) || hint.includes(text));
};
const controls = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'));
let target = controls.find(matches);
let source = 'control';
if (!target) {
  const elements = Array.from(document.querySelectorAll('label, div, span, p, li, section'));
  const label = elements.find(matches);
  if (label) {
    const container = label.closest('label, .field, .form-item, .van-cell, li, section, div') || label.parentElement;
    target = (container && container.querySelector('input, textarea, [contenteditable="true"]')) || label;
    source = 'label';
  }
}
if (!target) {
  return {matched: false, reason: 'hint-not-found', hints};
}
target.scrollIntoView({block: 'center', inline: 'nearest'});
return {
  matched: true,
  source,
  tag: target.tagName,
  text: (target.innerText || target.textContent || target.getAttribute('placeholder') || '').slice(0, 120),
  rect: (() => {
    const rect = target.getBoundingClientRect();
    return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
  })(),
  scrollX: window.scrollX,
  scrollY: window.scrollY
};
"""
        try:
            context, context_detail = client.switch_to_webview(
                preferred_context=str(step.get('webview_context') or ''),
                timeout=float(step.get('webview_timeout', 2.0)),
            )
            result = client.execute_script(script, [hints])
            diagnostics.update({
                'context': context,
                'context_detail': context_detail,
                'result': result,
                'matched': bool(isinstance(result, dict) and result.get('matched')),
            })
            if not diagnostics['matched']:
                diagnostics['reason'] = (
                    result.get('reason') if isinstance(result, dict) else 'script-not-matched'
                )
            return result if isinstance(result, dict) else None, diagnostics
        except Exception as exc:
            diagnostics['error'] = str(exc)
            diagnostics['reason'] = 'webview-scroll-failed'
            return None, diagnostics
        finally:
            try:
                diagnostics['restored_context'] = client.switch_to_native()
            except Exception as exc:
                diagnostics['restore_error'] = str(exc)
            self._last_webview_diagnostics = diagnostics

    def _detect_ios_system_overlay(self, nodes: Optional[List[Dict[str, Any]]] = None) -> str:
        if self.platform != 'ios':
            return ''
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        visible_text = ' '.join(
            str(node.get(key) or '')
            for node in nodes or []
            if node.get('visible', True) is not False
            for key in ('text', 'content_desc', 'resource_id')
        )
        if any(keyword in visible_text for keyword in (
            '精选集', '私密访问照片', '包含位置', '进一步了解',
            'PUOneUpView', 'PXGSingleViewContainerView_AX',
        )):
            return 'photo-picker'
        # Safari 页面中的 <input type="file"> 本身也会暴露为“选取文件”。
        # 只有“照片图库”和“选取文件”同时可见时，才是系统上传菜单。
        if all(keyword in visible_text for keyword in ('照片图库', '选取文件')):
            return 'file-upload-menu'
        return ''

    def _is_ios_photo_picker_done_step(self, step: Dict[str, Any]) -> bool:
        if self.platform != 'ios':
            return False
        config = self._step_element_config(step)
        fingerprint = config.get('fingerprint') or {}
        resource_ids = {
            str(config.get('resource_id') or '').strip(),
            str(fingerprint.get('resource_id') or '').strip(),
        }
        for strategy in config.get('locator_strategies') or []:
            if not isinstance(strategy, dict) or strategy.get('enabled') is False:
                continue
            if str(strategy.get('type') or '').strip().lower() == 'resource_id':
                resource_ids.add(str(strategy.get('value') or '').strip())
        return bool(resource_ids & IOS_PHOTO_PICKER_DONE_RESOURCE_IDS)

    def _is_ios_photo_asset_selection_step(self, step: Dict[str, Any]) -> bool:
        if self.platform != 'ios' or self._is_ios_photo_picker_done_step(step):
            return False
        action_type = str(step.get('type') or step.get('action') or '').strip().lower()
        if action_type not in {'click', 'touch', 'tap', 'smart_click', 'self_heal_click'}:
            return False
        if self._ios_pending_photo_upload_slot:
            return True
        haystack = self._step_text_haystack(step)
        if any(keyword in haystack for keyword in (
            '选择正面', '选择反面', '选择图片', '选择照片', '选中图片',
            '选中照片', '身份证正面', '身份证反面',
        )):
            return True

        config = self._step_element_config(step)
        fingerprint = config.get('fingerprint') or {}
        if isinstance(fingerprint, dict):
            class_name = str(fingerprint.get('class_name') or '')
            text_values = ' '.join(
                str(fingerprint.get(key) or '')
                for key in ('resource_id', 'text', 'content_desc')
            )
            if (
                class_name == 'XCUIElementTypeImage'
                and any(marker in text_values for marker in ('PXGGridLayout', '照片,'))
            ):
                return True
        return False

    def _has_ios_photo_grid_fingerprint(self, step: Dict[str, Any]) -> bool:
        config = self._step_element_config(step)
        fingerprint = config.get('fingerprint') or {}
        if not isinstance(fingerprint, dict):
            return False
        class_name = str(fingerprint.get('class_name') or '')
        text_values = ' '.join(
            str(fingerprint.get(key) or '')
            for key in ('resource_id', 'text', 'content_desc')
        )
        return (
            class_name == 'XCUIElementTypeImage'
            and any(marker in text_values for marker in ('PXGGridLayout', '照片,'))
        )

    def _is_ios_photo_upload_menu_opener_step(self, step: Dict[str, Any]) -> bool:
        if self.platform != 'ios' or self._has_ios_photo_grid_fingerprint(step):
            return False
        haystack = self._step_text_haystack(step)
        return any(keyword in haystack for keyword in (
            '选择图片', '选择照片', '选取图片', '选取照片',
        ))

    def _ios_identity_photo_slot_from_step(self, step: Dict[str, Any]) -> str:
        haystack = self._step_text_haystack(step)
        if any(keyword in haystack for keyword in ('身份证反面', '国徽面', '反面')):
            return 'back'
        if any(keyword in haystack for keyword in ('身份证正面', '人像面', '正面')):
            return 'front'
        return ''

    def _recorded_step_point(self, step: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        config = self._step_element_config(step)
        point_config: Dict[str, Any] = {}
        normalized = config.get('normalized_position')
        if isinstance(normalized, dict):
            point_config['normalized_position'] = normalized
        else:
            selector_type = str(config.get('selector_type') or '').strip().lower()
            selector = str(config.get('selector') or '').strip()
            if selector_type in {'pos', 'position', 'coordinate'} and ',' in selector:
                try:
                    raw_x, raw_y = selector.split(',', 1)
                    point_config['x'] = float(raw_x.strip())
                    point_config['y'] = float(raw_y.strip())
                except (TypeError, ValueError):
                    return None
        if not point_config:
            return None
        if config.get('source_resolution'):
            point_config['source_resolution'] = config.get('source_resolution')
        return scale_point(point_config, self._get_current_resolution())

    @staticmethod
    def _ios_identity_photo_grid_estimate(
        upload_slot: str,
        width: int,
        height: int,
    ) -> Optional[Tuple[int, int]]:
        if width <= 0 or height <= 0:
            return None
        if upload_slot == 'back':
            return (int(round(width * 0.165)), int(round(height * 0.41)))
        if upload_slot == 'front':
            return (int(round(width * 0.5)), int(round(height * 0.41)))
        return None

    @staticmethod
    def _ios_identity_upload_frame_estimate(
        upload_slot: str,
        width: int,
        height: int,
    ) -> Optional[Tuple[int, int]]:
        if width <= 0 or height <= 0:
            return None
        if upload_slot == 'front':
            return (int(round(width * 0.286)), int(round(height * 0.5492)))
        if upload_slot == 'back':
            return (int(round(width * 0.739)), int(round(height * 0.5538)))
        return None

    def _ios_open_photo_upload_menu_target(
        self,
        step: Dict[str, Any],
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        upload_slot = self._ios_pending_photo_upload_slot
        width, height = parse_resolution(self._get_current_resolution())
        upload_target = self._ios_identity_upload_frame_estimate(upload_slot, width, height)
        detail = {
            'matched': False,
            'method': 'identity-upload-frame-open-menu',
            'upload_slot': upload_slot,
            'upload_target': list(upload_target) if upload_target else None,
            'observations': [],
        }
        if not upload_target:
            detail['reason'] = 'missing-upload-slot-or-resolution'
            return None, detail

        logger.info(
            "iOS 照片菜单未打开，重新点击身份证上传框: slot=%s target=%s",
            upload_slot,
            upload_target,
        )
        touch(upload_target)
        timeout = max(0.5, min(8.0, float(step.get(
            'photo_upload_menu_open_timeout',
            self.runtime.get('photo_upload_menu_open_timeout', 3.0),
        ))))
        interval = max(0.1, min(1.0, float(step.get(
            'photo_upload_menu_open_poll_interval',
            self.runtime.get('photo_upload_menu_open_poll_interval', 0.2),
        ))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ensure_not_stopped()
            self._sleep_interruptibly(interval)
            nodes = self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(nodes)
            detail['observations'].append({
                'overlay': overlay,
                'node_count': len(nodes),
            })
            detail['observations'] = detail['observations'][-5:]
            overlay_target, overlay_detail = self._ios_photo_upload_overlay_target(
                step, nodes, overlay
            )
            if overlay_target:
                detail.update({
                    'matched': True,
                    'overlay': overlay,
                    'overlay_target': list(overlay_target),
                    'overlay_detail': overlay_detail,
                })
                return overlay_target, detail

        detail['reason'] = 'photo-upload-menu-not-opened'
        return None, detail

    def _ios_photo_upload_overlay_target(
        self,
        step: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        overlay: str,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        detail = {
            'overlay': overlay,
            'matched': False,
            'method': '',
        }
        if not self._is_ios_photo_asset_selection_step(step):
            detail['reason'] = 'not-photo-asset-selection-step'
            return None, detail

        if overlay == 'file-upload-menu':
            for node in nodes or []:
                if node.get('visible', True) is False:
                    continue
                values = self._node_text_values(node)
                if not any('照片图库' in value for value in values):
                    continue
                center = self._node_center_tuple(node)
                if center:
                    detail.update({
                        'matched': True,
                        'method': 'file-upload-menu-photo-library',
                        'text_values': values,
                    })
                    return (int(round(center[0])), int(round(center[1]))), detail
            detail['reason'] = 'photo-library-action-not-found'
            return None, detail

        if overlay != 'photo-picker':
            detail['reason'] = 'unsupported-overlay'
            return None, detail

        width, height = parse_resolution(self._get_current_resolution())
        upload_slot = (
            self._ios_identity_photo_slot_from_step(step)
            or self._ios_pending_photo_upload_slot
        )
        identity_grid_target = self._ios_identity_photo_grid_estimate(
            upload_slot,
            width,
            height,
        )
        if identity_grid_target:
            detail.update({
                'matched': True,
                'method': (
                    'identity-front-second-photo-grid-estimate'
                    if upload_slot == 'front'
                    else 'identity-back-first-photo-grid-estimate'
                ),
                'target': list(identity_grid_target),
                'upload_slot': upload_slot,
            })
            return identity_grid_target, detail

        candidates = []
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            bounds = self._node_bounds_tuple(node)
            if not bounds:
                continue
            x1, y1, x2, y2 = bounds
            node_width = x2 - x1
            node_height = y2 - y1
            if node_width <= 0 or node_height <= 0:
                continue
            if x1 < 0 or y1 < height * 0.25 or y2 > height * 0.9:
                continue
            if not (width * 0.12 <= node_width <= width * 0.45):
                continue
            ratio = node_width / max(1.0, node_height)
            if not 0.65 <= ratio <= 1.45:
                continue
            text_values = ' '.join(self._node_text_values(node))
            if any(keyword in text_values for keyword in (
                '私密访问照片', '进一步了解', '包含位置', '精选集',
                '照片图库', '选取文件',
            )):
                continue
            center = self._node_center_tuple(node)
            if not center:
                continue
            candidates.append((y1, x1, center, bounds, node.get('class_name')))

        if candidates:
            recorded_target = None if upload_slot else self._recorded_step_point(step)
            method = 'first-photo-grid-cell'
            if upload_slot:
                candidates.sort(key=lambda item: (item[0], item[1]))
                if upload_slot == 'front' and len(candidates) >= 2:
                    candidates = [candidates[1]]
                    method = 'identity-front-second-photo-grid-cell'
                elif upload_slot == 'back':
                    method = 'identity-back-first-photo-grid-cell'
                else:
                    method = 'identity-photo-grid-cell'
            elif recorded_target:
                candidates.sort(key=lambda item: (
                    math.hypot(item[2][0] - recorded_target[0], item[2][1] - recorded_target[1]),
                    item[0],
                    item[1],
                ))
                method = 'recorded-photo-grid-cell'
            else:
                candidates.sort(key=lambda item: (item[0], item[1]))
            _y1, _x1, center, bounds, class_name = candidates[0]
            detail.update({
                'matched': True,
                'method': method,
                'bounds': [int(round(value)) for value in bounds],
                'class_name': class_name,
            })
            if recorded_target:
                detail['recorded_target'] = list(recorded_target)
            if upload_slot:
                detail['upload_slot'] = upload_slot
            return (int(round(center[0])), int(round(center[1]))), detail

        if width > 0 and height > 0:
            target = (int(round(width * 0.165)), int(round(height * 0.41)))
            detail.update({
                'matched': True,
                'method': 'first-photo-grid-estimate',
                'target': list(target),
                'reason': 'no-grid-node-candidate',
            })
            return target, detail

        detail['reason'] = 'invalid-resolution'
        return None, detail

    @staticmethod
    def _ios_photo_upload_loading_visible(nodes: List[Dict[str, Any]]) -> bool:
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            values = ' '.join(
                str(node.get(key) or '')
                for key in ('text', 'content_desc', 'resource_id', 'value')
            )
            if any(keyword in values for keyword in IOS_PHOTO_UPLOAD_LOADING_KEYWORDS):
                return True
        return False

    def _wait_for_ios_photo_upload_complete(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """Wait for the photo picker to dismiss and the WebView upload mask to clear."""
        timeout = max(1.0, min(60.0, float(step.get(
            'photo_upload_settle_timeout',
            self.runtime.get('photo_upload_settle_timeout', 12.0),
        ))))
        initial_delay = max(0.0, min(3.0, float(step.get(
            'photo_upload_initial_delay',
            self.runtime.get('photo_upload_initial_delay', 0.6),
        ))))
        interval = max(0.1, min(2.0, float(step.get(
            'photo_upload_poll_interval',
            self.runtime.get('photo_upload_poll_interval', 0.2),
        ))))
        if initial_delay:
            self._sleep_interruptibly(initial_delay)

        deadline = time.monotonic() + timeout
        clear_samples = 0
        saw_loading = False
        observations = []
        while time.monotonic() < deadline:
            self.ensure_not_stopped()
            nodes = self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(nodes)
            loading = self._ios_photo_upload_loading_visible(nodes)
            saw_loading = saw_loading or loading
            observations.append({
                'overlay': overlay,
                'loading': loading,
                'node_count': len(nodes),
            })
            observations = observations[-5:]

            if nodes and not overlay and not loading:
                clear_samples += 1
                if clear_samples >= 2:
                    detail = {
                        'settled': True,
                        'saw_loading': saw_loading,
                        'observations': observations,
                    }
                    logger.info('iOS 照片上传已完成: %s', detail)
                    return detail
            else:
                clear_samples = 0
            self._sleep_interruptibly(interval)

        raise AssertionError(
            f'iOS 照片上传等待超时（{timeout:.1f}s）: {observations}'
        )

    def _select_ios_photo_after_upload_menu_if_needed(
        self,
        step: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self.platform != 'ios' or not self._is_ios_photo_asset_selection_step(step):
            return {'matched': False, 'reason': 'not-photo-asset-selection-step'}
        if not self._has_ios_photo_grid_fingerprint(step):
            return {'matched': False, 'reason': 'not-recorded-photo-asset-step'}

        diagnostics = self._last_locator_diagnostics or {}
        if diagnostics.get('selected_strategy') != 'ios_photo_upload_overlay':
            return {'matched': False, 'reason': 'not-ios-photo-upload-overlay'}
        attempts = diagnostics.get('attempts') or []
        overlay_attempt = next(
            (
                item for item in reversed(attempts)
                if item.get('strategy') == 'ios_photo_upload_overlay'
            ),
            {},
        )
        overlay_detail = overlay_attempt.get('detail') or {}
        if overlay_detail.get('overlay') != 'file-upload-menu':
            return {'matched': False, 'reason': 'not-file-upload-menu'}

        timeout = max(0.5, min(8.0, float(step.get(
            'photo_picker_open_timeout',
            self.runtime.get('photo_picker_open_timeout', 3.0),
        ))))
        interval = max(0.1, min(1.0, float(step.get(
            'photo_picker_open_poll_interval',
            self.runtime.get('photo_picker_open_poll_interval', 0.2),
        ))))
        deadline = time.monotonic() + timeout
        observations = []
        while time.monotonic() < deadline:
            self.ensure_not_stopped()
            nodes = self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(nodes)
            observations.append({
                'overlay': overlay,
                'node_count': len(nodes),
            })
            observations = observations[-5:]
            if overlay == 'photo-picker':
                target, detail = self._ios_photo_upload_overlay_target(
                    step, nodes, overlay
                )
                if target:
                    logger.info(
                        "iOS 照片图库打开后继续选择图片: target=%s detail=%s",
                        target, detail,
                    )
                    touch(target)
                    return {
                        'matched': True,
                        'target': list(target),
                        'detail': detail,
                        'observations': observations,
                    }
            self._sleep_interruptibly(interval)

        return {
            'matched': False,
            'reason': 'photo-picker-not-opened',
            'observations': observations,
        }

    def _coordinate_fallback_block_reason(
        self,
        step: Dict[str, Any],
        attempts: List[Dict[str, Any]],
    ) -> str:
        if step.get('allow_coordinate_fallback') is True:
            return ''
        has_non_static_attempt = any(
            item.get('strategy') not in {'position', 'region_center'}
            for item in attempts
        )
        if step.get('block_coordinate_fallback') is True:
            return 'coordinate-fallback-disabled'
        if self._is_input_action_step(step):
            return 'input-action-coordinate-fallback-disabled'
        if self._step_targets_text_input(step) and has_non_static_attempt:
            return 'text-input-coordinate-fallback-disabled'
        if self._is_signature_action_step(step) and has_non_static_attempt:
            return 'signature-target-coordinate-fallback-disabled'
        if self._is_picker_entry_step(step) and has_non_static_attempt:
            return 'picker-entry-coordinate-fallback-disabled'
        if has_non_static_attempt:
            nodes = self._last_page_nodes or self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(nodes)
            if overlay == 'photo-picker' and self._is_ios_photo_asset_selection_step(step):
                return ''
            if overlay:
                return f'ios-system-overlay:{overlay}'
        return ''

    _notice_hint_keywords = (
        '服务器异常', '异常', '错误', '失败', '请联系', '网络', '超时',
        '请输入', '请填写', '请选择', '请勾选', '请同意', '无效', '不正确',
        '已存在', '已过期', '重新', '加载中', '稍后再试',
    )

    def _locator_failure_context_hints(
        self,
        step: Dict[str, Any],
        nodes: Optional[List[Dict[str, Any]]] = None,
        limit: int = 4,
    ) -> List[str]:
        """定位失败时提取页面上下文线索（toast/占位符/锚点丢失），解释真实原因。

        典型场景：上一步提交触发"服务器异常"后页面重载清空表单，
        当前步骤的元素永远不会出现——此时报"定位失败"会误导排查方向。
        """
        page_nodes = nodes if nodes is not None else (self._last_page_nodes or [])
        hints: List[str] = []

        def node_text(node: Any) -> str:
            if not isinstance(node, dict):
                return ''
            return str(
                node.get('text')
                or node.get('content_desc')
                or node.get('resource_id')
                or ''
            ).strip()

        for node in page_nodes:
            text = node_text(node)
            if not text or len(text) > 60:
                continue
            if any(keyword in text for keyword in self._notice_hint_keywords):
                hint = f"页面提示文本: {text}" if not hints else text
                if hint not in hints:
                    hints.append(hint)
                if len(hints) >= limit:
                    return hints

        config = self._step_element_config(step)
        fingerprint = config.get('fingerprint') or {}
        parent = fingerprint.get('parent') or {}
        anchor_text = str(parent.get('text') or parent.get('resource_id') or '').strip()
        if anchor_text and not any(anchor_text in node_text(node) for node in page_nodes):
            hints.append(
                f"录制时所在容器 '{anchor_text}' 不在当前页面，"
                "页面可能未发生预期跳转或已重载"
            )
        return hints[:limit]

    def _step_text_haystack(self, step: Dict[str, Any]) -> str:
        values = [
            step.get('name'),
            step.get('text'),
            step.get('ocr_text'),
            step.get('accessibility_id'),
            step.get('resource_id'),
            step.get('selector'),
        ]
        config = self._step_element_config(step)
        values.extend([
            config.get('text'),
            config.get('ocr_text'),
            config.get('accessibility_id'),
            config.get('resource_id'),
            config.get('selector'),
        ])
        fingerprint = config.get('fingerprint') or {}
        if isinstance(fingerprint, dict):
            values.extend([
                fingerprint.get('text'),
                fingerprint.get('content_desc'),
                fingerprint.get('resource_id'),
            ])
        return ' '.join(str(value or '') for value in values)

    def _is_signature_action_step(self, step: Dict[str, Any]) -> bool:
        haystack = self._step_text_haystack(step)
        return '去签署' in haystack or '点击签署' in haystack

    def _is_picker_entry_step(self, step: Dict[str, Any]) -> bool:
        haystack = self._step_text_haystack(step)
        action_type = str(step.get('type') or step.get('action') or '').strip().lower()
        if action_type not in {'click', 'touch', 'tap', 'smart_click'}:
            return False
        if any(action in haystack for action in FORM_SELECT_ACTION_LABELS):
            return False
        return any(keyword in haystack for keyword in (
            '申请地区', '选择地区', '所在地区', '省市区', '省/市/区',
            '省市', '城市', '省份',
        ))

    def _form_select_entry_label(self, step: Dict[str, Any]) -> str:
        haystack = self._step_text_haystack(step)
        action_type = str(step.get('type') or step.get('action') or '').strip().lower()
        if action_type not in {'click', 'touch', 'tap', 'smart_click', 'self_heal_click'}:
            return ''
        if any(action in haystack for action in FORM_SELECT_ACTION_LABELS):
            return ''
        for label in FORM_SELECT_PLACEHOLDER_LABELS:
            if label in haystack:
                return label
        return ''

    def _form_select_entry_target(
        self,
        step: Dict[str, Any],
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        label = self._form_select_entry_label(step)
        if not label:
            return None, {'matched': False, 'reason': 'not-form-select-entry'}
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        label_nodes = []
        row_nodes = []
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            values = [str(value or '').strip() for value in self._node_text_values(node)]
            if not any(values):
                continue
            bounds = self._node_bounds_tuple(node)
            if not bounds:
                continue
            if any(label in value for value in values):
                for value in values:
                    if label not in value:
                        continue
                    remaining = (
                        value
                        .replace(label, '')
                        .replace('', '')
                        .replace('>', '')
                        .replace('›', '')
                        .strip()
                    )
                    if (
                        remaining
                        and placeholder not in remaining
                        and remaining not in FORM_SELECT_ACTION_LABELS
                    ):
                        return 'selected'
                label_nodes.append(node)
                class_name = str(node.get('class_name') or '')
                if 'Button' in class_name or node.get('clickable') is True:
                    row_nodes.append(node)

        for label_node in label_nodes:
            label_bounds = self._node_bounds_tuple(label_node)
            if not label_bounds:
                continue
            lx1, ly1, lx2, ly2 = label_bounds
            label_cy = (ly1 + ly2) / 2
            label_height = max(1.0, ly2 - ly1)
            same_row_nodes = []
            for node in row_nodes or label_nodes:
                bounds = self._node_bounds_tuple(node)
                if not bounds:
                    continue
                x1, y1, x2, y2 = bounds
                cy = (y1 + y2) / 2
                if abs(cy - label_cy) <= max(label_height, y2 - y1) * 1.2:
                    same_row_nodes.append((x1, y1, x2, y2, node))
            if same_row_nodes:
                x1 = min(item[0] for item in same_row_nodes)
                y1 = min(item[1] for item in same_row_nodes)
                x2 = max(item[2] for item in same_row_nodes)
                y2 = max(item[3] for item in same_row_nodes)
                width, _ = parse_resolution(self._get_current_resolution())
                right_edge = width - 84 if width > 0 else x2
                target = (int((max(x2, right_edge) + lx1) / 2), int((y1 + y2) / 2))
                return target, {
                    'matched': True,
                    'method': 'form-select-label-row',
                    'label': label,
                    'row_bounds': {'x1': x1, 'y1': y1, 'x2': max(x2, right_edge), 'y2': y2},
                }
        return None, {
            'matched': False,
            'method': 'form-select-label-row',
            'label': label,
            'reason': 'label-row-not-visible',
        }

    def _scroll_to_picker_entry_target(
        self,
        step: Dict[str, Any],
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        detail: Dict[str, Any] = {
            'matched': False,
            'method': 'scroll-to-picker-entry',
            'attempts': [],
        }
        if self.platform != 'ios':
            detail['reason'] = 'unsupported-platform'
            return None, detail
        if not self._is_picker_entry_step(step):
            detail['reason'] = 'not-picker-entry'
            return None, detail

        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            detail['reason'] = 'invalid-resolution'
            return None, detail

        max_swipes = int(step.get(
            'picker_entry_scroll_attempts',
            self.runtime.get('picker_entry_scroll_attempts', 3),
        ) or 0)
        max_swipes = max(0, min(6, max_swipes))
        if max_swipes <= 0:
            detail['reason'] = 'scroll-disabled'
            return None, detail

        duration = float(step.get(
            'picker_entry_scroll_duration',
            self.runtime.get('picker_entry_scroll_duration', 0.35),
        ))
        wait_after = float(step.get(
            'picker_entry_scroll_wait_after',
            self.runtime.get('picker_entry_scroll_wait_after', 0.45),
        ))
        x = int(round(width * 0.55))
        gestures = [
            ('down', (x, int(round(height * 0.36))), (x, int(round(height * 0.74)))),
            ('up', (x, int(round(height * 0.74))), (x, int(round(height * 0.36)))),
        ]

        config = self._step_element_config(step)
        fingerprint = config.get('fingerprint') or step.get('fingerprint')
        minimum_score = float(config.get('semantic_min_score') or step.get('semantic_min_score') or 36)

        for direction, start, end in gestures:
            for index in range(max_swipes):
                self.ensure_not_stopped()
                try:
                    swipe(start, end, duration=duration)
                except Exception as exc:
                    detail.update({
                        'reason': 'scroll-action-failed',
                        'error': str(exc),
                    })
                    return None, detail
                self._sleep_interruptibly(wait_after)

                nodes = self._dump_current_ui_nodes()
                target, form_detail = self._form_select_entry_target(step, nodes)
                if not (
                    isinstance(target, (list, tuple))
                    and len(target) >= 2
                    and all(isinstance(value, (int, float)) for value in target[:2])
                ):
                    target = None
                semantic_target = None
                semantic_detail = {'matched': False, 'reason': 'no-safe-fingerprint'}
                if not target and fingerprint_is_replay_safe(fingerprint):
                    semantic_target, semantic_detail = self._semantic_target(
                        fingerprint,
                        minimum_score=minimum_score,
                    )

                resolved_target = target or semantic_target
                item = {
                    'direction': direction,
                    'index': index + 1,
                    'matched': bool(resolved_target),
                    'form_select': form_detail,
                    'semantic': semantic_detail,
                }
                detail['attempts'].append(item)
                if resolved_target:
                    detail.update({
                        'matched': True,
                        'target': list(resolved_target),
                        'scroll_count': index + 1,
                        'direction': direction,
                    })
                    return resolved_target, detail

        detail['reason'] = 'picker-entry-not-found-after-scroll'
        return None, detail

    def _visible_node_center(self, text: str, nodes: Optional[List[Dict[str, Any]]] = None):
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            if any(text in str(value or '') for value in self._node_text_values(node)):
                center = self._node_center_tuple(node)
                if center:
                    return (int(center[0]), int(center[1])), node
        return None, None

    def _ios_primary_transition_target(
        self,
        step: Dict[str, Any],
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        if self.platform != 'ios':
            return None, {'matched': False, 'reason': 'unsupported-platform'}
        if not (
            self._is_input_action_step(step)
            or self._step_targets_text_input(step)
            or self._is_picker_entry_step(step)
        ):
            return None, {'matched': False, 'reason': 'not-transition-target-step'}

        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            return None, {'matched': False, 'reason': 'invalid-resolution'}

        labels = ('同意并申请', '提交申请', '确认申请', '立即申请')
        candidates = []
        for node in nodes or []:
            if node.get('visible', True) is False or node.get('enabled') is False:
                continue
            values = self._node_text_values(node)
            matched_label = next(
                (label for label in labels if any(label in value for value in values)),
                '',
            )
            if not matched_label:
                continue
            center = self._node_center_tuple(node)
            bounds = self._node_bounds_tuple(node)
            if not center or not bounds:
                continue
            x1, y1, x2, y2 = bounds
            node_width = x2 - x1
            node_height = y2 - y1
            if node_width < width * 0.35 or node_height < 40:
                continue
            if center[1] < height * 0.45:
                continue
            candidates.append((
                -node_width,
                center[1],
                center,
                matched_label,
                node,
                bounds,
            ))

        if not candidates:
            return None, {'matched': False, 'reason': 'primary-transition-not-visible'}

        candidates.sort()
        _width_rank, _cy, center, label, node, bounds = candidates[0]
        target = (int(round(center[0])), int(round(center[1])))
        return target, {
            'matched': True,
            'method': 'ios-primary-transition',
            'label': label,
            'target': list(target),
            'class_name': node.get('class_name'),
            'bounds': [int(round(value)) for value in bounds],
        }

    def _recover_input_target_via_primary_transition(
        self,
        step: Dict[str, Any],
        attempts: List[Dict[str, Any]],
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        transition_target, transition_detail = self._ios_primary_transition_target(
            step,
            nodes,
        )
        detail = {
            'matched': False,
            'method': 'ios-primary-transition-retry',
            'transition': transition_detail,
        }
        if not transition_target:
            detail['reason'] = transition_detail.get('reason') or 'transition-target-not-found'
            return None, detail

        logger.info(
            "输入目标未出现，点击 iOS 过渡主按钮后重试定位: target=%s detail=%s",
            transition_target,
            transition_detail,
        )
        touch(transition_target)
        self._sleep_interruptibly(float(step.get(
            'primary_transition_wait_after',
            self.runtime.get('primary_transition_wait_after', 1.2),
        )))
        self._wait_for_page_stable({**step, 'page_stable_timeout': 0.8})
        self._guard_ios_input_no_blocking_modal(step, 'after_primary_transition')

        input_target, input_detail = self._input_control_target(step)
        retry_attempts = [{
            'strategy': 'input_control_after_primary_transition',
            'matched': bool(input_target),
            'elapsed': 0,
            'detail': input_detail,
        }]
        if not input_target:
            scrolled_target, scrolled_detail = self._scroll_to_input_control_target(step)
            retry_attempts.append({
                'strategy': 'scroll_to_input_control_after_primary_transition',
                'matched': bool(scrolled_target),
                'elapsed': 0,
                'detail': scrolled_detail,
            })
            input_target = scrolled_target
            input_detail = scrolled_detail

        detail.update({
            'matched': bool(input_target),
            'target': list(input_target) if input_target else None,
            'input_detail': input_detail,
            'retry_attempts': retry_attempts,
        })
        attempts.extend(retry_attempts)
        return input_target, detail

    def _ensure_ios_agreement_checked_for_transition(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if self.platform != 'ios':
            return {'matched': False, 'reason': 'unsupported-platform'}
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        candidates = []
        for node in nodes or []:
            if node.get('visible', True) is False or not self._is_checkbox_node(node):
                continue
            values = ' '.join(str(value or '') for value in self._node_text_values(node))
            normalized = re.sub(r'\s+', '', values)
            if not any(keyword in normalized for keyword in ('本人已阅读', '同意签署', '隐私政策', '服务许可协议', '授权书')):
                continue
            bounds = self._node_bounds_tuple(node)
            if not bounds:
                continue
            state = self._checkbox_state_from_node(node)
            candidates.append((bounds[1], node, state))
        if not candidates:
            return {'matched': False, 'reason': 'agreement-checkbox-not-visible'}
        candidates.sort(key=lambda item: item[0])
        _y, node, state = candidates[0]
        if state is True:
            return {'matched': True, 'already_checked': True}
        target = self._checkbox_click_target(node, self._node_center_tuple(node))
        if not target:
            return {'matched': False, 'reason': 'agreement-checkbox-target-missing'}
        logger.info('过渡前勾选协议复选框: target=%s', target)
        touch(target)
        self._sleep_interruptibly(0.35)
        return {
            'matched': True,
            'already_checked': False,
            'target': list(target),
            'before': state,
        }

    def _recover_picker_entry_via_primary_transition(
        self,
        step: Dict[str, Any],
        previous_error: Exception,
    ) -> Tuple[Optional[Tuple[int, int]], Dict[str, Any]]:
        detail: Dict[str, Any] = {
            'matched': False,
            'method': 'picker-entry-primary-transition-retry',
            'previous_error': str(previous_error),
        }
        if self.platform != 'ios':
            detail['reason'] = 'unsupported-platform'
            return None, detail
        if not self._is_picker_entry_step(step):
            detail['reason'] = 'not-picker-entry'
            return None, detail

        nodes = self._dump_current_ui_nodes()
        agreement_detail = self._ensure_ios_agreement_checked_for_transition(nodes)
        if agreement_detail.get('matched') and not agreement_detail.get('already_checked'):
            nodes = self._dump_current_ui_nodes()
        transition_target, transition_detail = self._ios_primary_transition_target(step, nodes)
        detail['agreement'] = agreement_detail
        detail['transition'] = transition_detail
        if not transition_target:
            detail['reason'] = transition_detail.get('reason') or 'transition-target-not-found'
            return None, detail

        logger.info(
            'Picker 入口未出现，点击 iOS 过渡主按钮后重试定位: target=%s detail=%s',
            transition_target,
            transition_detail,
        )
        touch(transition_target)
        self._sleep_interruptibly(float(step.get(
            'primary_transition_wait_after',
            self.runtime.get('primary_transition_wait_after', 1.2),
        )))
        self._wait_for_page_stable({**step, 'page_stable_timeout': 0.8})

        fresh_nodes = self._dump_current_ui_nodes()
        target, form_detail = self._form_select_entry_target(step, fresh_nodes)
        retry_attempts = [{
            'strategy': 'form_select_label_row_after_primary_transition',
            'matched': bool(target),
            'detail': form_detail,
        }]
        if not target:
            target, scroll_detail = self._scroll_to_picker_entry_target(step)
            retry_attempts.append({
                'strategy': 'scroll_to_picker_entry_after_primary_transition',
                'matched': bool(target),
                'detail': scroll_detail,
            })
        detail.update({
            'matched': bool(target),
            'target': list(target) if target else None,
            'retry_attempts': retry_attempts,
        })
        if not target:
            detail['reason'] = 'picker-entry-not-found-after-primary-transition'
        return target, detail

    def _clear_optional_blocking_modal_if_needed(self, step: Dict[str, Any]) -> Dict[str, Any]:
        if self.platform != 'ios':
            return {'matched': False, 'reason': 'unsupported-platform'}
        haystack = self._step_text_haystack(step)
        if any(keyword in haystack for keyword in (
            '上传行驶证', '重新输入车牌号', '符合条件的车辆信息',
        )):
            return {'matched': False, 'reason': 'step-targets-modal'}

        nodes = self._dump_current_ui_nodes()
        visible_text = ' '.join(
            value
            for node in nodes or []
            if node.get('visible', True) is not False
            for value in self._node_text_values(node)
        )
        modal_keywords = (
            '此身份证未找到符合条件的车辆信息',
            '可上传行驶证辅助验证',
            '上传行驶证',
            '重新输入车牌号',
            '请先阅读并同意签署协议',
        )
        if not any(keyword in visible_text for keyword in modal_keywords):
            return {'matched': False, 'reason': 'modal-not-visible'}

        target = None
        close_detail: Dict[str, Any] = {'method': ''}
        for close_text in ('返回', '关闭', '×', '✕', 'X'):
            target, node = self._visible_node_center(close_text, nodes)
            if target:
                close_detail = {
                    'method': 'visible-close-node',
                    'text': close_text,
                    'class_name': node.get('class_name') if node else '',
                }
                break

        if not target:
            width, height = parse_resolution(self._get_current_resolution())
            if width <= 0 or height <= 0:
                return {'matched': False, 'reason': 'modal-close-target-not-found'}
            target = (int(round(width * 0.895)), int(round(height * 0.25)))
            close_detail = {
                'method': 'semantic-modal-top-right-close',
                'reason': 'close-node-not-exposed',
            }

        logger.info("关闭非目标阻塞弹窗: target=%s detail=%s", target, close_detail)
        touch(target)
        self._sleep_interruptibly(0.8)
        return {
            'matched': True,
            'target': list(target),
            **close_detail,
        }

    def _clear_restored_form_state_if_needed(self, step: Dict[str, Any]) -> Dict[str, Any]:
        haystack = self._step_text_haystack(step)
        if any(action in haystack for action in FORM_SELECT_ACTION_LABELS):
            return {'matched': False, 'reason': 'confirmation-step'}
        if not any(keyword in haystack for keyword in (
            '申请地区', '贷款用途', '身份证', '选择图片', '同意申请',
        )):
            return {'matched': False, 'reason': 'step-not-sensitive-to-restored-state'}
        nodes = self._dump_current_ui_nodes()
        visible_text = ' '.join(
            value
            for node in nodes or []
            if node.get('visible', True) is not False
            for value in self._node_text_values(node)
        )
        downstream_supplement_page = (
            '详细地址' in visible_text
            and any(keyword in visible_text for keyword in ('工作信息', '职业类型', '单位名称'))
        )
        restored_state_visible = '恢复上一次填写' in visible_text or '一键清空' in visible_text
        if not restored_state_visible and not downstream_supplement_page:
            return {'matched': False, 'reason': 'restored-state-not-visible'}
        clear_target, _ = self._visible_node_center('一键清空', nodes)
        if clear_target:
            logger.info("检测到恢复上次填写状态，点击一键清空: %s", clear_target)
            touch(clear_target)
            self._sleep_interruptibly(0.8)
        nodes_after_clear = self._dump_current_ui_nodes()
        back_target, _ = self._visible_node_center('上一步', nodes_after_clear)
        if not back_target and downstream_supplement_page:
            width, height = parse_resolution(self._get_current_resolution())
            if width > 0 and height > 0:
                back_target = (int(width * 0.91), int(height * 0.70))
        if back_target:
            logger.info("恢复态清理后返回上一页以对齐录制流程: %s", back_target)
            touch(back_target)
            self._sleep_interruptibly(1.0)
        return {
            'matched': True,
            'method': 'clear-restored-form-state',
            'downstream_supplement_page': downstream_supplement_page,
            'clear_target': list(clear_target) if clear_target else None,
            'back_target': list(back_target) if back_target else None,
        }

    def _ios_picker_dialog_visible(self, nodes: Optional[List[Dict[str, Any]]] = None) -> bool:
        if self.platform != 'ios':
            return False
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        if self._license_plate_keyboard_visible(nodes):
            return False
        visible_values = []
        visible_classes = []
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            visible_values.extend(self._node_text_values(node))
            visible_classes.append(str(node.get('class_name') or ''))
        visible_text = ' '.join(visible_values)
        if not all(keyword in visible_text for keyword in ('确认', '取消')):
            return False
        if '网页对话框' in visible_text:
            return True
        if any('Picker' in class_name for class_name in visible_classes):
            return True
        picker_values = {
            '汉', '满', '蒙古', '回', '藏', '维吾尔', '苗', '彝', '壮', '布依',
            '朝鲜', '侗', '瑶', '白', '土家', '哈尼', '哈萨克', '傣', '黎',
        }
        return bool({value.strip() for value in visible_values} & picker_values)

    def _ios_input_blocking_modal(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        if self.platform != 'ios':
            return '', nodes or []
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        if self._ios_picker_dialog_visible(nodes):
            return 'picker-dialog', nodes
        overlay = self._detect_ios_system_overlay(nodes)
        if overlay:
            return overlay, nodes
        visible_text = ' '.join(
            value
            for node in nodes or []
            if node.get('visible', True) is not False
            for value in self._node_text_values(node)
        )
        vehicle_modal_keywords = (
            '此身份证未找到符合条件的车辆信息',
            '可上传行驶证',
            '重新输入车牌号',
        )
        if any(keyword in visible_text for keyword in vehicle_modal_keywords):
            return 'vehicle-verification-modal', nodes
        if OCR_AVAILABLE:
            try:
                screen = G.DEVICE.snapshot()
                if screen is not None:
                    ocr_text = get_ocr_helper(
                        languages=['ch_sim', 'en'],
                        use_gpu=False,
                    ).recognize_text(screen, min_confidence=0.35, use_cache=False)
                    if any(keyword in ocr_text for keyword in vehicle_modal_keywords):
                        return 'vehicle-verification-modal', nodes
            except Exception as exc:
                logger.debug('iOS 输入阻断弹窗 OCR 检测失败: %s', exc)
        return '', nodes

    def _guard_ios_input_no_blocking_modal(self, step: Dict[str, Any], stage: str) -> None:
        modal, nodes = self._ios_input_blocking_modal()
        if not modal:
            return
        diagnostics = {
            'matched': False,
            'stage': stage,
            'modal': modal,
            'node_count': len(nodes),
        }
        self._last_locator_diagnostics = {
            **(self._last_locator_diagnostics or {}),
            'input_blocked': diagnostics,
        }
        raise AssertionError(
            f"iOS 输入被阻止: 检测到 {modal} 弹层仍打开，"
            "已停止输入避免状态错位"
        )

    def _is_supplement_completion_step(self, step: Dict[str, Any]) -> bool:
        haystack = self._step_text_haystack(step)
        return '完成补充' in haystack or '补充信息完成' in haystack

    def _confirmed_form_select_label(self, step: Dict[str, Any]) -> str:
        haystack = self._step_text_haystack(step)
        if '确认' not in haystack:
            return ''
        for label in FORM_SELECT_PLACEHOLDER_LABELS:
            if label in haystack:
                return label
        return ''

    def _form_field_still_placeholder(
        self,
        label: str,
        placeholder: str = '请选择',
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        return self._form_field_selection_state(label, placeholder, nodes) == 'placeholder'

    def _form_field_selection_state(
        self,
        label: str,
        placeholder: str = '请选择',
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        label = str(label or '').strip()
        placeholder = str(placeholder or '').strip()
        if not label or not placeholder:
            return 'missing'
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        label_nodes = []
        row_value_nodes = []
        for node in nodes or []:
            if node.get('visible', True) is False:
                continue
            values = [str(value or '').strip() for value in self._node_text_values(node)]
            values = [value for value in values if value]
            if any(label in value for value in values):
                for value in values:
                    if label not in value:
                        continue
                    remaining = (
                        value
                        .replace(label, '')
                        .replace('', '')
                        .replace('>', '')
                        .replace('›', '')
                        .replace('〉', '')
                        .strip()
                    )
                    if (
                        remaining
                        and placeholder not in remaining
                        and remaining not in FORM_SELECT_ACTION_LABELS
                    ):
                        return 'selected'
                label_nodes.append(node)
            if any(placeholder in value for value in values):
                row_value_nodes.append((node, 'placeholder'))
                continue
            actionable_values = [
                value for value in values
                if value not in FORM_SELECT_ACTION_LABELS
                and label not in value
                and value not in {'>', '›', '〉'}
            ]
            if actionable_values:
                row_value_nodes.append((node, 'selected'))

        for label_node in label_nodes:
            label_bounds = self._node_bounds_tuple(label_node)
            if not label_bounds:
                continue
            lx1, ly1, lx2, ly2 = label_bounds
            label_cy = (ly1 + ly2) / 2
            label_height = max(1.0, ly2 - ly1)
            for value_node, state in row_value_nodes:
                value_bounds = self._node_bounds_tuple(value_node)
                if not value_bounds:
                    continue
                vx1, vy1, vx2, vy2 = value_bounds
                value_cy = (vy1 + vy2) / 2
                if vx1 <= lx2:
                    continue
                if abs(value_cy - label_cy) <= max(label_height, vy2 - vy1) * 1.2:
                    return state
        if label_nodes:
            return 'selected'
        return 'missing'

    def _wait_until_form_field_selected(
        self,
        label: str,
        timeout: float,
        interval: float = 0.5,
    ) -> str:
        deadline = time.monotonic() + max(0.0, timeout)
        last_state = 'missing'
        while True:
            self.ensure_not_stopped()
            last_state = self._form_field_selection_state(label)
            if last_state == 'selected':
                return last_state
            if time.monotonic() >= deadline:
                return last_state
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))

    def _visible_text_exists(self, expected_text: str) -> bool:
        expected = str(expected_text or '').strip()
        if not expected:
            return False
        for node in self._dump_current_ui_nodes():
            if node.get('visible') is False:
                continue
            for actual in self._node_text_values(node):
                if expected in str(actual or ''):
                    return True
        return False

    def _wait_for_visible_text(self, expected_text: str, timeout: float, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self.ensure_not_stopped()
            if self._visible_text_exists(expected_text):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))

    def _wait_until_text_gone(self, forbidden_text: str, timeout: float, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self.ensure_not_stopped()
            if not self._visible_text_exists(forbidden_text):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))

    def _verify_click_postcondition(self, step: Dict[str, Any]):
        if step.get('skip_click_postcondition') is True:
            return
        timeout = float(step.get('post_click_timeout', 10))
        if self._is_supplement_completion_step(step):
            if not self._wait_for_visible_text('去签署', timeout):
                raise AssertionError("点击完成补充后未进入签署页：未出现 '去签署'")
            logger.info("点击完成补充后置校验通过: 已出现 '去签署'")
        elif self._is_signature_action_step(step):
            if not self._wait_until_text_gone('去签署', timeout):
                raise AssertionError("点击去签署后页面未跳转：'去签署' 仍然可见")
            logger.info("点击去签署后置校验通过: '去签署' 已消失")
        else:
            select_label = self._confirmed_form_select_label(step)
            if select_label:
                select_timeout = float(step.get('form_select_post_timeout', 3))
                select_state = self._wait_until_form_field_selected(select_label, select_timeout)
                if select_state != 'selected':
                    if select_state == 'placeholder':
                        reason = f"字段仍为“请选择”"
                    else:
                        reason = "未在当前页面确认到字段及其已选值"
                    raise AssertionError(
                        f"点击{select_label}确认后{reason}，"
                        f"说明选择未生效，已停止后续步骤避免状态错位"
                    )
                logger.info("表单选择后置校验通过: %s 已不再是“请选择”", select_label)

    @staticmethod
    def _is_chrome_toolbar_fingerprint(fingerprint: Dict[str, Any]) -> bool:
        return bool(
            isinstance(fingerprint, dict)
            and fingerprint.get('package') == CHROME_PACKAGE
            and fingerprint.get('resource_id') in CHROME_TOOLBAR_RESOURCE_IDS
        )

    def _reveal_chrome_toolbar(self, fingerprint: Dict[str, Any], minimum_score: float = 36.0):
        """页面滚动导致 Chrome 工具栏隐藏时，先下拉页面再重新语义定位。"""
        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            return None, {'matched': False, 'reason': 'invalid-resolution'}

        start = (width // 2, max(320, int(height * 0.30)))
        end = (width // 2, min(height - 120, max(start[1] + 360, int(height * 0.58))))
        attempts = []
        for _ in range(2):
            self.ensure_not_stopped()
            swipe(start, end, duration=0.35)
            self._sleep_interruptibly(0.4)
            target, diagnostics = self._semantic_target(fingerprint, minimum_score)
            attempts.append(diagnostics)
            if target:
                return target, {
                    'matched': True,
                    'recovery': 'reveal-chrome-toolbar',
                    'swipe': [list(start), list(end)],
                    'attempts': attempts,
                }
        return None, {
            'matched': False,
            'recovery': 'reveal-chrome-toolbar',
            'swipe': [list(start), list(end)],
            'attempts': attempts,
        }

    def _element_locator_candidates(self, element) -> List[Dict[str, Any]]:
        config = dict(element.config or {})
        candidates: List[Dict[str, Any]] = []
        current_resolution = self._get_current_resolution()

        for strategy in normalize_locator_strategies(config, element.element_type):
            if strategy.get('enabled') is False:
                continue
            strategy_type = strategy.get('type')
            value = strategy.get('value')

            if strategy_type in ('resource_id', 'accessibility', 'text', 'xpath'):
                fingerprint = strategy_fingerprint(config, strategy_type, value)
                if not fingerprint:
                    continue
                minimum_scores = {
                    'resource_id': 45,
                    'accessibility': 36,
                    'text': 30,
                    'xpath': 30,
                }
                candidates.append({
                    'strategy': strategy_type,
                    'kind': 'semantic',
                    'fingerprint': fingerprint,
                    'minimum_score': float(
                        strategy.get('minimum_score')
                        or config.get(f'{strategy_type}_min_score')
                        or config.get('semantic_min_score')
                        or minimum_scores[strategy_type]
                    ),
                })
                continue

            if strategy_type == 'ocr':
                ocr_text = str(value or config.get('ocr_text') or config.get('text') or '').strip()
                if ocr_text:
                    candidates.append({
                        'strategy': 'ocr',
                        'kind': 'ocr',
                        'text': ocr_text,
                        'match_mode': strategy.get('match_mode') or config.get('ocr_match_mode', 'exact'),
                        'min_confidence': float(
                            strategy.get('min_confidence') or config.get('ocr_min_confidence', 0.5)
                        ),
                        'languages': strategy.get('languages') or config.get('ocr_languages') or ['ch_sim', 'en'],
                        'region': config.get('ocr_region'),
                    })
                continue

            if strategy_type == 'image':
                image_rel_path = str(value or config.get('image_path') or '')
                image_path = os.path.join(self.image_base_dir, image_rel_path) if image_rel_path else ''
                if image_path and os.path.isfile(image_path):
                    candidates.append({
                        'strategy': 'image',
                        'kind': 'image',
                        'target': self._create_template(image_path, config),
                        'path': image_path,
                    })
                elif image_path:
                    logger.warning(f"图片文件不存在: {image_path}")
                continue

            if strategy_type == 'position':
                point = None
                bounds = None
                if element.element_type == 'pos':
                    point = scale_point(config, current_resolution)
                elif element.element_type == 'region':
                    bounds = scale_region(config, current_resolution)
                    point = region_center(bounds)
                if point is None and isinstance(config.get('fallback_position'), dict):
                    point = scale_point({
                        'normalized_position': config.get('fallback_position'),
                        'source_resolution': config.get('source_resolution'),
                    }, current_resolution)
                if point is None and isinstance(config.get('normalized_position'), dict):
                    point = scale_point(config, current_resolution)
                if point is None:
                    region_config = dict(config)
                    if not region_config.get('normalized_region') and config.get('normalized_bounds'):
                        region_config['normalized_region'] = config.get('normalized_bounds')
                    bounds = scale_region(region_config, current_resolution)
                    point = region_center(bounds)
                if point:
                    candidates.append({
                        'strategy': 'position',
                        'kind': 'static',
                        'target': point,
                        'bounds': bounds,
                    })
        return candidates

    def _step_locator_candidates(self, step: Dict[str, Any]) -> List[Dict[str, Any]]:
        element_id = step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if element:
                return self._element_locator_candidates(element)
            logger.warning(
                "元素引用已失效，降级使用步骤内定位配置: element_id=%s, step=%s",
                element_id,
                step.get('name', step.get('type', 'unknown')),
            )

        config = self._step_element_config(step)
        candidates: List[Dict[str, Any]] = []
        fingerprint = config.get('fingerprint')
        if fingerprint_is_replay_safe(fingerprint):
            candidates.append({
                'strategy': 'semantic_fingerprint',
                'kind': 'semantic',
                'fingerprint': fingerprint,
                'minimum_score': float(config.get('semantic_min_score', 36)),
            })

        # 智能组件可直接配置完整定位链，而不必先创建 AppElement。
        # 每个策略都必须携带自己的值，执行时严格按数组顺序降级。
        if isinstance(config.get('locator_strategies'), list):
            selector_type = str(config.get('selector_type') or '').strip().lower()
            selector_value = config.get('selector')
            selector_field_map = {
                'id': 'resource_id',
                'resource_id': 'resource_id',
                'accessibility': 'accessibility_id',
                'accessibility_id': 'accessibility_id',
                'text': 'text',
                'xpath': 'xpath',
                'ocr': 'ocr_text',
                'image': 'image_path',
                'pos': 'fallback_position',
                'position': 'fallback_position',
            }
            mapped_field = selector_field_map.get(selector_type)
            if mapped_field and selector_value not in (None, '') and not config.get(mapped_field):
                config[mapped_field] = selector_value

            for strategy in normalize_locator_strategies(config, selector_type):
                if strategy.get('enabled') is False:
                    continue
                strategy_type = str(strategy.get('type') or '').lower()
                value = strategy.get('value')
                if value in (None, '', {}):
                    continue
                if strategy_type in ('resource_id', 'accessibility', 'text', 'xpath'):
                    strategy_fingerprint_value = strategy_fingerprint(
                        config, strategy_type, value
                    )
                    if strategy_fingerprint_value:
                        candidates.append({
                            'strategy': strategy_type,
                            'kind': 'semantic',
                            'fingerprint': strategy_fingerprint_value,
                            'minimum_score': float(
                                strategy.get('minimum_score')
                                or config.get('semantic_min_score', 36)
                            ),
                        })
                    continue
                if strategy_type == 'ocr':
                    candidates.append({
                        'strategy': 'ocr',
                        'kind': 'ocr',
                        'text': str(value),
                        'match_mode': strategy.get('match_mode') or config.get('match_mode', 'exact'),
                        'min_confidence': float(
                            strategy.get('min_confidence')
                            or config.get('ocr_min_confidence', 0.5)
                        ),
                        'languages': strategy.get('languages') or config.get('ocr_languages') or ['ch_sim', 'en'],
                        'region': strategy.get('region') or config.get('ocr_region'),
                    })
                    continue
                if strategy_type == 'image':
                    image_path = str(value)
                    if not os.path.isabs(image_path):
                        if os.path.dirname(image_path):
                            image_path = os.path.join(self.image_base_dir, image_path)
                        else:
                            image_path = os.path.join(
                                self.image_base_dir,
                                str(config.get('image_scope') or 'common'),
                                image_path,
                            )
                    if os.path.isfile(image_path):
                        candidates.append({
                            'strategy': 'image',
                            'kind': 'image',
                            'target': self._create_template(image_path, config),
                        })
                    continue
                if strategy_type == 'position':
                    position_config: Dict[str, Any]
                    if isinstance(value, dict):
                        position_config = {'normalized_position': value}
                    elif isinstance(value, (list, tuple)) and len(value) >= 2:
                        position_config = {
                            'normalized_position': {'x': value[0], 'y': value[1]}
                        }
                    else:
                        position_config = {
                            'selector_type': 'pos',
                            'selector': value,
                            'source_resolution': config.get('source_resolution'),
                        }
                    target = (
                        scale_point(position_config, self._get_current_resolution())
                        if 'normalized_position' in position_config
                        else self._resolve_selector(position_config)
                    )
                    if target:
                        candidates.append({
                            'strategy': 'position',
                            'kind': 'static',
                            'target': target,
                        })
            return candidates

        selector_type = str(config.get('selector_type', 'image') or 'image').strip().lower()
        selector_value = config.get('selector', '')
        semantic_types = {
            'id': 'resource_id',
            'resource_id': 'resource_id',
            'accessibility': 'accessibility',
            'accessibility_id': 'accessibility',
            'content_desc': 'accessibility',
            'text': 'text',
            'xpath': 'xpath',
        }
        semantic_type = semantic_types.get(selector_type)
        if semantic_type and selector_value not in (None, ''):
            semantic_config = {
                'resource_id': selector_value if semantic_type == 'resource_id' else '',
                'accessibility_id': selector_value if semantic_type == 'accessibility' else '',
                'text': selector_value if semantic_type == 'text' else '',
                'xpath': selector_value if semantic_type == 'xpath' else '',
                'fingerprint': fingerprint if isinstance(fingerprint, dict) else {},
            }
            semantic_fingerprint = strategy_fingerprint(
                semantic_config,
                semantic_type,
                selector_value,
            )
            if semantic_fingerprint:
                candidates.append({
                    'strategy': semantic_type,
                    'kind': 'semantic',
                    'fingerprint': semantic_fingerprint,
                    'minimum_score': float(config.get('semantic_min_score', 36)),
                })
        elif selector_type == 'ocr' and selector_value not in (None, ''):
            candidates.append({
                'strategy': 'ocr',
                'kind': 'ocr',
                'text': str(selector_value),
                'match_mode': config.get('match_mode', 'exact'),
                'min_confidence': float(config.get('ocr_min_confidence', 0.5)),
                'languages': config.get('ocr_languages') or ['ch_sim', 'en'],
                'region': config.get('ocr_region'),
            })

        target = None if semantic_type or selector_type == 'ocr' else self._resolve_selector(config)
        if target is not None:
            strategy = 'image' if isinstance(target, Template) else 'position'
            if isinstance(target, tuple) and len(target) >= 4:
                target = region_center(target)
                strategy = 'region_center'
            if target is not None:
                candidates.append({
                    'strategy': strategy,
                    'kind': 'image' if strategy == 'image' else 'static',
                    'target': target,
                })
        return candidates

    def _ocr_target(self, candidate: Dict[str, Any]):
        if not OCR_AVAILABLE:
            return None, {'matched': False, 'reason': 'ocr-module-unavailable'}
        languages = candidate.get('languages') or ['ch_sim', 'en']
        helper = get_ocr_helper(languages=languages, use_gpu=False)
        region = candidate.get('region')
        if isinstance(region, dict):
            region = scale_region(region, self._get_current_resolution())
        elif isinstance(region, (list, tuple)) and len(region) >= 4:
            region = tuple(int(value) for value in region[:4])
        else:
            region = None
        return helper.find_text(
            candidate.get('text', ''),
            match_mode=candidate.get('match_mode', 'exact'),
            min_confidence=float(candidate.get('min_confidence', 0.5)),
            region=region,
        )

    @staticmethod
    def _canonical_locator_strategy(strategy: Any) -> str:
        normalized = str(strategy or '').strip().lower()
        aliases = {
            'webview_css': 'css',
            'css_selector': 'css',
            'accessibility_id': 'accessibility',
            'content_desc': 'accessibility',
            'id': 'resource_id',
            'region_center': 'position',
            'pos': 'position',
        }
        return aliases.get(normalized, normalized)

    def _record_self_heal(
        self,
        step: Dict[str, Any],
        selected_strategy: Any,
        attempts: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """记录备用定位策略命中，并按显式配置决定是否持久化新优先级。"""
        mode = str(step.get('self_heal_mode') or 'off').strip().lower()
        if mode not in ('report', 'persist'):
            return None

        selected = self._canonical_locator_strategy(selected_strategy)
        normalized_attempts = []
        for item in attempts or []:
            if not isinstance(item, dict):
                continue
            normalized_attempts.append({
                'strategy': self._canonical_locator_strategy(item.get('strategy')),
                'matched': bool(item.get('matched')),
                'error': item.get('error') or (item.get('detail') or {}).get('error'),
            })

        element = None
        configured_strategies = []
        element_id = step.get('element_id')
        if element_id:
            try:
                from ..models import AppElement
                element = AppElement.objects.filter(id=element_id, is_active=True).first()
                if element:
                    configured_strategies = normalize_locator_strategies(
                        element.config or {}, element.element_type
                    )
            except Exception as exc:
                logger.warning('读取自愈元素失败: element_id=%s, error=%s', element_id, exc)

        primary = ''
        if configured_strategies:
            primary = self._canonical_locator_strategy(configured_strategies[0].get('type'))
        if not primary and normalized_attempts:
            primary = normalized_attempts[0]['strategy']

        selected_index = next(
            (
                index for index, item in enumerate(normalized_attempts)
                if item['strategy'] == selected and item['matched']
            ),
            -1,
        )
        triggered = bool(selected and primary and selected != primary) or selected_index > 0
        detail = {
            'mode': mode,
            'triggered': triggered,
            'element_id': element_id,
            'from_strategy': primary,
            'to_strategy': selected,
            'persisted': False,
        }
        if not triggered or mode != 'persist' or not element:
            return detail
        if selected == 'position' and not step.get('allow_coordinate_persist', False):
            detail['persist_skipped'] = 'coordinate-promotion-disabled'
            return detail

        strategy_index = next(
            (
                index for index, item in enumerate(configured_strategies)
                if self._canonical_locator_strategy(item.get('type')) == selected
            ),
            -1,
        )
        if strategy_index < 0:
            detail['persist_skipped'] = 'selected-strategy-not-configured'
            return detail

        try:
            from django.utils import timezone

            reordered = [configured_strategies[strategy_index]] + [
                item for index, item in enumerate(configured_strategies)
                if index != strategy_index
            ]
            stored_strategies = []
            for index, item in enumerate(reordered, 1):
                stored = {
                    key: value for key, value in item.items()
                    if key != 'priority'
                }
                stored['priority'] = index
                stored_strategies.append(stored)

            config = copy.deepcopy(element.config or {})
            metadata = copy.deepcopy(config.get('self_heal') or {})
            history = list(metadata.get('history') or [])[-19:]
            event = {
                'at': timezone.now().isoformat(),
                'execution_id': self.execution_id,
                'from_strategy': primary,
                'to_strategy': selected,
            }
            history.append(event)
            metadata.update({
                'heal_count': int(metadata.get('heal_count') or 0) + 1,
                'last_event': event,
                'history': history,
            })
            config['locator_strategies'] = stored_strategies
            config['self_heal'] = metadata
            element.config = config
            element.save(update_fields=['config', 'updated_at'])
            detail['persisted'] = True
            logger.info(
                '元素定位策略已自愈: element_id=%s, %s -> %s',
                element_id, primary, selected,
            )
        except Exception as exc:
            detail['persist_error'] = str(exc)
            logger.exception('持久化自愈定位策略失败: element_id=%s', element_id)
        return detail

    def _resolve_action_target(self, step: Dict[str, Any]) -> Any:
        """按语义、图像、归一化坐标的顺序解析实际操作点。"""
        self._wait_for_page_stable(step)
        candidates = self._step_locator_candidates(step)
        timeout = max(0.1, float(step.get('timeout', self.runtime.get('locator_timeout', 5))))
        deadline = time.monotonic() + timeout
        attempts = []
        static_candidates = []
        chrome_toolbar_target = False
        prefer_input_control = bool(
            step.get('prefer_input_control')
            or self._is_input_action_step(step)
            or self._step_targets_text_input(step)
        )
        if prefer_input_control:
            self._guard_ios_input_no_blocking_modal(step, 'before_locate')
        if not prefer_input_control:
            optional_modal_detail = self._clear_optional_blocking_modal_if_needed(step)
            if optional_modal_detail.get('matched'):
                self._wait_for_page_stable({**step, 'page_stable_timeout': 0.8})

        if self.platform == 'ios' and self._is_ios_photo_asset_selection_step(step):
            fresh_nodes = self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(fresh_nodes)
            overlay_target, overlay_detail = self._ios_photo_upload_overlay_target(
                step, fresh_nodes, overlay
            )
            if overlay_target:
                attempts.append({
                    'strategy': 'ios_photo_upload_overlay',
                    'matched': True,
                    'elapsed': 0,
                    'detail': overlay_detail,
                })
                heal_detail = self._record_self_heal(step, 'ios_photo_upload_overlay', attempts)
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': 'ios_photo_upload_overlay',
                    'target': list(overlay_target),
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'self_heal': heal_detail,
                }
                logger.info(
                    "iOS 照片上传覆盖层定位成功: overlay=%s target=%s detail=%s",
                    overlay, overlay_target, overlay_detail,
                )
                return overlay_target
            upload_slot = self._ios_identity_photo_slot_from_step(step)
            if upload_slot and not overlay:
                width, height = parse_resolution(self._get_current_resolution())
                upload_target = self._ios_identity_upload_frame_estimate(
                    upload_slot,
                    width,
                    height,
                )
                if upload_target:
                    attempts.append({
                        'strategy': 'ios_identity_upload_frame',
                        'matched': True,
                        'elapsed': 0,
                        'detail': {
                            'matched': True,
                            'method': 'identity-upload-frame-estimate',
                            'upload_slot': upload_slot,
                            'target': list(upload_target),
                        },
                    })
                    heal_detail = self._record_self_heal(
                        step, 'ios_identity_upload_frame', attempts
                    )
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'ios_identity_upload_frame',
                        'target': list(upload_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info(
                        "iOS 身份证上传框定位成功: slot=%s target=%s",
                        upload_slot,
                        upload_target,
                    )
                    return upload_target
            if self._ios_pending_photo_upload_slot and not overlay:
                menu_target, menu_detail = self._ios_open_photo_upload_menu_target(step)
                attempts.append({
                    'strategy': 'ios_photo_upload_menu_reopen',
                    'matched': bool(menu_target),
                    'elapsed': 0,
                    'detail': menu_detail,
                })
                if menu_target:
                    heal_detail = self._record_self_heal(
                        step, 'ios_photo_upload_menu_reopen', attempts
                    )
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'ios_photo_upload_menu_reopen',
                        'target': list(menu_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info(
                        "iOS 身份证上传菜单恢复成功: target=%s detail=%s",
                        menu_target,
                        menu_detail,
                    )
                    return menu_target
                upload_target = menu_detail.get('upload_target')
                if (
                    self._is_ios_photo_upload_menu_opener_step(step)
                    and isinstance(upload_target, list)
                    and len(upload_target) >= 2
                ):
                    retry_target = (
                        int(round(float(upload_target[0]))),
                        int(round(float(upload_target[1]))),
                    )
                    retry_detail = {
                        'matched': True,
                        'method': 'identity-upload-frame-retry-target',
                        'upload_slot': self._ios_pending_photo_upload_slot,
                        'target': list(retry_target),
                        'previous_open_menu': menu_detail,
                        'reason': 'defer-menu-open-to-explicit-upload-frame-tap',
                    }
                    attempts.append({
                        'strategy': 'ios_identity_upload_frame_retry',
                        'matched': True,
                        'elapsed': 0,
                        'detail': retry_detail,
                    })
                    heal_detail = self._record_self_heal(
                        step, 'ios_identity_upload_frame_retry', attempts
                    )
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'ios_identity_upload_frame_retry',
                        'target': list(retry_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info(
                        "iOS 身份证上传菜单未出现，使用受限上传框重试目标: target=%s detail=%s",
                        retry_target,
                        retry_detail,
                    )
                    return retry_target
            if self._ios_pending_photo_upload_slot:
                block_detail = {
                    'overlay': overlay,
                    'upload_slot': self._ios_pending_photo_upload_slot,
                    'reason': 'ios-identity-photo-flow-blocked-ordinary-fallback',
                }
                if overlay_detail:
                    block_detail['overlay_detail'] = overlay_detail
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': None,
                    'target': None,
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'blocked_reason': block_detail['reason'],
                    'blocked_detail': block_detail,
                }
                raise ValueError(
                    f"步骤 '{step.get('name', step.get('type', 'unknown'))}' "
                    "处于 iOS 身份证照片上传流程，但未定位到照片图库/系统上传菜单，"
                    "已阻止普通语义或坐标兜底"
                )

        for candidate in candidates:
            strategy = candidate.get('strategy')
            kind = candidate.get('kind') or strategy
            started = time.monotonic()
            if kind == 'static':
                static_candidates.append(candidate)
                continue

            target = None
            detail = {}
            if kind == 'semantic':
                chrome_toolbar_target = self._is_chrome_toolbar_fingerprint(candidate.get('fingerprint'))
                semantic_budget = min(
                    max(0.1, float(self.runtime.get('semantic_locator_timeout', 0.6))),
                    max(0.1, deadline - time.monotonic()),
                )
                semantic_deadline = time.monotonic() + semantic_budget
                while time.monotonic() <= semantic_deadline:
                    self.ensure_not_stopped()
                    target, detail = self._semantic_target(
                        candidate['fingerprint'], candidate.get('minimum_score', 36)
                    )
                    if target:
                        break
                    self._sleep_interruptibly(min(0.25, max(0.0, semantic_deadline - time.monotonic())))
                if not target and chrome_toolbar_target:
                    target, recovery_detail = self._reveal_chrome_toolbar(
                        candidate['fingerprint'], candidate.get('minimum_score', 36)
                    )
                    detail = {
                        'initial': detail,
                        'recovery': recovery_detail,
                    }
            elif kind == 'ocr':
                try:
                    target, detail = self._ocr_target(candidate)
                except Exception as exc:
                    detail = {'matched': False, 'error': str(exc)}
            elif kind == 'image':
                first_image_attempt = True
                while first_image_attempt or time.monotonic() <= deadline:
                    first_image_attempt = False
                    self.ensure_not_stopped()
                    try:
                        target = exists(candidate['target'])
                    except Exception as exc:
                        detail = {'error': str(exc)}
                    if target:
                        break
                    self._sleep_interruptibly(min(0.2, max(0.0, deadline - time.monotonic())))

            attempt = {
                'strategy': strategy,
                'matched': bool(target),
                'elapsed': round(time.monotonic() - started, 3),
                'detail': detail,
            }
            attempts.append(attempt)
            if target:
                if prefer_input_control:
                    input_target, input_detail = self._input_control_target(step, target)
                    attempts.append({
                        'strategy': 'input_control',
                        'matched': bool(input_target),
                        'elapsed': 0,
                        'detail': input_detail,
                    })
                    if input_target:
                        heal_detail = self._record_self_heal(step, 'input_control', attempts)
                        self._last_locator_diagnostics = {
                            'step': step.get('name', step.get('type', 'unknown')),
                            'selected_strategy': 'input_control',
                            'target': list(input_target),
                            'attempts': attempts,
                            'current_resolution': self._get_current_resolution(),
                            'webview': self._last_webview_diagnostics or None,
                            'self_heal': heal_detail,
                        }
                        logger.info("输入控件定位成功: target=%s", input_target)
                        return input_target
                    # 输入动作不能把命中的 label/placeholder 当作输入框点击。
                    # 继续尝试后续策略，最终仍会走输入控件兜底或给出明确失败。
                    continue
                heal_detail = self._record_self_heal(step, strategy, attempts)
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': strategy,
                    'target': list(target) if isinstance(target, tuple) else target,
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'self_heal': heal_detail,
                }
                logger.info(f"稳健定位成功: strategy={strategy}, target={target}")
                return target

        if static_candidates and not chrome_toolbar_target:
            if step.get('guard_security_challenge', True):
                page_state = self._current_page_state()
                page_state.update(self._detect_semantic_page({}, page_state))
                if page_state.get('page_name') == 'security_verify':
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': None,
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'page_state': page_state,
                        'blocked_reason': 'security-challenge-coordinate-fallback',
                    }
                    raise RuntimeError(
                        '检测到安全验证/滑块页面，已阻止坐标兜底点击；'
                        '请人工完成验证后继续执行'
                    )
            if prefer_input_control:
                candidate = static_candidates[0]
                input_target, input_detail = self._input_control_target(
                    step,
                    candidate.get('target'),
                )
                attempts.append({
                    'strategy': 'input_control',
                    'matched': bool(input_target),
                    'elapsed': 0,
                    'detail': input_detail,
                })
                if input_target:
                    heal_detail = self._record_self_heal(step, 'input_control', attempts)
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'input_control',
                        'target': list(input_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info("输入控件定位成功: target=%s", input_target)
                    return input_target
                scrolled_input_target, scrolled_input_detail = self._scroll_to_input_control_target(step)
                attempts.append({
                    'strategy': 'scroll_to_input_control',
                    'matched': bool(scrolled_input_target),
                    'elapsed': 0,
                    'detail': scrolled_input_detail,
                })
                if scrolled_input_target:
                    heal_detail = self._record_self_heal(
                        step, 'scroll_to_input_control', attempts
                    )
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'scroll_to_input_control',
                        'target': list(scrolled_input_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info("滚动后输入控件定位成功: target=%s", scrolled_input_target)
                    return scrolled_input_target
            fresh_nodes = self._dump_current_ui_nodes()
            overlay = self._detect_ios_system_overlay(fresh_nodes)
            overlay_target, overlay_detail = self._ios_photo_upload_overlay_target(
                step, fresh_nodes, overlay
            )
            if overlay_target:
                attempts.append({
                    'strategy': 'ios_photo_upload_overlay',
                    'matched': True,
                    'elapsed': 0,
                    'detail': overlay_detail,
                })
                heal_detail = self._record_self_heal(step, 'ios_photo_upload_overlay', attempts)
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': 'ios_photo_upload_overlay',
                    'target': list(overlay_target),
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'self_heal': heal_detail,
                }
                logger.info(
                    "iOS 照片上传覆盖层定位成功: overlay=%s target=%s detail=%s",
                    overlay, overlay_target, overlay_detail,
                )
                return overlay_target
            form_entry_target, form_entry_detail = self._form_select_entry_target(step, fresh_nodes)
            if not (
                isinstance(form_entry_target, (list, tuple))
                and len(form_entry_target) >= 2
                and all(isinstance(value, (int, float)) for value in form_entry_target[:2])
            ):
                form_entry_target = None
            attempts.append({
                'strategy': 'form_select_label_row',
                'matched': bool(form_entry_target),
                'elapsed': 0,
                'detail': form_entry_detail,
            })
            if form_entry_target:
                heal_detail = self._record_self_heal(step, 'form_select_label_row', attempts)
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': 'form_select_label_row',
                    'target': list(form_entry_target),
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'self_heal': heal_detail,
                }
                logger.info("表单选择字段行定位成功: target=%s detail=%s", form_entry_target, form_entry_detail)
                return form_entry_target
            picker_scroll_target, picker_scroll_detail = self._scroll_to_picker_entry_target(step)
            attempts.append({
                'strategy': 'scroll_to_picker_entry',
                'matched': bool(picker_scroll_target),
                'elapsed': 0,
                'detail': picker_scroll_detail,
            })
            if picker_scroll_target:
                heal_detail = self._record_self_heal(step, 'scroll_to_picker_entry', attempts)
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': 'scroll_to_picker_entry',
                    'target': list(picker_scroll_target),
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'self_heal': heal_detail,
                }
                logger.info(
                    "滚动后表单选择字段行定位成功: target=%s detail=%s",
                    picker_scroll_target,
                    picker_scroll_detail,
                )
                return picker_scroll_target
            if prefer_input_control:
                transition_target, transition_detail = (
                    self._recover_input_target_via_primary_transition(
                        step,
                        attempts,
                        fresh_nodes,
                    )
                )
                attempts.append({
                    'strategy': 'ios_primary_transition',
                    'matched': bool(transition_target),
                    'elapsed': 0,
                    'detail': transition_detail,
                })
                if transition_target:
                    heal_detail = self._record_self_heal(
                        step,
                        'ios_primary_transition',
                        attempts,
                    )
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'ios_primary_transition',
                        'target': list(transition_target),
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                        'webview': self._last_webview_diagnostics or None,
                        'self_heal': heal_detail,
                    }
                    logger.info(
                        "iOS 过渡主按钮后输入控件定位成功: target=%s detail=%s",
                        transition_target,
                        transition_detail,
                    )
                    return transition_target
            block_reason = self._coordinate_fallback_block_reason(step, attempts)
            if block_reason:
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': None,
                    'attempts': attempts,
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'blocked_reason': block_reason,
                }
                notice_hints = self._locator_failure_context_hints(step)
                hint_suffix = f"；{'；'.join(notice_hints)}" if notice_hints else ''
                raise ValueError(
                    f"步骤 '{step.get('name', step.get('type', 'unknown'))}' 定位失败，"
                    f"已阻止坐标兜底: {block_reason}，"
                    f"已尝试 {[item.get('strategy') for item in attempts]}"
                    f"{hint_suffix}"
                )
            candidate = static_candidates[0]
            target = candidate.get('target')
            attempts.append({'strategy': candidate.get('strategy'), 'matched': True, 'fallback': True})
            heal_detail = self._record_self_heal(step, candidate.get('strategy'), attempts)
            self._last_locator_diagnostics = {
                'step': step.get('name', step.get('type', 'unknown')),
                'selected_strategy': candidate.get('strategy'),
                'target': list(target) if isinstance(target, tuple) else target,
                'attempts': attempts,
                'current_resolution': self._get_current_resolution(),
                'webview': self._last_webview_diagnostics or None,
                'self_heal': heal_detail,
            }
            logger.warning(f"语义/图像定位未命中，使用坐标兜底: {target}")
            return target

        if static_candidates and chrome_toolbar_target:
            attempts.append({
                'strategy': static_candidates[0].get('strategy'),
                'matched': False,
                'skipped': True,
                'reason': 'chrome-toolbar-hidden-position-fallback-is-unsafe',
            })

        self._last_locator_diagnostics = {
            'step': step.get('name', step.get('type', 'unknown')),
            'selected_strategy': None,
            'attempts': attempts,
            'current_resolution': self._get_current_resolution(),
            'webview': self._last_webview_diagnostics or None,
        }
        notice_hints = self._locator_failure_context_hints(step)
        hint_suffix = f"；{'；'.join(notice_hints)}" if notice_hints else ''
        raise ValueError(
            f"步骤 '{step.get('name', step.get('type', 'unknown'))}' 定位失败，"
            f"已尝试 {[item.get('strategy') for item in attempts]}"
            f"{hint_suffix}"
        )
    
    def _resolve_selector(self, step: Dict[str, Any]) -> Any:
        """
        解析选择器
        
        支持的类型:
        - element_id: 从数据库加载元素
        - image: 图片元素
        - pos: 坐标点
        - region: 区域
        """
        # 优先使用 element_id
        element_id = step.get('element_id')
        if element_id:
            return self._resolve_element_by_id(element_id)
        
        selector_type = step.get('selector_type', 'image')
        selector = step.get('selector', '')
        
        if selector_type == 'image':
            # 图片选择器
            if not selector:
                logger.warning(f"图片选择器的 selector 为空，请检查步骤配置: {step.get('name', step.get('type', 'unknown'))}")
                return None
            
            image_scope = step.get('image_scope', 'common')
            image_path = os.path.join(self.image_base_dir, image_scope, selector)
            
            if not os.path.isfile(image_path):
                logger.warning(f"图片文件不存在: {image_path}")
                return None
            
            return self._create_template(image_path, step)
        
        elif selector_type == 'pos':
            # 坐标选择器
            if isinstance(selector, str):
                parts = [p.strip() for p in selector.split(',')]
                if len(parts) >= 2:
                    point_config = dict(step)
                    point_config.update({'x': parts[0], 'y': parts[1]})
                    return scale_point(point_config, self._get_current_resolution())
            elif isinstance(selector, (list, tuple)) and len(selector) >= 2:
                point_config = dict(step)
                point_config.update({'x': selector[0], 'y': selector[1]})
                return scale_point(point_config, self._get_current_resolution())
            
            logger.warning(f"无效的坐标格式: {selector}")
            return None
        
        elif selector_type == 'region':
            # 区域选择器（用于 exists 等）
            if isinstance(selector, str):
                parts = [p.strip() for p in selector.split(',')]
                if len(parts) >= 4:
                    region_config = dict(step)
                    region_config.update(dict(zip(('x1', 'y1', 'x2', 'y2'), parts[:4])))
                    return scale_region(region_config, self._get_current_resolution())
            elif isinstance(selector, (list, tuple)) and len(selector) >= 4:
                region_config = dict(step)
                region_config.update(dict(zip(('x1', 'y1', 'x2', 'y2'), selector[:4])))
                return scale_region(region_config, self._get_current_resolution())
            
            logger.warning(f"无效的区域格式: {selector}")
            return None
        
        logger.warning(f"未知的选择器类型: {selector_type}")
        return None
    
    def _resolve_element_by_id(self, element_id: int) -> Any:
        """从数据库加载元素"""
        try:
            element = self._load_element(element_id)
            if not element:
                logger.warning(f"未找到元素: element_id={element_id}")
                return None
            
            # 根据元素类型返回选择器
            if element.element_type == 'image':
                image_rel_path = element.config.get('image_path', '')
                if not image_rel_path:
                    logger.warning(f"元素 {element_id} 的 image_path 为空")
                    return None
                
                image_path = os.path.join(self.image_base_dir, image_rel_path)
                
                if not os.path.isfile(image_path):
                    logger.warning(f"图片文件不存在: {image_path}")
                    return None
                
                return self._create_template(image_path, element.config)
            
            elif element.element_type == 'pos':
                return scale_point(element.config, self._get_current_resolution())
            
            elif element.element_type == 'region':
                return scale_region(element.config, self._get_current_resolution())
            
        except Exception as e:
            logger.error(f"解析元素失败: element_id={element_id}, 错误: {e}", exc_info=True)
            return None
    
    def _action_touch(self, step: Dict[str, Any]):
        """点击动作"""
        restored_state_detail = self._clear_restored_form_state_if_needed(step)
        if self._is_picker_entry_step(step):
            nodes = self._dump_current_ui_nodes()
            keyboard_dismissal = None
            if self.platform == 'ios' and self._license_plate_keyboard_visible(nodes):
                keyboard_dismissal = self._close_license_plate_keyboard(step)
                self._sleep_interruptibly(step.get('keyboard_dismiss_wait_after', 0.4))
                nodes, keyboard_hidden = self._wait_for_ios_keyboard_hidden(
                    step.get('keyboard_dismiss_timeout', 1.5),
                    step.get('keyboard_dismiss_interval', 0.1),
                )
                if not keyboard_hidden and self._license_plate_keyboard_visible(nodes):
                    self._last_locator_diagnostics = {
                        'step': step.get('name', step.get('type', 'unknown')),
                        'selected_strategy': 'picker_entry_keyboard_blocked',
                        'keyboard_dismissal': keyboard_dismissal,
                        'current_resolution': self._get_current_resolution(),
                    }
                    raise RuntimeError('iOS 车牌键盘未收起，已阻止点击地区选择入口')
            if self._ios_picker_dialog_visible(nodes):
                self._last_locator_diagnostics = {
                    'step': step.get('name', step.get('type', 'unknown')),
                    'selected_strategy': 'picker_already_open',
                    'target': None,
                    'attempts': [{
                        'strategy': 'picker_already_open',
                        'matched': True,
                        'detail': {
                            'platform': self.platform,
                            'node_count': len(nodes),
                        },
                    }],
                    'current_resolution': self._get_current_resolution(),
                    'webview': self._last_webview_diagnostics or None,
                    'restored_state': restored_state_detail if restored_state_detail.get('matched') else None,
                    'keyboard_dismissal': keyboard_dismissal,
                }
                logger.info("Picker 弹框已打开，跳过入口点击: %s", step.get('name'))
                return
        try:
            target = self._resolve_action_target(step)
        except ValueError as exc:
            if not self._is_picker_entry_step(step):
                raise
            target, transition_detail = self._recover_picker_entry_via_primary_transition(
                step,
                exc,
            )
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'picker_entry_primary_transition': transition_detail,
            }
            if not target:
                raise
        logger.info(f"执行点击: {target}")
        upload_slot = self._ios_identity_photo_slot_from_step(step)
        if upload_slot:
            self._ios_pending_photo_upload_slot = upload_slot
            logger.info("记录 iOS 身份证上传位: %s", upload_slot)
        touch(target)
        photo_asset_followup = self._select_ios_photo_after_upload_menu_if_needed(step)
        if photo_asset_followup.get('matched'):
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'ios_photo_asset_followup': photo_asset_followup,
            }
        self._remember_ios_recorded_input_target(step, target)
        if restored_state_detail.get('matched'):
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'restored_state': restored_state_detail,
            }
        ios_retap = self._ios_primary_button_retap_target(step, target)
        if ios_retap:
            self._sleep_interruptibly(step.get('ios_primary_button_retap_delay', 0.35))
            logger.info("iOS 主按钮保险复点: %s", ios_retap)
            touch(ios_retap)
            self._sleep_interruptibly(step.get('ios_primary_button_wait_after', 1.5))
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'ios_primary_button_retap': {
                    'target': list(ios_retap),
                    'reason': 'bottom-primary-submit-button',
                },
            }
        wait_after = step.get('wait_after')
        if wait_after not in (None, ''):
            self._sleep_interruptibly(wait_after)
        if self._is_ios_photo_picker_done_step(step):
            upload_wait = self._wait_for_ios_photo_upload_complete(step)
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'ios_photo_upload_wait': upload_wait,
            }
            self._ios_pending_photo_upload_slot = ''
        self._verify_click_postcondition(step)

    def _ios_primary_button_retap_target(
        self,
        step: Dict[str, Any],
        target: Any,
    ) -> Optional[Tuple[int, int]]:
        if self.platform != 'ios':
            return None
        if not isinstance(target, (list, tuple)) or len(target) < 2:
            return None
        config = self._step_element_config(step)
        text_values = [
            step.get('name'),
            step.get('text'),
            step.get('ocr_text'),
            step.get('accessibility_id'),
            step.get('resource_id'),
        ]
        text_values.extend([
            config.get('text'),
            config.get('ocr_text'),
            config.get('accessibility_id'),
            config.get('resource_id'),
        ])
        label = ''.join(str(value or '') for value in text_values)
        if not any(keyword in label for keyword in ('同意并申请', '提交申请', '确认申请', '立即申请')):
            return None
        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            return None
        try:
            x = float(target[0])
            y = float(target[1])
        except (TypeError, ValueError):
            return None
        if y < height * 0.6:
            return None
        adjusted_y = max(1, int(round(y - height * 0.035)))
        return int(round(x)), adjusted_y
    
    def _action_double_click(self, step: Dict[str, Any]):
        """双击动作"""
        target = self._resolve_action_target(step)
        logger.info(f"执行双击: {target}")
        double_click(target)
    
    def _action_swipe(self, step: Dict[str, Any]):
        """滑动动作"""
        import json
        
        start = step.get('start')
        end = step.get('end')
        duration = step.get('duration', 0.5)
        
        # 处理 pos 定位方式的 selector
        if not start and not end:
            selector = step.get('selector')
            selector_type = step.get('selector_type')
            if selector and selector_type == 'pos':
                try:
                    if isinstance(selector, str):
                        locator = json.loads(selector)
                    else:
                        locator = selector
                    if isinstance(locator, list) and len(locator) >= 2:
                        start = tuple(locator[0])
                        end = tuple(locator[1])
                    else:
                        raise ValueError(f"Invalid locator for swipe: {locator}")
                except (json.JSONDecodeError, ValueError) as e:
                    raise ValueError(f"Failed to parse swipe locator: {selector}, error: {e}")
        
        def scale_swipe_point(value, normalized_key):
            if isinstance(value, str):
                parts = [part.strip() for part in value.split(',')]
            elif isinstance(value, (list, tuple)):
                parts = list(value)
            else:
                return None
            if len(parts) < 2:
                return None
            point_config = {
                'x': parts[0],
                'y': parts[1],
                'source_resolution': step.get('source_resolution'),
                'normalized_position': step.get(normalized_key),
            }
            return scale_point(point_config, self._get_current_resolution())

        start = scale_swipe_point(start, 'normalized_start')
        end = scale_swipe_point(end, 'normalized_end')
        
        if not start or not end:
            direction = str(step.get('direction') or '').strip().lower()
            if direction:
                width, height = parse_resolution(self._get_current_resolution())
                if width <= 0 or height <= 0:
                    raise RuntimeError('无法读取设备分辨率，不能执行方向滑动')
                try:
                    distance = max(0.1, min(0.9, float(step.get('distance', 0.55))))
                except (TypeError, ValueError):
                    distance = 0.55
                half_distance = distance / 2
                center_x = 0.5
                center_y = 0.5
                points = {
                    'up': (
                        (int(width * center_x), int(height * (center_y + half_distance))),
                        (int(width * center_x), int(height * (center_y - half_distance))),
                    ),
                    'down': (
                        (int(width * center_x), int(height * (center_y - half_distance))),
                        (int(width * center_x), int(height * (center_y + half_distance))),
                    ),
                    'left': (
                        (int(width * (center_x + half_distance)), int(height * center_y)),
                        (int(width * (center_x - half_distance)), int(height * center_y)),
                    ),
                    'right': (
                        (int(width * (center_x - half_distance)), int(height * center_y)),
                        (int(width * (center_x + half_distance)), int(height * center_y)),
                    ),
                }
                if direction not in points:
                    raise ValueError(f'不支持的滑动方向: {direction}')
                start, end = points[direction]

        if not start or not end:
            raise ValueError(f"Missing start or end coordinates for swipe: start={start}, end={end}")
        
        self._wait_for_page_stable(step)
        logger.info(f"执行滑动: {start} -> {end}")
        swipe(start, end, duration=duration)

    @staticmethod
    def _clamp_probability(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _resolve_video_region(region: Any, width: int, height: int) -> tuple:
        """把视频区域配置转换为当前屏幕上的像素区域。"""
        default_region = (0.05, 0.12, 0.95, 0.88)
        value = region
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = [item.strip() for item in value.split(',')]
        if isinstance(value, dict):
            value = [value.get(key) for key in ('x1', 'y1', 'x2', 'y2')]
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            value = default_region
        try:
            numbers = [float(item) for item in value[:4]]
        except (TypeError, ValueError):
            numbers = list(default_region)

        if max(abs(item) for item in numbers) <= 1.0:
            x1, y1, x2, y2 = (
                numbers[0] * width,
                numbers[1] * height,
                numbers[2] * width,
                numbers[3] * height,
            )
        else:
            x1, y1, x2, y2 = numbers
        x1 = max(0, min(width - 1, int(round(x1))))
        y1 = max(0, min(height - 1, int(round(y1))))
        x2 = max(x1 + 1, min(width, int(round(x2))))
        y2 = max(y1 + 1, min(height, int(round(y2))))
        return x1, y1, x2, y2

    def _take_video_frame(self, region: Any = None):
        """截取视频主体并降采样，用于播放和切换检测。"""
        try:
            import cv2

            screen = G.DEVICE.snapshot()
            if screen is None or not hasattr(screen, 'shape'):
                return None
            height, width = screen.shape[:2]
            x1, y1, x2, y2 = self._resolve_video_region(region, width, height)
            cropped = screen[y1:y2, x1:x2]
            if cropped.size == 0:
                return None
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY) if len(cropped.shape) == 3 else cropped
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            return cv2.resize(gray, (96, 96), interpolation=cv2.INTER_AREA)
        except Exception as exc:
            logger.debug(f"视频区域截图失败: {exc}")
            return None

    def _video_ui_texts(self) -> tuple:
        nodes = self._dump_current_ui_nodes()
        texts = set()
        for node in nodes:
            for key in ('text', 'content_desc'):
                value = re.sub(r'\s+', ' ', str(node.get(key) or '')).strip()
                if value:
                    texts.add(value)
        return texts, nodes

    def _wait_for_video_play(self, step: Dict[str, Any]) -> Dict[str, Any]:
        timeout = max(0.2, float(step.get('play_timeout', step.get('timeout', 8))))
        interval = max(0.1, float(step.get('sample_interval', 0.35)))
        threshold = max(0.001, float(step.get('motion_threshold', 0.012)))
        required_samples = max(1, int(step.get('required_motion_samples', 2)))
        video_region = step.get('video_region')
        previous = self._take_video_frame(video_region)
        if previous is None:
            raise RuntimeError("无法获取视频区域截图，不能检测视频播放状态")

        deadline = time.monotonic() + timeout
        motion_samples = 0
        differences = []
        while time.monotonic() < deadline:
            self.ensure_not_stopped()
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))
            current = self._take_video_frame(video_region)
            if current is None:
                continue
            difference = self._frame_difference(previous, current)
            differences.append(round(difference, 4))
            if difference >= threshold:
                motion_samples += 1
                if motion_samples >= required_samples:
                    diagnostics = {
                        'playing': True,
                        'motion_samples': motion_samples,
                        'threshold': threshold,
                        'differences': differences[-8:],
                    }
                    logger.info(f"视频播放检测通过: {diagnostics}")
                    return diagnostics
            previous = current

        raise AssertionError(
            f"等待视频播放超时: {timeout}s 内未检测到连续画面变化，"
            f"threshold={threshold}, differences={differences[-8:]}"
        )

    def _wait_for_video_play_with_recovery(self, step: Dict[str, Any]) -> tuple:
        """正常路径只用帧差；播放失败时再启用 OCR 识别并尝试恢复。"""
        try:
            return self._wait_for_video_play(step), None
        except AssertionError:
            exception = self._handle_video_exception(step, force_ocr=True)
            if not exception or not exception.get('dismissed'):
                raise
            return self._wait_for_video_play(step), exception

    def _watch_video_for_duration(self, step: Dict[str, Any], duration: float) -> Dict[str, Any]:
        duration = max(0.0, float(duration))
        verify_playing = bool(step.get('verify_playing', True))
        if duration <= 0:
            return {'duration': 0.0, 'playing': None, 'differences': []}
        if not verify_playing:
            self._sleep_interruptibly(duration)
            return {'duration': duration, 'playing': None, 'differences': []}

        interval = max(0.1, min(float(step.get('sample_interval', 0.5)), duration))
        threshold = max(0.001, float(step.get('motion_threshold', 0.012)))
        video_region = step.get('video_region')
        previous = self._take_video_frame(video_region)
        if previous is None:
            raise RuntimeError("无法获取视频区域截图，不能校验观看过程")

        deadline = time.monotonic() + duration
        motion_detected = False
        differences = []
        while time.monotonic() < deadline:
            self.ensure_not_stopped()
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))
            current = self._take_video_frame(video_region)
            if current is None:
                continue
            difference = self._frame_difference(previous, current)
            differences.append(round(difference, 4))
            motion_detected = motion_detected or difference >= threshold
            previous = current

        if not motion_detected:
            raise AssertionError(
                f"观看视频期间未检测到画面变化，视频可能暂停或卡住: "
                f"duration={duration}s, threshold={threshold}, differences={differences[-8:]}"
            )
        return {
            'duration': duration,
            'playing': True,
            'threshold': threshold,
            'differences': differences[-8:],
        }

    def _resolve_video_target(self, target: Any) -> Optional[tuple]:
        if not target:
            return None
        if isinstance(target, dict) and (
            target.get('element_id') or target.get('selector') or target.get('fingerprint')
        ):
            target_step = dict(target)
            target_step.setdefault('wait_for_stable', False)
            return self._resolve_action_target(target_step)

        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            return None
        value = target.get('normalized_position', target) if isinstance(target, dict) else target
        if isinstance(value, dict):
            x, y = value.get('x'), value.get('y')
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            x, y = value[0], value[1]
        else:
            return None
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            return None
        if abs(x) <= 1 and abs(y) <= 1:
            x, y = x * width, y * height
        return (
            max(0, min(width - 1, int(round(x)))),
            max(0, min(height - 1, int(round(y)))),
        )

    def _tap_video_target(self, action_name: str, target: Any) -> bool:
        point = self._resolve_video_target(target)
        if not point:
            logger.warning(f"随机行为 '{action_name}' 已命中概率，但未配置有效目标，跳过")
            return False
        logger.info(f"执行短视频随机行为: {action_name} -> {point}")
        touch(point)
        return True

    def _perform_comment_action(self, target: Any, comment_texts: List[str], interval: float) -> bool:
        if not isinstance(target, dict) or not any(
            key in target for key in ('open', 'input', 'submit', 'close')
        ):
            return self._tap_video_target('comment', target)

        if not self._tap_video_target('comment.open', target.get('open')):
            return False
        self._sleep_interruptibly(interval)
        submitted = False
        if comment_texts and target.get('input') and target.get('submit'):
            if self._tap_video_target('comment.input', target.get('input')):
                airtest_text(str(random.choice(comment_texts)))
                self._sleep_interruptibly(interval)
                submitted = self._tap_video_target('comment.submit', target.get('submit'))
                self._sleep_interruptibly(interval)
        if target.get('close'):
            self._tap_video_target('comment.close', target.get('close'))
        return submitted or not comment_texts

    def _perform_random_video_actions(self, step: Dict[str, Any]) -> List[Dict[str, Any]]:
        targets = step.get('action_targets') or {}
        if not isinstance(targets, dict):
            targets = {}
        comment_texts = step.get('comment_texts') or []
        if not isinstance(comment_texts, list):
            comment_texts = [str(comment_texts)] if comment_texts else []
        interval = max(0.0, float(step.get('action_interval', 0.5)))
        probabilities = {
            'like': self._clamp_probability(step.get('like_probability', 0)),
            'favorite': self._clamp_probability(step.get('favorite_probability', 0)),
            'comment': self._clamp_probability(step.get('comment_probability', 0)),
            'follow': self._clamp_probability(step.get('follow_probability', 0)),
        }
        results = []
        for action_name, probability in probabilities.items():
            roll = random.random()
            if roll >= probability:
                continue
            if action_name == 'comment':
                executed = self._perform_comment_action(
                    targets.get(action_name), comment_texts, interval
                )
            else:
                executed = self._tap_video_target(action_name, targets.get(action_name))
            results.append({
                'action': action_name,
                'probability': probability,
                'roll': round(roll, 6),
                'executed': executed,
            })
            if executed and interval > 0:
                self._sleep_interruptibly(interval)
        return results

    def _handle_video_exception(
        self,
        step: Dict[str, Any],
        force_ocr: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not bool(step.get('exception_detection', False)):
            return None
        keywords = step.get('exception_keywords') or [
            '请先登录', '登录后继续', '网络异常', '加载失败', '青少年模式', '跳过广告'
        ]
        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        texts, nodes = self._video_ui_texts()
        combined_text = ' '.join(sorted(texts))
        detected = next(
            (keyword for keyword in keywords if str(keyword).casefold() in combined_text.casefold()),
            None,
        )

        use_ocr = force_ocr and bool(step.get('ocr_exception_detection', True))
        ocr_text = ''
        if not detected and use_ocr and OCR_AVAILABLE:
            try:
                helper = get_ocr_helper(languages=['ch_sim', 'en'], use_gpu=False)
                screen = G.DEVICE.snapshot()
                if screen is not None:
                    ocr_text = helper.recognize_text(screen, min_confidence=0.35, use_cache=False)
                    detected = next(
                        (keyword for keyword in keywords if str(keyword).casefold() in ocr_text.casefold()),
                        None,
                    )
            except Exception as exc:
                logger.warning(f"短视频异常 OCR 检测失败，继续使用页面树结果: {exc}")
        if not detected:
            return None

        close_targets = step.get('exception_close_targets') or {}
        if not isinstance(close_targets, dict):
            close_targets = {}
        configured_target = close_targets.get(str(detected), close_targets.get('default'))
        dismissed = self._tap_video_target(f'exception.{detected}', configured_target) if configured_target else False

        close_texts = step.get('exception_close_texts') or [
            '重试', '取消', '以后再说', '暂不登录', '我知道了', '关闭', '跳过'
        ]
        if not isinstance(close_texts, list):
            close_texts = [str(close_texts)]
        if not dismissed:
            for close_text in close_texts:
                matched_node = next((node for node in nodes if str(close_text).casefold() in (
                    f"{node.get('text', '')} {node.get('content_desc', '')}"
                ).casefold()), None)
                if matched_node and matched_node.get('bounds'):
                    touch(region_center(matched_node.get('bounds')))
                    dismissed = True
                    break
        if not dismissed and use_ocr and OCR_AVAILABLE:
            try:
                helper = get_ocr_helper(languages=['ch_sim', 'en'], use_gpu=False)
                for close_text in close_texts:
                    point, _ = helper.find_text(
                        str(close_text), match_mode='contains', min_confidence=0.4
                    )
                    if point:
                        touch(point)
                        dismissed = True
                        break
            except Exception as exc:
                logger.warning(f"短视频异常关闭按钮 OCR 定位失败: {exc}")
        if not dismissed:
            try:
                from airtest.core.api import keyevent
                keyevent('BACK')
                dismissed = True
            except Exception as exc:
                logger.warning(f"短视频异常页面无法自动关闭: {detected}, error={exc}")
        if dismissed:
            self._sleep_interruptibly(0.5)

        diagnostics = {
            'keyword': detected,
            'dismissed': dismissed,
            'ui_text': combined_text[:500],
            'ocr_text': ocr_text[:500],
        }
        logger.warning(f"检测到短视频流异常: {diagnostics}")
        return diagnostics

    def _video_swipe_points(self, step: Dict[str, Any]) -> tuple:
        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            raise RuntimeError("无法读取设备分辨率，不能生成短视频滑动轨迹")
        distance = max(0.2, min(0.9, float(step.get('swipe_distance', 0.8))))
        random_swipe = bool(step.get('random_swipe', True))
        if random_swipe:
            distance = max(0.2, min(0.9, distance * random.uniform(0.92, 1.06)))
            x_ratio = random.uniform(0.44, 0.56)
            center_ratio = random.uniform(0.49, 0.53)
            duration = random.uniform(0.28, 0.46)
        else:
            x_ratio = 0.5
            center_ratio = 0.51
            duration = 0.36
        delta = height * distance
        start_y = center_ratio * height + delta / 2
        end_y = center_ratio * height - delta / 2
        x = int(round(width * x_ratio))
        start_y = int(round(max(height * 0.08, min(height * 0.92, start_y))))
        end_y = int(round(max(height * 0.08, min(height * 0.92, end_y))))
        direction = str(step.get('direction', 'up')).lower()
        if direction == 'down':
            start_y, end_y = end_y, start_y
        elif direction != 'up':
            raise ValueError("短视频刷视频仅支持 up 或 down 方向")
        return (x, start_y), (x, end_y), duration

    def _swipe_to_next_video(self, step: Dict[str, Any]) -> Dict[str, Any]:
        video_region = step.get('video_region')
        threshold = max(0.001, float(step.get('transition_threshold', 0.045)))
        timeout = max(0.2, float(step.get('transition_timeout', 2.5)))
        retries = max(0, int(step.get('max_swipe_retries', 2)))
        previous = self._take_video_frame(video_region)
        before_texts, _ = self._video_ui_texts()
        attempts = []

        for attempt in range(retries + 1):
            self.ensure_not_stopped()
            start, end, duration = self._video_swipe_points(step)
            logger.info(f"短视频智能滑动 {attempt + 1}/{retries + 1}: {start} -> {end}, {duration:.2f}s")
            swipe(start, end, duration=duration)
            self._sleep_interruptibly(min(0.45, timeout))

            if previous is None:
                return {
                    'verified': False,
                    'verification': 'video-play-fallback',
                    'start': list(start),
                    'end': list(end),
                    'duration': round(duration, 3),
                }

            deadline = time.monotonic() + timeout
            best_difference = 0.0
            text_changed = False
            while time.monotonic() < deadline:
                current = self._take_video_frame(video_region)
                if current is not None:
                    best_difference = max(best_difference, self._frame_difference(previous, current))
                after_texts, _ = self._video_ui_texts()
                text_changed = bool(before_texts and after_texts and before_texts != after_texts)
                if best_difference >= threshold or text_changed:
                    diagnostics = {
                        'verified': True,
                        'attempt': attempt + 1,
                        'start': list(start),
                        'end': list(end),
                        'duration': round(duration, 3),
                        'difference': round(best_difference, 4),
                        'threshold': threshold,
                        'ui_text_changed': text_changed,
                    }
                    logger.info(f"下一视频切换验证通过: {diagnostics}")
                    return diagnostics
                self._sleep_interruptibly(min(0.2, max(0.0, deadline - time.monotonic())))
            attempts.append({
                'attempt': attempt + 1,
                'difference': round(best_difference, 4),
                'ui_text_changed': text_changed,
            })
            retry_frame = self._take_video_frame(video_region)
            if retry_frame is not None:
                previous = retry_frame
            before_texts, _ = self._video_ui_texts()

        raise AssertionError(
            f"滑动后未检测到下一视频，已重试 {retries} 次: "
            f"threshold={threshold}, attempts={attempts}"
        )

    def _action_wait_video_play(self, step: Dict[str, Any]):
        self._handle_video_exception(step)
        result, recovery_exception = self._wait_for_video_play_with_recovery(step)
        if recovery_exception:
            result['recovery_exception'] = recovery_exception
        self.context['outputs']['last'] = result

    def _action_watch_video(self, step: Dict[str, Any]):
        duration = float(step.get('duration', 10))
        result = self._watch_video_for_duration(step, duration)
        self.context['outputs']['last'] = result

    def _action_random_action(self, step: Dict[str, Any]):
        actions = self._perform_random_video_actions(step)
        result = {'actions': actions, 'executed_count': sum(1 for item in actions if item['executed'])}
        self.context['outputs']['last'] = result
        save_as = str(step.get('save_as') or '').strip()
        if save_as:
            self._set_variable(save_as, result, 'local')

    def _action_short_video_feed(self, step: Dict[str, Any]):
        count = max(1, int(step.get('count', 1)))
        watch_time = step.get('watch_time') or {}
        if not isinstance(watch_time, dict):
            watch_time = {}
        watch_min = max(0.0, float(watch_time.get('min', step.get('watch_time_min', 5))))
        watch_max = max(watch_min, float(watch_time.get('max', step.get('watch_time_max', watch_min))))
        random_stay = bool(step.get('random_stay', True))
        capture_each_video = bool(step.get('capture_each_video', True))
        result = {
            'requested_count': count,
            'watched_count': 0,
            'swipe_count': 0,
            'watch_durations': [],
            'actions': [],
            'exceptions': [],
            'screenshots': [],
        }

        if step.get('element_id') or step.get('selector'):
            entry_step = dict(step)
            entry_step['wait_for_stable'] = True
            logger.info("短视频刷视频：进入视频流")
            self._action_touch(entry_step)
            self._sleep_interruptibly(0.5)

        exception = self._handle_video_exception(step)
        if exception:
            result['exceptions'].append(exception)
        _, recovery_exception = self._wait_for_video_play_with_recovery(step)
        if recovery_exception:
            result['exceptions'].append(recovery_exception)

        for index in range(count):
            self.ensure_not_stopped(force=True)
            exception = self._handle_video_exception(step)
            if exception:
                result['exceptions'].append(exception)

            duration = random.uniform(watch_min, watch_max) if random_stay else watch_min
            duration = round(duration, 3)
            logger.info(f"短视频 {index + 1}/{count}：观看 {duration:.3f}s")
            self._watch_video_for_duration(step, duration)
            result['watch_durations'].append(duration)
            result['watched_count'] += 1

            actions = self._perform_random_video_actions(step)
            if actions:
                result['actions'].append({'video_index': index + 1, 'items': actions})

            if capture_each_video:
                screenshot = self._capture_screenshot(f"short_video_{index + 1}_verified")
                if screenshot:
                    result['screenshots'].append(screenshot)

            if index >= count - 1:
                continue

            self._swipe_to_next_video(step)
            result['swipe_count'] += 1
            exception = self._handle_video_exception(step)
            if exception:
                result['exceptions'].append(exception)
            _, recovery_exception = self._wait_for_video_play_with_recovery(step)
            if recovery_exception:
                result['exceptions'].append(recovery_exception)

        self.context['outputs']['last'] = result
        save_as = str(step.get('save_as') or 'short_video_feed_result').strip()
        if save_as:
            self._set_variable(save_as, result, 'local')
        logger.info(
            f"短视频刷视频完成: watched={result['watched_count']}, "
            f"swipes={result['swipe_count']}, exceptions={len(result['exceptions'])}"
        )

    def _action_wait(self, step: Dict[str, Any]):
        """等待：有 selector 时等待元素出现，没有时纯等待 timeout 秒"""
        has_selector = bool(step.get('element_id') or step.get('selector'))
        timeout = step.get('timeout', step.get('duration', 3))
        
        if has_selector:
            target = self._resolve_action_target({**step, 'timeout': timeout})
            logger.info(f"等待元素出现成功: {target}")
        else:
            logger.info(f"等待 {timeout} 秒")
            self._sleep_interruptibly(timeout)
    
    def _action_snapshot(self, step: Dict[str, Any]):
        """截图"""
        name = step.get('name', f'snapshot_{int(time.time())}')
        filename = f"{name}.png"
        
        filepath = os.path.join(self.screenshots_dir, filename)
        
        logger.info(f"截图保存: {filepath}")
        snapshot(filename=filepath)
    
    def _action_text(self, step: Dict[str, Any]):
        """输入文本"""
        text_value = step.get('text', '')
        logger.info(f"输入文本: {text_value}")
        airtest_text(text_value)

    def _target_package(self, step: Dict[str, Any]) -> str:
        package_name = str(step.get('package_name') or self.package_name or '').strip()
        if not package_name:
            raise ValueError('未配置应用包名，请在组件或执行设备中指定 package_name')
        return package_name

    def _action_launch_app(self, step: Dict[str, Any]):
        """启动应用。"""
        from airtest.core.api import start_app

        package_name = self._target_package(step)
        activity = str(step.get('activity') or '').strip()
        logger.info('启动应用: package=%s, activity=%s', package_name, activity or 'default')
        if activity:
            start_app(package_name, activity=activity)
        else:
            start_app(package_name)
        self._sleep_interruptibly(step.get('wait_after', 1))

    def _action_close_app(self, step: Dict[str, Any]):
        """关闭应用。"""
        from airtest.core.api import stop_app

        package_name = self._target_package(step)
        logger.info('关闭应用: %s', package_name)
        stop_app(package_name)

    def _action_restart_app(self, step: Dict[str, Any]):
        """关闭后重新启动应用。"""
        self._action_close_app(step)
        self._sleep_interruptibly(step.get('restart_interval', 0.8))
        self._action_launch_app(step)

    def _action_clear_data(self, step: Dict[str, Any]):
        """清理应用数据。"""
        from airtest.core.api import clear_app

        package_name = self._target_package(step)
        logger.info('清理应用数据: %s', package_name)
        clear_app(package_name)

    def _action_back(self, step: Dict[str, Any]):
        if self.platform == 'ios':
            width, height = parse_resolution(self._get_current_resolution())
            if width <= 0 or height <= 0:
                raise RuntimeError('无法读取设备分辨率，不能执行 iOS 返回手势')
            start = (max(2, int(width * 0.02)), int(height * 0.5))
            end = (int(width * 0.78), int(height * 0.5))
            duration = float(step.get('duration', 0.45))
            swipe(start, end, duration=duration)
            self.context['outputs']['last'] = {
                'pressed': 'BACK',
                'method': 'ios_edge_swipe',
                'start': list(start),
                'end': list(end),
            }
            self._sleep_interruptibly(step.get('wait_after', 0.3))
            return

        from airtest.core.api import keyevent

        keyevent('BACK')
        self._sleep_interruptibly(step.get('wait_after', 0.3))

    def _action_home(self, step: Dict[str, Any]):
        from airtest.core.api import keyevent

        keyevent('HOME')
        self._sleep_interruptibly(step.get('wait_after', 0.3))

    def _action_press_enter(self, step: Dict[str, Any]):
        """发送系统回车键。"""
        method = 'airtest_keyevent'
        if self.platform == 'ios':
            airtest_text('', enter=True)
            method = 'ios_text_enter'
        else:
            from airtest.core.api import keyevent

            keyevent('KEYCODE_ENTER')

        wait_after = step.get('wait_after', 0.3)
        self._sleep_interruptibly(wait_after)
        self._store_component_result(step, {
            'pressed': 'ENTER',
            'method': method,
            'wait_after': wait_after,
        })

    def _current_page_state(self) -> Dict[str, Any]:
        state = self._detect_page_context()
        package = ''
        activity = ''
        try:
            top_activity = G.DEVICE.get_top_activity() if G.DEVICE else None
            if top_activity:
                package = top_activity[0] or ''
                activity = top_activity[1] or ''
        except Exception as exc:
            logger.debug('读取当前页面 Activity 失败: %s', exc)
        state.update({'package': package, 'activity': activity})
        return state

    def _page_state_matches(self, state: Dict[str, Any], step: Dict[str, Any]) -> bool:
        expected_type = str(step.get('page_type') or 'any').strip().lower()
        expected_package = str(step.get('expected_package') or step.get('package_name') or '').strip()
        expected_activity = str(step.get('expected_activity') or step.get('expected') or '').strip()
        expected_page = str(step.get('expected_page') or step.get('page_name') or '').strip().lower()
        match_mode = str(step.get('match_mode') or 'contains').strip().lower()

        if expected_type not in ('', 'any') and state.get('type') != expected_type:
            return False
        if expected_package and not self._match_text(str(state.get('package') or ''), expected_package, match_mode):
            return False
        if expected_activity and not self._match_text(str(state.get('activity') or ''), expected_activity, match_mode):
            return False
        if expected_page:
            if not state.get('page_name'):
                state.update(self._detect_semantic_page(step, state))
            actual_page = ' '.join(str(value or '') for value in (
                state.get('page_name'), state.get('page_label')
            )).strip()
            if not self._match_text(actual_page, expected_page, match_mode):
                return False
        return True

    def _wait_for_page(self, step: Dict[str, Any]) -> Dict[str, Any]:
        timeout = max(0.0, float(step.get('timeout', 5)))
        interval = max(0.05, float(step.get('retry_interval', 0.5)))
        deadline = time.monotonic() + timeout
        last_state: Dict[str, Any] = {}
        while True:
            self.ensure_not_stopped()
            last_state = self._current_page_state()
            if self._page_state_matches(last_state, step):
                return last_state
            if time.monotonic() >= deadline:
                break
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))
        raise AssertionError(f'等待页面超时，最后页面状态: {last_state}')

    def _action_wait_page(self, step: Dict[str, Any]):
        result = self._wait_for_page(step)
        self.context['outputs']['last'] = result

    def _action_wait_element(self, step: Dict[str, Any]):
        self._action_element_probe(step)
    
    def _action_set_variable(self, step: Dict[str, Any]):
        """设置变量"""
        name = step.get('name')
        value = step.get('value')
        scope = step.get('scope', 'local')
        
        if name:
            self._set_variable(name, value, scope)
            logger.info(f"设置变量: {scope}.{name} = {value}")
    
    def _action_assert(self, step: Dict[str, Any]):
        """
        断言入口，根据 assert_type 分发到具体实现。
        支持 timeout 参数：断言失败后在 timeout 秒内持续重试（适配页面加载延迟）。
        
        支持的 assert_type:
        - text:   OCR 识别文本，支持 exact/contains/regex 匹配
        - number: OCR 识别数字（自动去除逗号等格式符号），精确匹配
        - regex:  OCR 识别文本，用正则表达式匹配（text + match_mode=regex 的快捷方式）
        - range:  OCR 识别数字，判断是否在 [min, max] 范围内
        - exists: 判断图片元素是否存在于屏幕上
        - image:  在屏幕上查找期望图片是否存在（图片对比断言）
        """
        assert_type = step.get('assert_type', 'text')
        timeout = float(step.get('timeout', 0))
        retry_interval = float(step.get('retry_interval', 1))
        
        assert_map = {
            'text': self._assert_text,
            'number': self._assert_number,
            'regex': self._assert_regex,
            'range': self._assert_range,
            'exists': self._assert_exists,
            'image': self._assert_image,
        }
        
        handler = assert_map.get(assert_type)
        if not handler:
            raise ValueError(f"未知的断言类型: {assert_type}，支持: {', '.join(assert_map.keys())}")
        
        # 无超时：直接执行一次
        if timeout <= 0:
            handler(step)
            return
        
        # 有超时：在 timeout 秒内持续重试
        deadline = time.time() + timeout
        last_error = None
        attempt = 0
        while True:
            self.ensure_not_stopped()
            attempt += 1
            try:
                handler(step)
                if attempt > 1:
                    logger.info(f"断言在第 {attempt} 次尝试后通过")
                return  # 断言通过
            except (AssertionError, Exception) as e:
                last_error = e
                if time.time() >= deadline:
                    break
                remaining = deadline - time.time()
                wait_time = min(retry_interval, remaining)
                if wait_time > 0:
                    logger.debug(
                        f"断言未通过 (第 {attempt} 次)，{wait_time:.1f}s 后重试: {e}"
                    )
                    self._sleep_interruptibly(wait_time)
        
        raise last_error

    def _action_assert_exists(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized.setdefault('assert_type', 'exists')
        normalized.setdefault('expected_exists', True)
        self._action_assert(normalized)

    def _action_assert_text(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized.setdefault('assert_type', 'text')
        self._action_assert(normalized)

    def _action_assert_image(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized.setdefault('assert_type', 'image')
        self._action_assert(normalized)

    def _action_assert_page(self, step: Dict[str, Any]):
        result = self._current_page_state()
        if not self._page_state_matches(result, step):
            raise AssertionError(f'页面断言失败，当前页面状态: {result}')
        self.context['outputs']['last'] = result

    def _action_assert_activity(self, step: Dict[str, Any]):
        """断言当前前台 Activity，用于校验页面跳转结果。"""
        expected_package = step.get('expected_package', '')
        expected_activity = step.get('expected_activity') or step.get('expected', '')
        match_mode = step.get('match_mode', 'contains')
        timeout = float(step.get('timeout', 5))
        retry_interval = float(step.get('retry_interval', 0.5))

        if not expected_package and not expected_activity:
            raise ValueError("assert_activity 需要 expected_package 或 expected_activity")
        if not G.DEVICE:
            raise RuntimeError("设备未连接，无法断言 Activity")

        deadline = time.time() + timeout
        last_actual = ''

        while True:
            self.ensure_not_stopped()
            package = ''
            activity = ''
            top_activity = G.DEVICE.get_top_activity()
            if top_activity:
                package = top_activity[0] or ''
                activity = top_activity[1] or ''
            last_actual = f"{package}/{activity}" if package or activity else ''

            package_passed = self._match_text(package, expected_package, match_mode) if expected_package else True
            activity_passed = self._match_text(activity, expected_activity, match_mode) if expected_activity else True

            if package_passed and activity_passed:
                logger.info(f"Activity 断言成功: {last_actual}")
                return

            if time.time() >= deadline:
                break
            self._sleep_interruptibly(min(retry_interval, max(deadline - time.time(), 0)))

        raise AssertionError(
            f"Activity 断言失败: 期望 package='{expected_package}', "
            f"activity='{expected_activity}' ({match_mode}), 实际 '{last_actual}'"
        )

    @staticmethod
    def _match_text(actual: str, expected: str, match_mode: str) -> bool:
        if match_mode == 'exact':
            return actual == expected
        if match_mode == 'regex':
            return re.search(expected, actual) is not None
        return expected in actual
    
    # ---------- 断言内部实现 ----------
    
    def _parse_ocr_region(self, step: Dict[str, Any]) -> tuple:
        """
        从步骤配置中解析 OCR 区域坐标 (x1, y1, x2, y2)。
        同时支持 selector 和 ocr_selector 字段名。
        """
        element_id = step.get('ocr_element_id') or step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if not element or element.element_type != 'region':
                raise ValueError(f"OCR 元素必须是区域类型: element_id={element_id}")
            region = scale_region(element.config or {}, self._get_current_resolution())
            if not region:
                raise ValueError(f"OCR 区域元素配置无效: element_id={element_id}")
            return region

        selector = step.get('ocr_selector') or step.get('selector')
        selector_type = step.get('ocr_selector_type') or step.get('selector_type', 'region')
        
        if not selector:
            raise ValueError("断言需要 selector 或 ocr_selector 参数来指定 OCR 区域")
        
        if selector_type != 'region':
            raise ValueError(f"OCR 断言仅支持 selector_type=region，当前: {selector_type}")
        
        if isinstance(selector, str):
            parts = [p.strip() for p in selector.split(',')]
            if len(parts) != 4:
                raise ValueError(f"region 格式错误，需要 4 个值 (x1,y1,x2,y2): {selector}")
            region_config = dict(step)
            region_config.update(dict(zip(('x1', 'y1', 'x2', 'y2'), parts)))
            return scale_region(region_config, self._get_current_resolution())
        elif isinstance(selector, (list, tuple)) and len(selector) >= 4:
            region_config = dict(step)
            region_config.update(dict(zip(('x1', 'y1', 'x2', 'y2'), selector[:4])))
            return scale_region(region_config, self._get_current_resolution())
        else:
            raise ValueError(f"无法解析 region: {selector}")
    
    def _ocr_recognize_text(self, region: tuple) -> str:
        """OCR 识别指定区域的文本"""
        ocr = self._get_ocr_helper()
        return ocr.recognize_region_text(region)
    
    def _ocr_recognize_number(self, region: tuple) -> int:
        """OCR 识别指定区域的数字（自动去除逗号等格式符号）"""
        ocr = self._get_ocr_helper()
        return ocr.recognize_region_number(region)
    
    def _assert_text(self, step: Dict[str, Any]):
        """文本断言：OCR 识别文本，支持 exact/contains/regex 匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("文本断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        expected = step.get('expected', '')
        match_mode = step.get('match_mode', 'contains')
        
        actual_text = self._ocr_recognize_text(region)
        
        if match_mode == 'exact':
            passed = actual_text == expected
        elif match_mode == 'contains':
            passed = expected in actual_text
        elif match_mode == 'regex':
            passed = re.search(expected, actual_text) is not None
        else:
            raise ValueError(f"不支持的 match_mode: {match_mode}")
        
        if not passed:
            raise AssertionError(f"文本断言失败: 期望 '{expected}' ({match_mode}), 实际 '{actual_text}'")
        
        logger.info(f"文本断言成功: '{expected}' ({match_mode}) 匹配 '{actual_text}'")
    
    def _assert_number(self, step: Dict[str, Any]):
        """数值断言：OCR 识别数字（去逗号），与期望值精确匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("数值断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        expected_raw = step.get('expected', '0')
        
        # 期望值也做去逗号处理，兼容用户填 "3,000,000" 或 "3000000"
        try:
            expected_num = int(str(expected_raw).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            raise ValueError(f"number 断言的期望值无法转为数字: {expected_raw}")
        
        actual_num = self._ocr_recognize_number(region)
        
        if actual_num != expected_num:
            raise AssertionError(f"数值断言失败: 期望 {expected_num}, 实际 {actual_num}")
        
        logger.info(f"数值断言成功: 期望 {expected_num}, 实际 {actual_num}")
    
    def _assert_regex(self, step: Dict[str, Any]):
        """正则断言：OCR 识别文本，用正则表达式匹配"""
        if not OCR_AVAILABLE:
            raise RuntimeError("正则断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        pattern = step.get('expected', '')
        
        if not pattern:
            raise ValueError("regex 断言需要在 expected 字段填写正则表达式")
        
        actual_text = self._ocr_recognize_text(region)
        
        match = re.search(pattern, actual_text)
        if not match:
            raise AssertionError(f"正则断言失败: 模式 '{pattern}' 未匹配到文本 '{actual_text}'")
        
        logger.info(f"正则断言成功: 模式 '{pattern}' 匹配到 '{match.group()}' (全文: '{actual_text}')")
    
    def _assert_range(self, step: Dict[str, Any]):
        """范围断言：OCR 识别数字，判断是否在 [min, max] 范围内"""
        if not OCR_AVAILABLE:
            raise RuntimeError("范围断言需要 OCR 支持，请安装 easyocr")
        
        region = self._parse_ocr_region(step)
        
        min_val = step.get('min')
        max_val = step.get('max')
        
        if min_val is None and max_val is None:
            raise ValueError("range 断言需要至少设置 min 或 max 之一")
        
        # 转换为数值
        try:
            min_num = int(str(min_val).replace(',', '').replace(' ', '')) if min_val is not None else None
        except (ValueError, TypeError):
            raise ValueError(f"range 断言的 min 值无法转为数字: {min_val}")
        try:
            max_num = int(str(max_val).replace(',', '').replace(' ', '')) if max_val is not None else None
        except (ValueError, TypeError):
            raise ValueError(f"range 断言的 max 值无法转为数字: {max_val}")
        
        actual_num = self._ocr_recognize_number(region)
        
        if min_num is not None and actual_num < min_num:
            raise AssertionError(f"范围断言失败: 实际值 {actual_num} 小于最小值 {min_num}")
        if max_num is not None and actual_num > max_num:
            raise AssertionError(f"范围断言失败: 实际值 {actual_num} 大于最大值 {max_num}")
        
        range_desc = f"[{min_num if min_num is not None else '-∞'}, {max_num if max_num is not None else '+∞'}]"
        logger.info(f"范围断言成功: 实际值 {actual_num} 在范围 {range_desc} 内")
    
    def _assert_exists(self, step: Dict[str, Any]):
        """存在性断言：支持语义、OCR、图片和坐标定位链。"""
        expected_exists = step.get('expected_exists', True)

        try:
            self._resolve_action_target(step)
            result = True
        except (ValueError, AssertionError):
            result = False
        
        if expected_exists and not result:
            raise AssertionError(f"期望元素存在，但实际不存在")
        elif not expected_exists and result:
            raise AssertionError(f"期望元素不存在，但实际存在")
        
        logger.info(f"存在性断言成功: 期望存在={expected_exists}, 实际存在={result}")
    
    def _assert_image(self, step: Dict[str, Any]):
        """图片断言：在屏幕上查找期望图片是否存在"""
        expected_element_id = step.get('expected_element_id')
        if expected_element_id:
            element = self._load_element(expected_element_id)
            if not element or element.element_type != 'image':
                raise ValueError(f"图片断言元素无效: element_id={expected_element_id}")
            image_rel_path = (element.config or {}).get('image_path', '')
            image_path = os.path.join(self.image_base_dir, image_rel_path)
            if not image_rel_path or not os.path.isfile(image_path):
                raise ValueError(f"期望图片文件不存在: {image_path}")
            target = self._create_template(image_path, element.config or {})
            result = exists(target)
            if result is None:
                raise AssertionError(f"图片断言失败: 未找到元素 '{element.name}'")
            logger.info(f"图片断言成功: 找到元素 '{element.name}', 位置: {result}")
            return

        expected_image = step.get('expected', '')
        image_scope = step.get('expected_image_scope') or step.get('image_scope', 'common')
        threshold = step.get('image_threshold', 0.85)
        
        if not expected_image:
            raise ValueError("image 断言需要在 expected 字段填写图片文件名")
        
        image_path = os.path.join(self.image_base_dir, image_scope, expected_image)
        
        if not os.path.isfile(image_path):
            raise ValueError(f"期望图片文件不存在: {image_path}")
        
        target = self._create_template(image_path, {**step, 'image_threshold': threshold})
        result = exists(target)
        
        if result is None:
            raise AssertionError(f"图片断言失败: 未在屏幕上找到图片 '{expected_image}' (阈值: {threshold})")
        
        logger.info(f"图片断言成功: 在屏幕上找到图片 '{expected_image}', 位置: {result}")
    
    # ============ 新增动作方法 ============
    
    def _action_click(self, step: Dict[str, Any]):
        """点击动作（重命名自 _action_touch）"""
        if self.platform == 'ios' and self._action_recorded_license_plate_key(step):
            return
        if self.platform == 'ios' and self._skip_duplicate_license_plate_province(step):
            return
        if self.platform == 'ios' and self._is_license_plate_trigger(step):
            open_result = self._open_license_plate_keyboard(step)
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'license_plate_keyboard': open_result,
            }
            return
        webview_result = self._try_webview_action(step, 'click')
        if webview_result:
            self._last_locator_diagnostics = {
                'step': step.get('name', step.get('type', '点击')),
                'selected_strategy': 'webview_dom',
                'webview': webview_result,
            }
            return
        self._action_touch(step)

    def _action_smart_click(self, step: Dict[str, Any]):
        """按 WebView DOM → 语义 → OCR → 图片 → 坐标的顺序点击。"""
        if self._smart_click_targets_checkbox(step):
            normalized = dict(step)
            # “智能点击”表达的是让目标控件生效，而不是无条件翻转状态。
            # 默认固定为 checked，确保 Agent 重试或重复执行时具备幂等性；
            # 只有显式的 checkbox_toggle 步骤才能使用 toggle。
            normalized.setdefault('desired_state', 'checked')
            logger.info('智能点击识别为复选框/开关，改用幂等勾选')
            self._action_checkbox_toggle(normalized)
            return
        self._action_click(step)

    def _smart_click_targets_checkbox(self, step: Dict[str, Any]) -> bool:
        """识别智能元素是否为 checkbox/switch，避免点击复合标签中心。"""
        config: Dict[str, Any] = {}
        element_id = step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if element:
                config.update(element.config or {})
        config.update({
            key: value for key, value in step.items()
            if key in {'class_name', 'traits', 'checked', 'selected'}
            and value is not None
        })
        fingerprint = config.get('fingerprint') or {}
        node = {
            'class_name': config.get('class_name') or fingerprint.get('class_name'),
            'traits': config.get('traits') or fingerprint.get('traits'),
            'checked': config.get('checked', fingerprint.get('checked')),
            'selected': config.get('selected', fingerprint.get('selected')),
        }
        return self._is_checkbox_node(node)

    @staticmethod
    def _normalize_checkbox_desired_state(value: Any) -> str:
        if isinstance(value, bool):
            return 'checked' if value else 'unchecked'
        normalized = str(value or 'checked').strip().lower()
        aliases = {
            'check': 'checked', 'select': 'checked', 'selected': 'checked',
            'true': 'checked', '1': 'checked', 'on': 'checked', '勾选': 'checked',
            'uncheck': 'unchecked', 'clear': 'unchecked', 'false': 'unchecked',
            '0': 'unchecked', 'off': 'unchecked', '取消勾选': 'unchecked',
            '反选': 'toggle', '切换': 'toggle',
        }
        desired = aliases.get(normalized, normalized)
        if desired not in {'checked', 'unchecked', 'toggle'}:
            raise ValueError(f'不支持的复选框目标状态: {value}')
        return desired

    @staticmethod
    def _checkbox_state_from_node(node: Optional[Dict[str, Any]]) -> Optional[bool]:
        if not node:
            return None
        for key in ('checked', 'selected'):
            value = node.get(key)
            if isinstance(value, bool):
                return value
        class_name = str(node.get('class_name') or '').lower()
        traits = str(node.get('traits') or '').lower()
        values = [node.get('value')]
        if any(token in class_name for token in ('checkbox', 'switch', 'toggle')):
            values.extend((node.get('text'), node.get('content_desc')))
        for value in values:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            normalized = str(value or '').strip().lower()
            if normalized in {'true', '1', 'yes', 'on', 'checked', 'selected', '已勾选'}:
                return True
            if normalized in {'false', '0', 'no', 'off', 'unchecked', 'unselected', '未勾选'}:
                return False
        if 'selected' in traits or 'checked' in traits:
            return True
        return None

    @classmethod
    def _is_checkbox_node(cls, node: Dict[str, Any]) -> bool:
        class_name = str(node.get('class_name') or '').lower()
        traits = str(node.get('traits') or '').lower()
        return (
            any(token in class_name for token in ('checkbox', 'switch', 'toggle'))
            or 'togglebutton' in traits
            or node.get('checked') is not None
            or node.get('selected') is not None
        )

    def _matching_checkbox_node(
        self,
        nodes: List[Dict[str, Any]],
        target: Any,
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            node for node in nodes
            if node.get('visible', True) is not False and self._is_checkbox_node(node)
        ]
        if not candidates:
            return None
        if isinstance(target, (list, tuple)) and len(target) >= 2:
            try:
                px, py = float(target[0]), float(target[1])
            except (TypeError, ValueError):
                px = py = None
            if px is not None and py is not None:
                containing = []
                for node in candidates:
                    bounds = node.get('bounds') or {}
                    try:
                        x1, y1 = float(bounds['x1']), float(bounds['y1'])
                        x2, y2 = float(bounds['x2']), float(bounds['y2'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if x1 <= px <= x2 and y1 <= py <= y2:
                        containing.append(((x2 - x1) * (y2 - y1), node))
                if containing:
                    containing.sort(key=lambda item: item[0])
                    return containing[0][1]

                def distance(item):
                    center = item.get('center') or {}
                    try:
                        return (
                            (float(center['x']) - px) ** 2
                            + (float(center['y']) - py) ** 2
                        )
                    except (KeyError, TypeError, ValueError):
                        return float('inf')

                nearest = min(candidates, key=distance)
                width, height = self._get_current_resolution()
                if distance(nearest) <= max(width * 0.35, height * 0.08) ** 2:
                    return nearest
        return candidates[0] if len(candidates) == 1 else None

    def _checkbox_node_for_selector(
        self,
        nodes: List[Dict[str, Any]],
        step: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """文字定位时只从 checkbox/switch 节点中匹配，不命中旁边链接。"""
        selector_type = str(step.get('selector_type') or '').strip().lower()
        selector = str(step.get('selector') or '').strip()
        if not selector or selector_type not in {'text', 'id'}:
            return None
        candidates = [
            node for node in nodes
            if node.get('visible', True) is not False and self._is_checkbox_node(node)
        ]
        ranked = []
        for node in candidates:
            if selector_type == 'id':
                values = [str(node.get('resource_id') or '').strip()]
            else:
                values = [
                    str(node.get(key) or '').strip()
                    for key in ('text', 'content_desc', 'resource_id')
                ]
            exact = any(value == selector for value in values if value)
            contains = any(selector in value for value in values if value)
            if not exact and not contains:
                continue
            bounds = node.get('bounds') or {}
            try:
                area = max(0, int(bounds['x2']) - int(bounds['x1'])) * max(
                    0, int(bounds['y2']) - int(bounds['y1'])
                )
            except (KeyError, TypeError, ValueError):
                area = 10**12
            ranked.append((0 if exact else 1, area, node))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0][2]

    def _checkbox_click_target(
        self,
        node: Optional[Dict[str, Any]],
        fallback: Any,
    ) -> Any:
        """返回复选框的安全点击点，避开 iOS 复合协议文本中的链接。"""
        if not node:
            return fallback
        bounds = node.get('bounds') or {}
        try:
            x1, y1 = float(bounds['x1']), float(bounds['y1'])
            x2, y2 = float(bounds['x2']), float(bounds['y2'])
        except (KeyError, TypeError, ValueError):
            return fallback
        width = max(0.0, x2 - x1)
        height = max(0.0, y2 - y1)
        if width <= 0 or height <= 0:
            return fallback

        class_name = str(node.get('class_name') or '').lower()
        traits = str(node.get('traits') or '').lower()
        checkbox_like = (
            any(token in class_name for token in ('checkbox', 'switch', 'toggle'))
            or 'togglebutton' in traits
        )
        if checkbox_like and width > height * 2.5:
            if self.platform == 'ios':
                # H5 checkbox 在 WDA 中常被暴露成覆盖“勾选框 + 多个协议链接”的
                # 宽 Switch 节点。左侧圆框靠近首行，不能点击整行中心。
                return (
                    int(round(x1 + height * 0.20)),
                    int(round(y1 + height * 0.25)),
                )
            # Android 的宽 CheckBox/CompoundButton 也只点击左侧控件区域。
            return (
                int(round(x1 + height * 0.50)),
                int(round((y1 + y2) / 2)),
            )
        center = node.get('center') or {}
        try:
            return int(round(float(center['x']))), int(round(float(center['y'])))
        except (KeyError, TypeError, ValueError):
            return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))

    def _wait_for_checkbox_state(
        self,
        target: Any,
        expected: bool,
        timeout: Any,
        interval: Any,
    ) -> tuple:
        deadline = time.monotonic() + max(0.1, float(timeout))
        last_node = None
        last_state = None
        while True:
            self.ensure_not_stopped()
            nodes = self._dump_current_ui_nodes()
            last_node = self._matching_checkbox_node(nodes, target)
            last_state = self._checkbox_state_from_node(last_node)
            if last_state is expected:
                return last_node, last_state, True
            if time.monotonic() >= deadline:
                return last_node, last_state, False
            self._sleep_interruptibly(
                min(max(0.05, float(interval)), max(0.0, deadline - time.monotonic()))
            )

    def _ios_keyboard_overlay_visible(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """识别会拦截页面点击的 iOS 系统/车牌键盘。"""
        if self.platform != 'ios':
            return False
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        if any(
            'keyboard' in str(node.get('class_name') or '').casefold()
            for node in nodes
        ):
            return True
        return self._license_plate_keyboard_visible(nodes)

    def _wait_for_ios_keyboard_hidden(
        self,
        timeout: Any = 1.5,
        interval: Any = 0.1,
    ) -> tuple:
        """等待键盘真正消失，并返回最新 UI 节点，避免点击被遮挡区域。"""
        deadline = time.monotonic() + max(0.1, float(timeout))
        last_nodes: List[Dict[str, Any]] = []
        while True:
            self.ensure_not_stopped()
            last_nodes = self._dump_current_ui_nodes()
            if not self._ios_keyboard_overlay_visible(last_nodes):
                return last_nodes, True
            if time.monotonic() >= deadline:
                return last_nodes, False
            self._sleep_interruptibly(
                min(max(0.05, float(interval)), max(0.0, deadline - time.monotonic()))
            )

    def _action_checkbox_toggle(self, step: Dict[str, Any]):
        """按目标状态处理原生/H5 复选框，已满足状态时不会重复点击。"""
        desired = self._normalize_checkbox_desired_state(
            step.get('desired_state', step.get('checked', 'checked'))
        )
        current_nodes = None
        keyboard_dismissal = None
        if self.platform == 'ios':
            current_nodes = self._dump_current_ui_nodes()
            if self._ios_keyboard_overlay_visible(current_nodes):
                keyboard_dismissal = self._dismiss_ios_system_keyboard(
                    context='复选框'
                )
                current_nodes, keyboard_hidden = self._wait_for_ios_keyboard_hidden(
                    step.get('keyboard_dismiss_timeout', 1.5),
                    step.get('keyboard_dismiss_interval', 0.1),
                )
                if not keyboard_hidden:
                    raise RuntimeError('iOS 键盘未收起，已阻止点击被遮挡的复选框')
        webview_result = self._try_webview_action(
            step,
            'checkbox',
            value=desired,
        )
        if webview_result:
            action_detail = webview_result.get('action_detail') or {}
            webview_expected = (
                not action_detail.get('before')
                if desired == 'toggle' and action_detail.get('before') is not None
                else {'checked': True, 'unchecked': False}.get(desired)
            )
            result = {
                'desired_state': desired,
                'before': action_detail.get('before'),
                'after': action_detail.get('after'),
                'clicked': bool(action_detail.get('clicked')),
                'verified': bool(action_detail.get('verified')),
                'method': 'webview_dom',
                'webview': webview_result,
                'keyboard_dismissal': keyboard_dismissal,
            }
            webview_state_supported = (
                result['before'] is not None or result['after'] is not None
            )
            if (
                step.get('verify_state', True)
                and webview_expected is not None
                and webview_state_supported
                and not result['verified']
            ):
                raise AssertionError(
                    f"WebView 复选框状态校验失败: 期望={desired}, "
                    f"点击前={result['before']}, 点击后={result['after']}"
                )
            if step.get('strict_verify', False) and not result['verified']:
                raise AssertionError('WebView 复选框状态校验失败')
            self._store_component_result(step, result)
            return

        if current_nodes is None:
            current_nodes = self._dump_current_ui_nodes()
        before_node = self._checkbox_node_for_selector(current_nodes, step)
        if before_node:
            target = region_center(before_node.get('bounds'))
        else:
            target = self._resolve_action_target(step)
            before_node = self._matching_checkbox_node(current_nodes, target)
            if before_node is None:
                # 目标解析可能刷新了 WDA 页面源；重新读取一次，避免使用解析前的
                # 空/旧节点回退到协议文字中心。
                current_nodes = self._dump_current_ui_nodes()
                before_node = self._matching_checkbox_node(current_nodes, target)
        selector_type = str(step.get('selector_type') or '').strip().lower()
        if before_node is None and selector_type in {'text', 'ocr'}:
            raise ValueError(
                '未识别到真实复选框控件，已拒绝点击旁边文字或协议链接；'
                '请采集左侧勾选框，或使用勾选框坐标/图片定位'
            )
        before = self._checkbox_state_from_node(before_node)
        click_target = self._checkbox_click_target(before_node, target)
        expected = (
            not before
            if desired == 'toggle' and before is not None
            else {'checked': True, 'unchecked': False}.get(desired)
        )
        should_click = desired == 'toggle' or before is None or before != expected
        if should_click:
            touch(click_target)

        after_node = before_node
        after = before
        verified = expected is not None and before == expected and not should_click
        if should_click and step.get('verify_state', True) and expected is not None:
            after_node, after, verified = self._wait_for_checkbox_state(
                click_target,
                expected,
                step.get('state_timeout', 2.0),
                step.get('retry_interval', 0.2),
            )
        elif should_click:
            after_node = self._matching_checkbox_node(
                self._dump_current_ui_nodes(), click_target
            )
            after = self._checkbox_state_from_node(after_node)
            verified = expected is not None and after == expected

        state_supported = before is not None or after is not None
        native_state_expected = self._smart_click_targets_checkbox(step)
        if (
            step.get('verify_state', True)
            and expected is not None
            and not verified
            and (state_supported or native_state_expected)
        ):
            if not state_supported:
                raise AssertionError(
                    '复选框状态校验失败: 已识别为 checkbox/switch，'
                    '但点击前后均未读取到状态'
                )
            raise AssertionError(
                f'复选框状态校验失败: 期望={desired}, 点击前={before}, 点击后={after}'
            )
        if step.get('strict_verify', False) and not verified:
            raise AssertionError('复选框未暴露可校验状态，请采集原生 checkbox/switch 节点')

        result = {
            'desired_state': desired,
            'before': before,
            'after': after,
            'clicked': should_click,
            'verified': verified,
            'state_supported': state_supported,
            'method': 'native_accessibility' if state_supported else 'smart_click',
            'target': list(click_target) if isinstance(click_target, tuple) else click_target,
            'keyboard_dismissal': keyboard_dismissal,
            'node': {
                'class_name': (after_node or before_node or {}).get('class_name'),
                'resource_id': (after_node or before_node or {}).get('resource_id'),
                'content_desc': (after_node or before_node or {}).get('content_desc'),
            },
        }
        self._last_locator_diagnostics = {
            **(self._last_locator_diagnostics or {}),
            'checkbox': result,
        }
        self._store_component_result(step, result)

    def _clear_focused_text(self, clear_count: Any = 128) -> Dict[str, Any]:
        """清空当前获得焦点的输入框，兼容 Android 与 iOS Airtest 设备。"""
        try:
            count = max(1, min(int(clear_count), 512))
        except (TypeError, ValueError):
            count = 128

        try:
            device = G.DEVICE
        except Exception:
            device = None
        device_class = device.__class__.__name__.lower() if device else ''
        if self.platform == 'android' or 'android' in device_class:
            try:
                G.DEVICE.shell('input keyevent KEYCODE_MOVE_END')
                G.DEVICE.shell('input keyevent ' + ' '.join(['KEYCODE_DEL'] * count))
                return {'cleared': True, 'method': 'android_keyevent', 'clear_count': count}
            except Exception as exc:
                logger.debug('Android shell 清空输入失败，回退 Airtest keyevent: %s', exc)
                try:
                    from airtest.core.api import keyevent
                    keyevent('KEYCODE_MOVE_END')
                    for _ in range(count):
                        keyevent('KEYCODE_DEL')
                    return {'cleared': True, 'method': 'airtest_keyevent', 'clear_count': count}
                except Exception as fallback_exc:
                    raise RuntimeError(f'清空输入失败: {fallback_exc}') from fallback_exc

        try:
            # Airtest 的 text() 默认会在文本末尾追加回车。清空输入框时若发送回车，
            # Safari/H5 输入框会先失焦，后续输入命令即使不报错也可能完全没有生效。
            airtest_text('\b' * count, enter=False)
            return {'cleared': True, 'method': 'ios_backspace', 'clear_count': count}
        except Exception as exc:
            raise RuntimeError(f'iOS 清空输入失败: {exc}') from exc

    @staticmethod
    def _is_text_input_node(node: Dict[str, Any]) -> bool:
        return UiFlowRunner._text_input_class_name(node.get('class_name'))

    @staticmethod
    def _is_ios_browser_address_input(node: Dict[str, Any]) -> bool:
        resource_id = str(node.get('resource_id') or '').strip()
        if resource_id in IOS_SAFARI_ADDRESS_FIELD_RESOURCE_IDS:
            return True

        text_values = ' '.join(UiFlowRunner._node_text_values(node)).strip()
        if not text_values:
            return False
        return bool(re.search(r'\b(?:https?://|localhost(?::\d+)?|(?:\d{1,3}\.){3}\d{1,3})(?:[/:?#]|$)', text_values))

    @staticmethod
    def _normalize_input_value(value: Any) -> str:
        return re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', str(value or '')).strip()

    @classmethod
    def _input_value_matches(cls, node: Dict[str, Any], expected: Any) -> bool:
        actual = cls._normalize_input_value(node.get('value'))
        placeholder = cls._normalize_input_value(node.get('placeholder'))
        expected_text = cls._normalize_input_value(expected)
        if not actual or (placeholder and actual == placeholder):
            return False
        if actual == expected_text:
            return True

        class_name = str(node.get('class_name') or '')
        if 'SecureTextField' in class_name:
            # WDA 对密码框通常只返回等长圆点，不能读取明文。
            return len(actual) == len(expected_text) or bool(re.fullmatch(r'[•●*]+', actual))

        expected_digits = re.sub(r'\D+', '', expected_text)
        actual_digits = re.sub(r'\D+', '', actual)
        if expected_digits and expected_digits == actual_digits:
            digitish_pattern = r'[\d\s\-()+.]+'
            return bool(
                re.fullmatch(digitish_pattern, expected_text)
                and re.fullmatch(digitish_pattern, actual)
            )

        if cls._is_ios_browser_address_input(node):
            # Safari 地址栏可能只暴露当前 URL 的可见部分，允许稳定的包含关系。
            shorter = min(len(actual), len(expected_text))
            return shorter >= 4 and (expected_text in actual or actual in expected_text)
        return False

    def _input_node_identity_score(
        self,
        step: Dict[str, Any],
        node: Dict[str, Any],
    ) -> Tuple[int, str]:
        score = 0
        matched_hint = ''
        for hint in self._input_locator_hints(step):
            for actual in self._node_text_values(node):
                match_score = self._input_text_match_score(hint, actual)
                if match_score > score:
                    score = match_score
                    matched_hint = hint
        return score, matched_hint

    def _matching_input_node(
        self,
        step: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        target: Any,
    ) -> Optional[Dict[str, Any]]:
        inputs = [node for node in nodes if self._is_text_input_node(node)]
        if not inputs:
            return None

        focused = [node for node in inputs if node.get('focused')]
        if focused:
            hints = self._input_locator_hints(step)
            if not hints:
                return focused[0]
            for node in focused:
                identity_score, _ = self._input_node_identity_score(step, node)
                if identity_score:
                    return node

        if isinstance(target, (list, tuple)) and len(target) >= 2:
            try:
                px, py = float(target[0]), float(target[1])
            except (TypeError, ValueError):
                return None
            containing = []
            for node in inputs:
                bounds = node.get('bounds') or {}
                if (
                    float(bounds.get('x1', 0)) <= px <= float(bounds.get('x2', 0))
                    and float(bounds.get('y1', 0)) <= py <= float(bounds.get('y2', 0))
                ):
                    containing.append(node)
            if containing:
                selected = min(
                    containing,
                    key=lambda item: (
                        item['bounds']['x2'] - item['bounds']['x1']
                    ) * (
                        item['bounds']['y2'] - item['bounds']['y1']
                    ),
                )
                hints = self._input_locator_hints(step)
                if not hints:
                    return selected
                identity_score, _ = self._input_node_identity_score(step, selected)
                if identity_score:
                    return selected

        for candidate in self._step_locator_candidates(step):
            if candidate.get('kind') != 'semantic':
                continue
            node, _diagnostics = select_best_ui_node(
                candidate.get('fingerprint') or {},
                inputs,
                minimum_score=float(candidate.get('minimum_score', 36)),
            )
            if node:
                return node
        return None

    def _verify_ios_input(self, step: Dict[str, Any], value: Any, target: Any) -> None:
        if self.platform != 'ios' or step.get('verify_input', True) is False:
            return
        expected = self._normalize_input_value(value)
        if not expected:
            return

        timeout = max(0.1, float(step.get('input_verify_timeout', 2.0)))
        deadline = time.monotonic() + timeout
        last_node = None
        while True:
            self.ensure_not_stopped()
            nodes = self._dump_current_ui_nodes()
            node = self._matching_input_node(step, nodes, target)
            if node:
                last_node = node
                identity_score, matched_hint = self._input_node_identity_score(step, node)
                hints = self._input_locator_hints(step)
                if hints and not identity_score:
                    if time.monotonic() >= deadline:
                        break
                    self._sleep_interruptibly(min(0.25, max(0.0, deadline - time.monotonic())))
                    continue
                actual_value = self._normalize_input_value(node.get('value'))
                identity_scoped_contains = bool(
                    identity_score
                    and expected in actual_value
                    and actual_value != self._normalize_input_value(node.get('placeholder'))
                )
                if self._input_value_matches(node, expected) or identity_scoped_contains:
                    diagnostics = {
                        'matched': True,
                        'field': node.get('resource_id') or node.get('content_desc'),
                        'class_name': node.get('class_name'),
                        'value_length': len(self._normalize_input_value(node.get('value'))),
                        'identity_score': identity_score,
                        'matched_hint': matched_hint,
                    }
                    self._last_locator_diagnostics = {
                        **(self._last_locator_diagnostics or {}),
                        'input_verification': diagnostics,
                    }
                    logger.info('iOS 输入校验成功: %s', diagnostics)
                    return
            if time.monotonic() >= deadline:
                break
            self._sleep_interruptibly(min(0.25, max(0.0, deadline - time.monotonic())))

        ocr_diagnostics = self._verify_recorded_ios_input_by_ocr(step, expected)
        if ocr_diagnostics.get('matched'):
            self._last_locator_diagnostics = {
                **(self._last_locator_diagnostics or {}),
                'input_verification': ocr_diagnostics,
            }
            logger.info('iOS 录制输入 OCR 校验成功: %s', ocr_diagnostics)
            return

        actual = self._normalize_input_value((last_node or {}).get('value'))
        placeholder = self._normalize_input_value((last_node or {}).get('placeholder'))
        diagnostics = {
            'matched': False,
            'field': (last_node or {}).get('resource_id') or (last_node or {}).get('content_desc'),
            'class_name': (last_node or {}).get('class_name'),
            'actual_length': len(actual),
            'placeholder_visible': bool(placeholder and actual == placeholder),
        }
        if (
            self._is_input_action_step(step)
            and step.get('selector') in (None, '')
            and not step.get('_inherited_ios_recording_input_target')
        ):
            diagnostics['blank_selector_input'] = True
            diagnostics['suggestion'] = (
                '输入步骤缺少稳定定位；请重新录制该输入框，或在保存脚本前继承上一输入框定位'
            )
        self._last_locator_diagnostics = {
            **(self._last_locator_diagnostics or {}),
            'input_verification': diagnostics,
        }
        hint = diagnostics.get('suggestion')
        if hint:
            raise AssertionError(
                f"iOS 输入校验失败: 字段 '{diagnostics['field'] or 'unknown'}' "
                f"未写入期望文本。{hint}"
            )
        raise AssertionError(
            f"iOS 输入校验失败: 字段 '{diagnostics['field'] or 'unknown'}' 未写入期望文本"
        )

    def _verify_recorded_ios_input_by_ocr(
        self,
        step: Dict[str, Any],
        expected: str,
    ) -> Dict[str, Any]:
        if self.platform != 'ios' or not OCR_AVAILABLE:
            return {'matched': False, 'method': 'screen_ocr', 'reason': 'unsupported'}
        is_exploratory = bool(step.get('exploratory'))
        is_generated_recording_input = self._is_generated_recording_input_name(step.get('name'))
        has_blank_selector_recorded_input = (
            self._is_input_action_step(step)
            and step.get('selector') in (None, '')
        )
        has_inherited_recording_input = bool(step.get('_inherited_ios_recording_input_target'))
        if not (
            is_generated_recording_input
            or is_exploratory
            or has_blank_selector_recorded_input
            or has_inherited_recording_input
        ):
            return {'matched': False, 'method': 'screen_ocr', 'reason': 'not-recorded-input'}
        if (
            step.get('selector') not in (None, '')
            and not has_inherited_recording_input
            and not is_exploratory
        ):
            return {'matched': False, 'method': 'screen_ocr', 'reason': 'step-has-selector'}

        try:
            screen = G.DEVICE.snapshot()
            if screen is None:
                return {'matched': False, 'method': 'screen_ocr', 'reason': 'snapshot-empty'}
            ocr_text = self._get_ocr_helper().recognize_text(
                screen,
                min_confidence=0.35,
                use_cache=False,
            )
        except Exception as exc:
            logger.debug('iOS 录制输入 OCR 校验失败: %s', exc)
            return {'matched': False, 'method': 'screen_ocr', 'reason': str(exc)}

        normalized_ocr = self._normalize_input_value(ocr_text)
        match_detail = self._recorded_input_ocr_match_detail(expected, normalized_ocr)
        return {
            'matched': match_detail['matched'],
            'method': 'screen_ocr',
            'field': 'recorded-ios-input',
            'class_name': '',
            'expected_length': len(expected),
            'ocr_length': len(normalized_ocr),
            **match_detail,
        }

    @classmethod
    def _recorded_input_ocr_match_detail(cls, expected: str, ocr_text: str) -> Dict[str, Any]:
        expected_text = cls._normalize_input_value(expected)
        normalized_ocr = cls._normalize_input_value(ocr_text)
        expected_digits = re.sub(r'\D+', '', expected_text)
        ocr_digits = re.sub(r'\D+', '', normalized_ocr)
        if expected_text and expected_text in normalized_ocr:
            return {
                'matched': True,
                'match_mode': 'exact_contains',
                'digit_match': bool(expected_digits and expected_digits in ocr_digits),
            }
        if (
            expected_digits
            and expected_digits in ocr_digits
            and len(expected_digits) >= min(4, len(expected_text))
        ):
            return {
                'matched': True,
                'match_mode': 'digit_contains',
                'digit_match': True,
            }

        expected_compact = re.sub(r'\s+', '', expected_text)
        ocr_compact = re.sub(r'\s+', '', normalized_ocr)
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', expected_compact))
        if has_cjk and len(expected_compact) >= 3 and ocr_compact:
            match_count = 0
            search_from = 0
            for char in expected_compact:
                index = ocr_compact.find(char, search_from)
                if index < 0:
                    continue
                match_count += 1
                search_from = index + 1
            threshold = max(3, int(math.ceil(len(expected_compact) * 0.75)))
            if match_count >= threshold:
                return {
                    'matched': True,
                    'match_mode': 'cjk_ordered_similarity',
                    'matched_chars': match_count,
                    'required_chars': threshold,
                    'digit_match': bool(expected_digits and expected_digits in ocr_digits),
                }

        return {
            'matched': False,
            'match_mode': 'none',
            'digit_match': bool(expected_digits and expected_digits in ocr_digits),
        }

    def _action_clear_text(self, step: Dict[str, Any]):
        """定位输入框并清空已有内容。"""
        webview_result = self._try_webview_action(
            {**step, 'clear_first': True},
            'input',
            value='',
            send_enter=False,
        )
        if webview_result:
            result = {'cleared': True, 'method': 'webview', 'webview': webview_result}
            self._store_component_result(step, result)
            return

        has_selector = bool(step.get('element_id') or step.get('selector'))
        target_step = {
            **step,
            'prefer_input_control': True,
            'block_coordinate_fallback': step.get('block_coordinate_fallback', True),
        }
        target = self._resolve_action_target(target_step) if has_selector else None
        if target:
            touch(target)
            self._sleep_interruptibly(step.get('focus_wait', 0.2))
        result = self._clear_focused_text(step.get('clear_count', 128))
        if target:
            result['target'] = list(target) if isinstance(target, tuple) else target
        self._store_component_result(step, result)
    
    def _action_input(self, step: Dict[str, Any]):
        """输入文本"""
        value = step.get('value', '')
        
        # 解析变量表达式（如随机数函数）
        from apps.core.variable_resolver import resolve_variables
        value = resolve_variables(value)
        action_step = self._inherit_ios_recorded_input_target(step)
        
        send_enter = action_step.get('send_enter', False)
        webview_result = self._try_webview_action(
            action_step,
            'input',
            value=value,
            send_enter=send_enter,
        )
        if webview_result:
            self._last_locator_diagnostics = {
                'step': step.get('name', step.get('type', '输入文本')),
                'selected_strategy': 'webview_dom',
                'webview': webview_result,
            }
            if action_step is not step:
                self._last_ios_recorded_input_target_config = None
            return

        self._guard_ios_input_no_blocking_modal(action_step, 'before_locate')
        has_selector = bool(action_step.get('element_id') or action_step.get('selector'))
        target_step = {
            **action_step,
            'prefer_input_control': True,
            'block_coordinate_fallback': action_step.get('block_coordinate_fallback', True),
        }
        target = self._resolve_action_target(target_step) if has_selector else None
        
        if target:
            touch(target)
            self._sleep_interruptibly(action_step.get('focus_wait', 0.3))
            self._guard_ios_input_no_blocking_modal(action_step, 'after_focus')

        if action_step.get('clear_first', False):
            self._clear_focused_text(action_step.get('clear_count', 128))
        
        masked = bool(action_step.get('sensitive')) or str(action_step.get('input_kind') or '').lower() == 'password'
        logger.info("输入文本: %s", '***' if masked else value)
        try:
            if self.platform == 'ios':
                # iOS 先写值并校验，再按需发送回车；否则页面跳转后无法读取原输入框。
                airtest_text(value, enter=False)
                self._verify_ios_input(action_step, value, target)
                if send_enter:
                    airtest_text('', enter=True)
            else:
                # 显式透传 enter，避免 Airtest 默认追加换行。
                airtest_text(value, enter=bool(send_enter))
        finally:
            if action_step is not step:
                self._last_ios_recorded_input_target_config = None

    @staticmethod
    def _normalize_smart_input_value(value: Any, input_kind: str) -> str:
        text_value = str(value if value is not None else '')
        kind = str(input_kind or 'auto').strip().lower()
        if kind in ('phone', 'verification_code', 'bank_card'):
            return re.sub(r'\D+', '', text_value)
        if kind == 'money':
            compact = re.sub(r'[^0-9.]', '', text_value)
            if compact.count('.') > 1:
                first, *rest = compact.split('.')
                compact = first + '.' + ''.join(rest)
            if '.' in compact:
                integer, decimal = compact.split('.', 1)
                compact = f'{integer or "0"}.{decimal[:2]}'
            return compact
        if kind == 'license_plate':
            return re.sub(r'\s+', '', text_value).upper()
        return text_value

    @staticmethod
    def _infer_smart_input_kind(step: Dict[str, Any], value: Any) -> str:
        """根据字段语义与值形态推断输入类型。"""
        configured = str(step.get('input_kind') or 'auto').strip().lower()
        if configured and configured != 'auto':
            return configured

        fingerprint = step.get('fingerprint') or {}
        hints = ' '.join(str(item or '') for item in (
            step.get('name'), step.get('description'), step.get('selector'),
            step.get('text'), step.get('webview_text'), fingerprint.get('text'),
            fingerprint.get('resource_id'), fingerprint.get('content_desc'),
        )).casefold()
        raw_value = str(value if value is not None else '').strip()
        compact = re.sub(r'\s+', '', raw_value)
        digits = re.sub(r'\D+', '', raw_value)

        if re.fullmatch(r'[\u4e00-\u9fff][A-Za-z][A-Za-z0-9]{5,6}', compact):
            return 'license_plate'
        if any(token in hints for token in ('密码', 'password', 'passwd', 'pwd', 'pin')):
            return 'password'
        if any(token in hints for token in ('金额', '支付金额', 'money', 'amount', 'price', '余额')):
            return 'money'
        if any(token in hints for token in ('银行卡', '卡号', 'bank card', 'bank_card', 'cardno', 'card_no')):
            return 'bank_card'
        if any(token in hints for token in ('验证码', '校验码', 'verify', 'verification', 'captcha', 'otp', 'sms code')):
            return 'verification_code'
        if any(token in hints for token in ('手机号', '手机号码', 'mobile', 'phone', 'tel')):
            return 'phone'
        if any(symbol in raw_value for symbol in ('￥', '¥', '$', '€')) or re.fullmatch(r'\d+[.,]\d{1,3}', compact):
            return 'money'
        if re.fullmatch(r'1\d{10}', digits) and len(compact) == 11:
            return 'phone'
        if digits == compact and 12 <= len(digits) <= 19:
            return 'bank_card'
        if digits == compact and 4 <= len(digits) <= 8:
            return 'verification_code'
        return 'text'

    def _action_smart_input(self, step: Dict[str, Any]):
        normalized = dict(step)
        inferred_kind = self._infer_smart_input_kind(step, step.get('value', ''))
        normalized['input_kind'] = inferred_kind
        normalized['value'] = self._normalize_smart_input_value(
            step.get('value', ''),
            inferred_kind,
        )
        if (
            inferred_kind == 'license_plate'
            and not self._license_plate_target_is_native_text(normalized)
        ):
            normalized['plate'] = normalized['value']
            self._action_license_plate_input(normalized)
        else:
            self._action_input(normalized)
        self._last_locator_diagnostics = {
            **(self._last_locator_diagnostics or {}),
            'input_kind': inferred_kind,
            'value_length': len(str(normalized['value'])),
        }

    def _action_bank_card_input(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['input_kind'] = 'bank_card'
        self._action_smart_input(normalized)

    def _action_money_input(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['input_kind'] = 'money'
        self._action_smart_input(normalized)

    def _action_element_probe(self, step: Dict[str, Any]):
        """只探测并记录元素，不触发点击。"""
        config, element_type = self._webview_step_config(step)
        result: Dict[str, Any]
        if self._should_try_webview(step, config, element_type):
            client = self._get_appium_webview_client()
            if client is None:
                raise RuntimeError('WebView 元素探测需要可用的 Appium 会话')
            context, context_detail = client.switch_to_webview(
                preferred_context=step.get('webview_context') or config.get('webview_context') or '',
                timeout=float(step.get('webview_timeout', step.get('timeout', 5))),
            )
            element_id, diagnostics = client.find_element(config)
            heal_detail = self._record_self_heal(
                step,
                diagnostics.get('selected_strategy'),
                diagnostics.get('attempts') or [],
            )
            result = {
                'found': True,
                'context': context,
                'context_detail': context_detail,
                'element_id': element_id,
                'diagnostics': diagnostics,
                'self_heal': heal_detail,
            }
        else:
            target = self._resolve_action_target(step)
            result = {
                'found': True,
                'target': list(target) if isinstance(target, tuple) else target,
                'diagnostics': self._last_locator_diagnostics,
            }
        self.context['outputs']['last'] = result
        save_as = str(step.get('save_as') or '').strip()
        if save_as:
            self._set_variable(save_as, result, step.get('scope', 'local'))

    def _action_pull_refresh(self, step: Dict[str, Any]):
        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            raise RuntimeError('无法读取设备分辨率，不能执行下拉刷新')
        start = (int(width * 0.5), int(height * 0.28))
        end = (int(width * 0.5), int(height * 0.72))
        swipe(start, end, duration=float(step.get('duration', 0.6)))
        self._sleep_interruptibly(step.get('wait_after', 1))

    def _action_switch_native(self, step: Dict[str, Any]):
        client = self._get_appium_webview_client()
        if client is None:
            raise RuntimeError('切换 Native 需要可用的 Appium 会话')
        context = client.switch_to_native()
        self.context['outputs']['last'] = {'context': context}

    def _action_switch_webview(self, step: Dict[str, Any]):
        client = self._get_appium_webview_client()
        if client is None:
            raise RuntimeError('切换 WebView 需要可用的 Appium 会话')
        context, diagnostics = client.switch_to_webview(
            preferred_context=step.get('webview_context', ''),
            timeout=float(step.get('timeout', 5)),
        )
        self.context['outputs']['last'] = {
            'context': context,
            'diagnostics': diagnostics,
        }

    def _action_click_web_element(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['context_preference'] = 'webview'
        self._action_click(normalized)

    def _action_input_web_text(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['context_preference'] = 'webview'
        self._action_input(normalized)

    def _action_execute_js(self, step: Dict[str, Any]):
        client = self._get_appium_webview_client()
        if client is None:
            raise RuntimeError('执行 JavaScript 需要可用的 Appium 会话')
        context, diagnostics = client.switch_to_webview(
            preferred_context=step.get('webview_context', ''),
            timeout=float(step.get('timeout', 5)),
        )
        result = client.execute_script(step.get('script', ''), step.get('args') or [])
        output = {'context': context, 'diagnostics': diagnostics, 'result': result}
        self.context['outputs']['last'] = output
        save_as = str(step.get('save_as') or '').strip()
        if save_as:
            self._set_variable(save_as, result, step.get('scope', 'local'))

    def _action_scroll_web_page(self, step: Dict[str, Any]):
        direction = str(step.get('direction') or 'down').lower()
        distance = float(step.get('distance', 0.8))
        axis = 'innerWidth' if direction in ('left', 'right') else 'innerHeight'
        sign = -1 if direction in ('up', 'left') else 1
        x_expr = f'window.{axis} * {sign * distance}' if direction in ('left', 'right') else '0'
        y_expr = f'window.{axis} * {sign * distance}' if direction in ('up', 'down') else '0'
        normalized = dict(step)
        normalized['script'] = f'window.scrollBy({x_expr}, {y_expr}); return {{x: window.scrollX, y: window.scrollY}};'
        normalized['args'] = []
        self._action_execute_js(normalized)

    def _action_page_detect(self, step: Dict[str, Any]):
        result = self._current_page_state()
        result.update(self._detect_semantic_page(step, result))
        self.context['outputs']['last'] = result
        save_as = str(step.get('save_as') or 'page_detect_result').strip()
        if save_as:
            self._set_variable(save_as, result, step.get('scope', 'local'))

    def _visual_locate_target(self, step: Dict[str, Any]) -> tuple:
        description = str(step.get('description') or step.get('text') or '').strip()
        reference_image = str(step.get('reference_image') or '').strip()
        fallback_position = step.get('fallback_position')
        raw_order = step.get('strategy_order') or ['semantic', 'ocr', 'image', 'position']
        if isinstance(raw_order, str):
            try:
                parsed_order = json.loads(raw_order)
                raw_order = parsed_order if isinstance(parsed_order, list) else raw_order.split(',')
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_order = raw_order.split(',')
        strategy_order = [str(item).strip().lower() for item in raw_order if str(item).strip()]
        if not description and not reference_image and not fallback_position:
            raise ValueError('视觉定位至少需要 description、reference_image 或 fallback_position')

        timeout = max(0.1, float(step.get('timeout', 5)))
        interval = max(0.05, float(step.get('retry_interval', 0.4)))
        deadline = time.monotonic() + timeout
        attempts = []
        while True:
            for strategy in strategy_order:
                self.ensure_not_stopped()
                started = time.monotonic()
                target = None
                detail: Dict[str, Any] = {}
                try:
                    if strategy == 'semantic' and description:
                        target, detail = self._semantic_target(
                            {'version': 1, 'text': description},
                            float(step.get('semantic_min_score', 20)),
                        )
                    elif strategy == 'ocr' and description:
                        target, detail = self._ocr_target({
                            'text': description,
                            'match_mode': step.get('match_mode', 'contains'),
                            'min_confidence': float(step.get('ocr_min_confidence', 0.45)),
                            'languages': step.get('ocr_languages') or ['ch_sim', 'en'],
                            'region': step.get('region'),
                        })
                    elif strategy == 'image' and reference_image:
                        image_scope = str(step.get('image_scope') or 'common')
                        image_path = reference_image
                        if not os.path.isabs(image_path):
                            image_path = os.path.join(self.image_base_dir, image_scope, reference_image)
                        if os.path.isfile(image_path):
                            target = exists(self._create_template(image_path, step))
                        else:
                            detail = {'error': f'图片不存在: {image_path}'}
                    elif strategy == 'position' and fallback_position:
                        position_config = {'normalized_position': fallback_position}
                        if isinstance(fallback_position, (list, tuple)) and len(fallback_position) >= 2:
                            position_config = {
                                'normalized_position': {
                                    'x': fallback_position[0],
                                    'y': fallback_position[1],
                                }
                            }
                        target = scale_point(position_config, self._get_current_resolution())
                        detail = {'fallback': True}
                except Exception as exc:
                    detail = {'error': str(exc)}

                attempt = {
                    'strategy': strategy,
                    'matched': bool(target),
                    'elapsed': round(time.monotonic() - started, 3),
                    'detail': detail,
                }
                attempts.append(attempt)
                if target:
                    diagnostics = {
                        'description': description,
                        'selected_strategy': strategy,
                        'target': list(target) if isinstance(target, tuple) else target,
                        'attempts': attempts,
                        'current_resolution': self._get_current_resolution(),
                    }
                    self._last_locator_diagnostics = diagnostics
                    return target, diagnostics

            if time.monotonic() >= deadline:
                break
            self._sleep_interruptibly(min(interval, max(0.0, deadline - time.monotonic())))

        raise ValueError(
            f"视觉定位失败: description='{description}', "
            f"已尝试 {[item['strategy'] for item in attempts]}"
        )

    def _action_visual_locate(self, step: Dict[str, Any]):
        target, diagnostics = self._visual_locate_target(step)
        result = {
            'found': True,
            'target': list(target) if isinstance(target, tuple) else target,
            'diagnostics': diagnostics,
        }
        self._store_component_result(step, result)

    def _action_visual_click(self, step: Dict[str, Any]):
        target, diagnostics = self._visual_locate_target(step)
        touch(target)
        self._store_component_result(step, {
            'completed': True,
            'target': list(target) if isinstance(target, tuple) else target,
            'diagnostics': diagnostics,
        })

    def _action_visual_assert(self, step: Dict[str, Any]):
        expected_exists = bool(step.get('expected_exists', True))
        try:
            target, diagnostics = self._visual_locate_target(step)
            found = True
        except ValueError as exc:
            target = None
            diagnostics = {'error': str(exc)}
            found = False
        if found != expected_exists:
            raise AssertionError(
                f'视觉断言失败: 期望存在={expected_exists}, 实际存在={found}'
            )
        self._store_component_result(step, {
            'passed': True,
            'found': found,
            'target': list(target) if isinstance(target, tuple) else target,
            'diagnostics': diagnostics,
        })

    def _action_self_heal_click(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['self_heal_mode'] = step.get('self_heal_mode', 'report')
        self._action_smart_click(normalized)
        result = {
            'completed': True,
            'self_heal': (
                self._last_webview_diagnostics.get('self_heal')
                or self._last_locator_diagnostics.get('self_heal')
            ),
            'locator': self._last_locator_diagnostics,
            'webview': self._last_webview_diagnostics,
        }
        self._store_component_result(step, result)

    def _action_self_heal_input(self, step: Dict[str, Any]):
        normalized = dict(step)
        normalized['self_heal_mode'] = step.get('self_heal_mode', 'report')
        self._action_smart_input(normalized)
        result = {
            'completed': True,
            'self_heal': (
                self._last_webview_diagnostics.get('self_heal')
                or self._last_locator_diagnostics.get('self_heal')
            ),
            'locator': self._last_locator_diagnostics,
            'webview': self._last_webview_diagnostics,
        }
        self._store_component_result(step, result)

    @staticmethod
    def _normalize_component_target(value: Any) -> Dict[str, Any]:
        """把复合组件里的目标统一转换为定位配置。"""
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return {'selector_type': 'text', 'selector': stripped}
        return {}

    def _component_target_step(
        self,
        parent: Dict[str, Any],
        target: Any,
        name: str,
    ) -> Dict[str, Any]:
        target_config = self._normalize_component_target(target)
        inherited_keys = (
            'timeout', 'image_scope', 'image_threshold', 'semantic_min_score',
            'ocr_min_confidence', 'context_preference', 'webview_context',
            'webview_timeout', 'retry_interval',
        )
        merged = {
            key: parent[key]
            for key in inherited_keys
            if key in parent and parent[key] not in (None, '')
        }
        merged.update(target_config)
        merged['name'] = name
        return merged

    def _click_component_target(
        self,
        parent: Dict[str, Any],
        target: Any,
        name: str,
        required: bool = True,
    ) -> bool:
        target_step = self._component_target_step(parent, target, name)
        if not any(target_step.get(key) for key in (
            'element_id', 'selector', 'webview_css', 'webview_xpath', 'webview_text'
        )):
            if required:
                raise ValueError(f'{name} 未配置定位目标')
            return False
        self._action_smart_click(target_step)
        return True

    def _input_component_target(
        self,
        parent: Dict[str, Any],
        target: Any,
        value: Any,
        name: str,
        input_kind: str = 'auto',
        sensitive: bool = False,
    ) -> None:
        target_step = self._component_target_step(parent, target, name)
        if not any(target_step.get(key) for key in (
            'element_id', 'selector', 'webview_css', 'webview_xpath', 'webview_text'
        )):
            raise ValueError(f'{name} 未配置定位目标')
        target_step.update({
            'value': value,
            'input_kind': input_kind,
            'sensitive': sensitive,
            'clear_first': target_step.get('clear_first', parent.get('clear_first', True)),
        })
        self._action_smart_input(target_step)

    def _probe_component_target(
        self,
        parent: Dict[str, Any],
        target: Any,
        name: str,
        required: bool = True,
    ) -> Optional[Dict[str, Any]]:
        target_step = self._component_target_step(parent, target, name)
        if not any(target_step.get(key) for key in (
            'element_id', 'selector', 'webview_css', 'webview_xpath', 'webview_text'
        )):
            if required:
                raise ValueError(f'{name} 未配置定位目标')
            return None
        try:
            self._action_element_probe(target_step)
            return self.context['outputs'].get('last')
        except Exception:
            if required:
                raise
            return None

    def _store_component_result(self, step: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.context['outputs']['last'] = result
        save_as = str(step.get('save_as') or '').strip()
        if save_as:
            self._set_variable(save_as, result, step.get('scope', 'local'))

    def _action_fill_form(self, step: Dict[str, Any]):
        fields = step.get('fields') or []
        if not isinstance(fields, list) or not fields:
            raise ValueError('填写表单需要 fields 数组')
        results = []
        for index, field in enumerate(fields, 1):
            if not isinstance(field, dict):
                raise ValueError(f'表单第 {index} 个字段格式错误')
            field_name = str(field.get('name') or f'字段{index}')
            action = str(field.get('action') or 'input').lower()
            target = field.get('target') or field
            if action in ('click', 'select', 'toggle'):
                self._click_component_target(step, target, f'表单-{field_name}')
                result = {'name': field_name, 'action': action, 'completed': True}
            else:
                value = field.get('value', '')
                input_kind = str(field.get('input_kind') or 'auto')
                sensitive = bool(field.get('sensitive')) or input_kind == 'password'
                self._input_component_target(
                    step, target, value, f'表单-{field_name}', input_kind, sensitive
                )
                result = {
                    'name': field_name,
                    'action': 'input',
                    'input_kind': input_kind,
                    'value_length': len(str(value or '')),
                    'completed': True,
                }
            results.append(result)
            self._sleep_interruptibly(field.get('interval', step.get('field_interval', 0.2)))
        output = {'completed': True, 'field_count': len(results), 'fields': results}
        self._store_component_result(step, output)

    def _action_login(self, step: Dict[str, Any]):
        mode = str(step.get('mode') or 'password').lower()
        if mode not in ('password', 'verification_code', 'biometric', 'fingerprint', 'faceid'):
            raise ValueError(f'不支持的登录方式: {mode}')

        if mode in ('biometric', 'fingerprint', 'faceid'):
            self._click_component_target(step, step.get('biometric_target'), '生物识别登录')
        else:
            account = step.get('account', '')
            if step.get('account_target'):
                self._input_component_target(
                    step, step.get('account_target'), account, '登录账号',
                    step.get('account_kind', 'phone'),
                )
            if mode == 'password':
                self._input_component_target(
                    step, step.get('password_target'), step.get('password', ''),
                    '登录密码', 'password', True,
                )
            else:
                self._click_component_target(
                    step, step.get('get_code_target'), '获取验证码', required=False
                )
                self._sleep_interruptibly(step.get('code_wait', 0))
                self._input_component_target(
                    step, step.get('verification_code_target'),
                    step.get('verification_code', ''), '登录验证码', 'verification_code', True,
                )
            self._click_component_target(step, step.get('submit_target'), '提交登录')

        success_probe = None
        if step.get('success_target'):
            success_probe = self._probe_component_target(
                step, step.get('success_target'), '登录成功标识'
            )
        if isinstance(step.get('success_page'), dict):
            page_step = dict(step)
            page_step.update(step['success_page'])
            self._wait_for_page(page_step)
        output = {'completed': True, 'mode': mode, 'success_probe': success_probe}
        self._store_component_result(step, output)

    def _action_close_popup(self, step: Dict[str, Any]):
        targets = step.get('targets') or []
        if not isinstance(targets, list) or not targets:
            raise ValueError('关闭弹窗需要 targets 数组')
        attempts = []
        for index, target in enumerate(targets, 1):
            name = str(target.get('name') if isinstance(target, dict) else '') or f'弹窗目标{index}'
            target_step = self._component_target_step(step, target, name)
            target_step['timeout'] = target_step.get('timeout', step.get('probe_timeout', 0.8))
            try:
                self._action_element_probe(target_step)
                self._action_smart_click(target_step)
                output = {'closed': True, 'matched_target': name, 'attempts': attempts + [name]}
                self._store_component_result(step, output)
                return
            except Exception as exc:
                attempts.append({'name': name, 'error': str(exc)})
        if step.get('required', False):
            raise AssertionError(f'未发现可关闭的弹窗，尝试: {attempts}')
        self._store_component_result(step, {'closed': False, 'attempts': attempts})

    def _action_scroll_list(self, step: Dict[str, Any]):
        count = max(1, int(step.get('count', 1)))
        for index in range(count):
            self._action_swipe({
                'name': f"{step.get('name', '列表滚动')}-{index + 1}",
                'direction': step.get('direction', 'up'),
                'distance': step.get('distance', 0.55),
                'duration': step.get('duration', 0.5),
            })
            if index + 1 < count:
                self._sleep_interruptibly(step.get('interval', 0.5))
        self._store_component_result(step, {
            'completed': True,
            'count': count,
            'direction': step.get('direction', 'up'),
        })

    def _action_popup_select_text(self, step: Dict[str, Any]):
        """通过输入文本或斜杠分隔路径选择当前滚轮/级联弹框。"""
        value = str(step.get('value') or step.get('option_label') or '').strip()
        result = select_popup_text_path(
            value,
            resolution=self._get_current_resolution(),
            get_nodes=self._dump_current_ui_nodes,
            tap=lambda x, y: touch((x, y)),
            swipe=lambda x1, y1, x2, y2, duration: swipe(
                (x1, y1), (x2, y2), duration=duration
            ),
            pause=self._sleep_interruptibly,
            max_swipes=step.get('max_swipes', 8),
            interval=step.get('interval', 0.25),
            duration=step.get('duration', 0.35),
            popup_region=step.get('popup_region'),
            match_mode=step.get('match_mode', 'exact'),
        )
        self._last_locator_diagnostics = {
            'step': step.get('name', '弹框选择'),
            'selected_strategy': 'popup_text_path',
            'popup_selection': result,
            'current_resolution': self._get_current_resolution(),
        }
        self._store_component_result(step, result)

    def _action_search_product(self, step: Dict[str, Any]):
        search_target = step.get('search_target')
        input_step = self._component_target_step(step, search_target, '商品搜索框')
        input_step['send_enter'] = not bool(step.get('search_button_target'))
        self._input_component_target(
            input_step, input_step, step.get('keyword', ''), '商品关键词', 'text'
        )
        self._click_component_target(
            step, step.get('search_button_target'), '搜索按钮', required=False
        )
        result_probe = self._probe_component_target(
            step, step.get('result_target'), '搜索结果', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'keyword': step.get('keyword', ''),
            'result_probe': result_probe,
        })

    def _action_browse_product(self, step: Dict[str, Any]):
        if step.get('product_target'):
            self._click_component_target(step, step.get('product_target'), '浏览商品')
        scroll_count = max(0, int(step.get('scroll_count', 0)))
        if scroll_count:
            self._action_scroll_list({
                **step,
                'count': scroll_count,
                'direction': step.get('direction', 'up'),
            })
        self._sleep_interruptibly(step.get('dwell_time', 3))
        self._store_component_result(step, {
            'completed': True,
            'scroll_count': scroll_count,
            'dwell_time': float(step.get('dwell_time', 3)),
        })

    def _action_open_product_detail(self, step: Dict[str, Any]):
        self._click_component_target(step, step.get('product_target'), '打开商品详情')
        detail_probe = self._probe_component_target(
            step, step.get('detail_target'), '商品详情标识', required=False
        )
        self._store_component_result(step, {'completed': True, 'detail_probe': detail_probe})

    def _click_target_list(self, step: Dict[str, Any], targets: Any, prefix: str) -> int:
        target_list = targets or []
        if not isinstance(target_list, list):
            raise ValueError(f'{prefix}需要数组配置')
        for index, target in enumerate(target_list, 1):
            self._click_component_target(step, target, f'{prefix}{index}')
            self._sleep_interruptibly(step.get('action_interval', 0.2))
        return len(target_list)

    def _action_add_cart(self, step: Dict[str, Any]):
        selected_specs = self._click_target_list(step, step.get('spec_targets'), '选择规格')
        self._click_component_target(step, step.get('add_cart_target'), '加入购物车')
        confirmation = self._probe_component_target(
            step, step.get('confirmation_target'), '加购成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'selected_specs': selected_specs,
            'confirmation': confirmation,
        })

    def _action_create_order(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('product_target'), '选择商品', required=False
        )
        selected_specs = self._click_target_list(step, step.get('spec_targets'), '选择规格')
        self._click_component_target(step, step.get('buy_target'), '立即购买')
        address_fields = step.get('address_fields') or []
        if address_fields:
            self._action_fill_form({
                **step,
                'fields': address_fields,
                'save_as': '',
            })
        self._click_component_target(step, step.get('submit_order_target'), '提交订单')
        success_probe = self._probe_component_target(
            step, step.get('order_success_target'), '订单成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'selected_specs': selected_specs,
            'address_field_count': len(address_fields),
            'success_probe': success_probe,
        })

    def _action_verify_code(self, step: Dict[str, Any]):
        """获取、等待并输入验证码，可独立用于登录、支付和身份认证。"""
        requested = self._click_component_target(
            step, step.get('get_code_target'), '获取验证码', required=False
        )
        wait_seconds = max(0.0, float(step.get('code_wait', step.get('countdown', 0))))
        if wait_seconds:
            self._sleep_interruptibly(wait_seconds)
        code = step.get('verification_code', step.get('code', ''))
        if code in (None, ''):
            raise ValueError('验证码不能为空，可直接填写或使用变量表达式')
        self._input_component_target(
            step,
            step.get('verification_code_target') or step.get('code_target'),
            code,
            '输入验证码',
            'verification_code',
            True,
        )
        submitted = self._click_component_target(
            step, step.get('submit_target'), '提交验证码', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'requested': requested,
            'wait_seconds': wait_seconds,
            'code_length': len(re.sub(r'\D+', '', str(code))),
            'submitted': submitted,
        })

    def _action_identity_verify(self, step: Dict[str, Any]):
        """组合姓名、身份证、银行卡、手机号和人脸入口完成身份认证。"""
        fields = list(step.get('fields') or [])
        standard_fields = (
            ('姓名', 'full_name', 'full_name_target', 'text', False),
            ('身份证', 'id_card', 'id_card_target', 'text', True),
            ('银行卡', 'bank_card', 'bank_card_target', 'bank_card', True),
            ('手机号', 'phone', 'phone_target', 'phone', False),
        )
        for label, value_key, target_key, input_kind, sensitive in standard_fields:
            if step.get(target_key) and step.get(value_key) not in (None, ''):
                fields.append({
                    'name': label,
                    'value': step.get(value_key),
                    'input_kind': input_kind,
                    'sensitive': sensitive,
                    'target': step.get(target_key),
                })
        if not fields and not step.get('face_target'):
            raise ValueError('身份认证至少需要一个认证字段或人脸认证入口')
        if fields:
            self._action_fill_form({**step, 'fields': fields, 'save_as': ''})
        face_started = self._click_component_target(
            step, step.get('face_target'), '开始人脸认证', required=False
        )
        submitted = self._click_component_target(
            step, step.get('submit_target'), '提交身份认证', required=False
        )
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '身份认证成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'field_count': len(fields),
            'face_started': face_started,
            'submitted': submitted,
            'success_probe': success_probe,
        })

    def _action_risk_popup_handle(self, step: Dict[str, Any]):
        """按顺序处理风险提示、协议确认和二次确认。"""
        targets = step.get('targets') or step.get('risk_targets') or []
        confirm_targets = step.get('confirm_targets') or []
        if not isinstance(targets, list) or not isinstance(confirm_targets, list):
            raise ValueError('风险弹窗目标必须为数组')
        handled = []
        attempts = []
        max_handles = max(1, int(step.get('max_handles', len(targets) + len(confirm_targets) or 1)))
        for index, target in enumerate([*targets, *confirm_targets], 1):
            if len(handled) >= max_handles:
                break
            name = str(target.get('name') if isinstance(target, dict) else '') or f'风险确认{index}'
            target_step = self._component_target_step(step, target, name)
            target_step['timeout'] = step.get('probe_timeout', 0.8)
            try:
                self._action_element_probe(target_step)
                self._action_smart_click(target_step)
                handled.append(name)
                self._sleep_interruptibly(step.get('action_interval', 0.2))
            except Exception as exc:
                attempts.append({'name': name, 'error': str(exc)})
        if step.get('required', False) and not handled:
            raise AssertionError(f'未发现风险确认弹窗: {attempts}')
        self._store_component_result(step, {
            'completed': True,
            'handled_count': len(handled),
            'handled': handled,
            'attempts': attempts,
        })

    def _action_payment_flow(self, step: Dict[str, Any]):
        """支付测试流程；默认停在最终确认前，显式授权后才点击真实支付按钮。"""
        self._click_component_target(
            step, step.get('start_target'), '进入支付', required=False
        )
        method_targets = step.get('method_targets') or []
        selected_methods = self._click_target_list(step, method_targets, '选择支付方式')

        if step.get('password_target') and step.get('payment_password') not in (None, ''):
            self._input_component_target(
                step, step.get('password_target'), step.get('payment_password'),
                '输入支付密码', 'password', True,
            )
        verification_used = False
        if step.get('verification_code_target'):
            self._action_verify_code({
                **step,
                'save_as': '',
                'submit_target': {},
            })
            verification_used = True
        face_started = self._click_component_target(
            step, step.get('face_target'), '支付人脸认证', required=False
        )
        bank_jump = self._click_component_target(
            step, step.get('bank_jump_target'), '跳转银行页面', required=False
        )
        allow_real_payment = bool(step.get('allow_real_payment', False))
        submitted = False
        if allow_real_payment:
            submitted = self._click_component_target(
                step, step.get('confirm_payment_target'), '确认支付', required=True
            )
        success_probe = None
        if submitted:
            success_probe = self._probe_component_target(
                step, step.get('success_target'), '支付成功标识', required=False
            )
            if isinstance(step.get('success_page'), dict):
                page_step = dict(step)
                page_step.update(step['success_page'])
                self._wait_for_page(page_step)
        self._store_component_result(step, {
            'completed': True,
            'selected_methods': selected_methods,
            'verification_used': verification_used,
            'face_started': face_started,
            'bank_jump': bank_jump,
            'allow_real_payment': allow_real_payment,
            'submitted': submitted,
            'dry_run': not allow_real_payment,
            'success_probe': success_probe,
        })

    def _action_like_video(self, step: Dict[str, Any]):
        self._click_component_target(step, step.get('target'), '点赞视频')
        self._store_component_result(step, {'completed': True, 'action': 'like'})

    def _action_favorite_video(self, step: Dict[str, Any]):
        self._click_component_target(step, step.get('target'), '收藏视频')
        self._store_component_result(step, {'completed': True, 'action': 'favorite'})

    def _action_follow_user(self, step: Dict[str, Any]):
        self._click_component_target(step, step.get('target'), '关注用户')
        self._store_component_result(step, {'completed': True, 'action': 'follow'})

    def _action_comment_video(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('comment_entry_target'), '打开评论区', required=False
        )
        self._input_component_target(
            step, step.get('comment_input_target'), step.get('comment', ''),
            '输入评论', 'text'
        )
        self._click_component_target(step, step.get('submit_target'), '发布评论')
        self._store_component_result(step, {
            'completed': True,
            'action': 'comment',
            'comment_length': len(str(step.get('comment') or '')),
        })

    def _push_media_to_device(
        self,
        media_path: Any,
        media_kind: str,
        remote_dir: Any = '',
        required: bool = False,
    ) -> Dict[str, Any]:
        """把本地媒体推送到 Android 相册目录；iOS 可使用预置相册目标。"""
        local_path = os.path.abspath(os.path.expanduser(str(media_path or '').strip()))
        if not local_path:
            return {'requested': False, 'pushed': False}
        if not os.path.isfile(local_path):
            raise ValueError(f'媒体文件不存在: {local_path}')
        if self.platform != 'android' or not G.DEVICE:
            if required:
                raise RuntimeError('当前设备不支持自动推送媒体，请预先导入相册并配置媒体目标')
            return {
                'requested': True,
                'pushed': False,
                'local_path': local_path,
                'reason': 'preloaded-media-required',
            }

        target_dir = str(remote_dir or '/sdcard/Download/TestHub').strip()
        if not re.fullmatch(r'/[A-Za-z0-9._/-]+', target_dir):
            raise ValueError('远程媒体目录包含不安全字符')
        filename = self._safe_filename(os.path.basename(local_path))
        extension = os.path.splitext(local_path)[1].lower()
        if extension and not filename.lower().endswith(extension):
            filename += extension
        remote_path = f"{target_dir.rstrip('/')}/{filename}"
        G.DEVICE.shell(f'mkdir -p {target_dir}')
        G.DEVICE.push(local_path, remote_path)
        try:
            G.DEVICE.shell(
                'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE '
                f'-d file://{remote_path}'
            )
        except Exception as exc:
            logger.debug('刷新 Android 媒体库失败: %s', exc)
        return {
            'requested': True,
            'pushed': True,
            'kind': media_kind,
            'local_path': local_path,
            'remote_path': remote_path,
        }

    def _action_upload_video(self, step: Dict[str, Any]):
        media_result = self._push_media_to_device(
            step.get('media_path'),
            'video',
            step.get('remote_dir'),
            bool(step.get('require_push', False)),
        )
        self._click_component_target(
            step, step.get('entry_target'), '进入视频发布', required=False
        )
        self._click_component_target(
            step, step.get('album_target'), '打开视频相册', required=False
        )
        self._click_component_target(step, step.get('media_target'), '选择视频')
        edit_count = self._click_target_list(step, step.get('edit_targets'), '视频编辑步骤')
        caption = str(step.get('caption') or '')
        if caption and step.get('caption_target'):
            self._input_component_target(
                step, step.get('caption_target'), caption, '输入视频文案', 'text'
            )
        published = self._click_component_target(
            step, step.get('publish_target'), '发布视频'
        )
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '视频发布成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'media': media_result,
            'edit_step_count': edit_count,
            'caption_length': len(caption),
            'published': published,
            'success_probe': success_probe,
        })

    def _action_search_user(self, step: Dict[str, Any]):
        keyword = step.get('keyword', '')
        self._input_component_target(
            step, step.get('search_target'), keyword, '输入用户关键词', 'text'
        )
        self._click_component_target(
            step, step.get('search_button_target'), '搜索用户', required=False
        )
        result_probe = self._probe_component_target(
            step, step.get('result_target'), '用户搜索结果', required=False
        )
        opened = self._click_component_target(
            step, step.get('user_target'), '打开用户', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'keyword': keyword,
            'result_probe': result_probe,
            'opened': opened,
        })

    def _action_send_message(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('conversation_target'), '打开会话', required=False
        )
        message = str(step.get('message') or '')
        if not message and not step.get('attachment_target'):
            raise ValueError('发送消息至少需要文本或附件目标')
        if message:
            self._input_component_target(
                step, step.get('message_input_target'), message, '输入消息', 'text'
            )
        attachment_added = self._click_component_target(
            step, step.get('attachment_target'), '添加消息附件', required=False
        )
        sent = self._click_component_target(step, step.get('send_target'), '发送消息')
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '消息发送成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'message_length': len(message),
            'attachment_added': attachment_added,
            'sent': sent,
            'success_probe': success_probe,
        })

    def _action_upload_image(self, step: Dict[str, Any]):
        media_result = self._push_media_to_device(
            step.get('media_path'),
            'image',
            step.get('remote_dir'),
            bool(step.get('require_push', False)),
        )
        self._click_component_target(
            step, step.get('entry_target'), '打开图片选择器', required=False
        )
        self._click_component_target(step, step.get('media_target'), '选择图片')
        confirmed = self._click_component_target(
            step, step.get('confirm_target'), '确认选择图片', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'media': media_result,
            'confirmed': confirmed,
        })

    def _action_publish_post(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('entry_target'), '进入发布页面', required=False
        )
        attachment_count = self._click_target_list(
            step, step.get('attachment_targets'), '添加帖子媒体'
        )
        content = str(step.get('content') or '')
        if content and step.get('content_target'):
            self._input_component_target(
                step, step.get('content_target'), content, '输入帖子内容', 'text'
            )
        option_count = self._click_target_list(
            step, step.get('option_targets'), '设置帖子选项'
        )
        published = self._click_component_target(
            step, step.get('publish_target'), '发布帖子'
        )
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '帖子发布成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'content_length': len(content),
            'attachment_count': attachment_count,
            'option_count': option_count,
            'published': published,
            'success_probe': success_probe,
        })

    def _action_share_content(self, step: Dict[str, Any]):
        self._click_component_target(step, step.get('share_target'), '打开分享面板')
        self._click_component_target(
            step, step.get('channel_target'), '选择分享渠道', required=False
        )
        self._click_component_target(
            step, step.get('recipient_target'), '选择分享对象', required=False
        )
        confirmed = self._click_component_target(
            step, step.get('confirm_target'), '确认分享'
        )
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '分享成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'confirmed': confirmed,
            'success_probe': success_probe,
        })

    def _action_comment(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('comment_entry_target'), '打开评论区', required=False
        )
        comment = str(step.get('comment') or '')
        if not comment:
            raise ValueError('评论内容不能为空')
        self._input_component_target(
            step, step.get('comment_input_target'), comment, '输入评论', 'text'
        )
        submitted = self._click_component_target(
            step, step.get('submit_target'), '发布评论'
        )
        success_probe = self._probe_component_target(
            step, step.get('success_target'), '评论发布成功标识', required=False
        )
        self._store_component_result(step, {
            'completed': True,
            'comment_length': len(comment),
            'submitted': submitted,
            'success_probe': success_probe,
        })

    def _action_live_room(self, step: Dict[str, Any]):
        self._click_component_target(
            step, step.get('room_target'), '进入直播间', required=False
        )
        like_count = max(0, int(step.get('like_count', 0)))
        for _ in range(like_count):
            self._click_component_target(step, step.get('like_target'), '直播点赞')
            self._sleep_interruptibly(step.get('like_interval', 0.15))
        danmaku = str(step.get('danmaku') or '')
        if danmaku:
            self._input_component_target(
                step, step.get('danmaku_input_target'), danmaku, '输入弹幕', 'text'
            )
            self._click_component_target(step, step.get('danmaku_submit_target'), '发送弹幕')
        self._sleep_interruptibly(step.get('dwell_time', 5))
        self._store_component_result(step, {
            'completed': True,
            'like_count': like_count,
            'danmaku_sent': bool(danmaku),
        })

    def _license_plate_step_context(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """合并步骤与元素配置，用于识别车牌控件及其原生输入能力。"""
        config = dict(step or {})
        element = None
        element_id = step.get('element_id')
        if element_id:
            element = self._load_element(element_id)
            if element:
                element_config = dict(element.config or {})
                element_config.update({
                    key: value for key, value in config.items()
                    if value not in (None, '', [], {})
                })
                config = element_config
        if element:
            config['_element_name'] = element.name
            config['_element_type'] = element.element_type
        return config

    def _is_license_plate_trigger(self, step: Dict[str, Any]) -> bool:
        """识别点击车牌分格输入区域的步骤，避免把普通键盘字符误判为入口。"""
        config = self._license_plate_step_context(step)
        fingerprint = config.get('fingerprint') or {}
        class_name = str(
            config.get('class_name') or fingerprint.get('class_name') or ''
        ).casefold()
        identity_values = [
            step.get('selector'), config.get('resource_id'),
            config.get('accessibility_id'), config.get('text'),
            config.get('ocr_text'), fingerprint.get('resource_id'),
            fingerprint.get('content_desc'), fingerprint.get('text'),
        ]
        normalized_identity_values = {
            re.sub(r'\s+', '', str(value or '')).casefold()
            for value in identity_values
            if value not in (None, '')
        }
        keyboard_controls = {
            '确认', '确定', '完成', '取消', '关闭', '收起',
            'confirm', 'done', 'cancel', 'close',
        }
        if normalized_identity_values & keyboard_controls:
            return False

        hints = ' '.join(str(value or '') for value in (
            step.get('name'), step.get('description'), step.get('selector'),
            config.get('_element_name'), config.get('resource_id'),
            config.get('accessibility_id'), config.get('text'), config.get('ocr_text'),
            fingerprint.get('resource_id'), fingerprint.get('content_desc'),
            fingerprint.get('text'),
        ))
        normalized = re.sub(r'\s+', '', hints).casefold()
        if any(
            normalized.endswith(control)
            for control in keyboard_controls
        ) and any(token in class_name for token in ('button', 'statictext', 'key')):
            return False
        return '车牌' in normalized or 'licenseplate' in normalized

    def _license_plate_target_is_native_text(self, step: Dict[str, Any]) -> bool:
        """标准 TextField 继续使用 send_keys，自定义分格控件改走虚拟键盘。"""
        config = self._license_plate_step_context(step)
        fingerprint = config.get('fingerprint') or {}
        class_name = str(
            config.get('class_name') or fingerprint.get('class_name') or ''
        )
        return self._is_text_input_node({'class_name': class_name})

    def _license_plate_key_character(self, step: Dict[str, Any]) -> str:
        config = self._license_plate_step_context(step)
        fingerprint = config.get('fingerprint') or {}
        province_characters = set(
            ''.join(''.join(row) for row in LICENSE_PLATE_PROVINCE_LAYOUT)
        ) | set(LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS)
        alphanumeric_characters = set(
            ''.join(row for row, _offset in LICENSE_PLATE_ALPHANUMERIC_LAYOUT)
        )
        key_characters = province_characters | alphanumeric_characters
        for value in (
            config.get('_element_name'), config.get('resource_id'),
            config.get('accessibility_id'), config.get('text'),
            fingerprint.get('resource_id'), fingerprint.get('content_desc'),
            fingerprint.get('text'), step.get('selector'),
        ):
            candidate = re.sub(r'\s+', '', str(value or '')).upper()
            if len(candidate) == 1 and candidate in key_characters:
                return candidate
        return ''

    def _recorded_license_plate_key_character(self, step: Dict[str, Any]) -> str:
        if self.platform != 'ios':
            return ''
        if not self._is_generated_recording_click_name(step.get('name')):
            return ''
        character = self._license_plate_key_character(step)
        if not character:
            return ''
        config = self._license_plate_step_context(step)
        fingerprint = config.get('fingerprint') or {}
        parent = fingerprint.get('parent') if isinstance(fingerprint, dict) else {}
        parent_values = []
        if isinstance(parent, dict):
            parent_values.extend(parent.get(key) for key in ('resource_id', 'text', 'content_desc'))
        parent_text = re.sub(r'\s+', '', ' '.join(str(value or '') for value in parent_values))
        class_name = str(fingerprint.get('class_name') or config.get('class_name') or '')
        if '网页对话框' in parent_text or 'StaticText' in class_name:
            return character
        return ''

    def _action_recorded_license_plate_key(self, step: Dict[str, Any]) -> bool:
        character = self._recorded_license_plate_key_character(step)
        if not character:
            return False

        nodes = self._dump_current_ui_nodes()
        phase = self._license_plate_keyboard_phase(nodes)
        open_result = None
        if phase == 'closed':
            open_result = self._open_license_plate_keyboard(step)
            self._sleep_interruptibly(step.get('license_plate_open_wait_after', 0.2))
            nodes = self._dump_current_ui_nodes()
            phase = self._license_plate_keyboard_phase(nodes)

        province_characters = set(
            ''.join(''.join(row) for row in LICENSE_PLATE_PROVINCE_LAYOUT)
        ) | set(LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS)
        if character in province_characters and phase == 'alphanumeric':
            diagnostics = {
                'skipped': True,
                'reason': 'duplicate-province-after-keyboard-switched',
                'character': character,
                'keyboard_phase': phase,
            }
            self._last_locator_diagnostics = {
                'step': step.get('name', step.get('type', '点击')),
                'selected_strategy': 'license_plate_recorded_key',
                'license_plate_key': diagnostics,
                'keyboard_open': open_result,
            }
            logger.info('跳过录制车牌省份按键: %s（键盘已切换为英文/数字）', character)
            return True

        if phase == 'closed':
            raise RuntimeError(f"录制车牌按键 '{character}' 回放失败: 车牌键盘未打开")

        target, strategy, attempts = self._find_license_plate_key(character, phase, step)
        diagnostics = {
            'character': character,
            'keyboard_phase': phase,
            'strategy': strategy,
            'target': list(target) if target else None,
            'attempts': attempts,
        }
        if open_result is not None:
            diagnostics['keyboard_open'] = open_result
        self._last_locator_diagnostics = {
            'step': step.get('name', step.get('type', '点击')),
            'selected_strategy': 'license_plate_recorded_key',
            'license_plate_key': diagnostics,
        }
        if not target:
            raise RuntimeError(f"录制车牌按键 '{character}' 回放失败: 未定位到键盘按键")

        logger.info(
            '录制车牌按键回放: character=%s phase=%s strategy=%s target=%s',
            character, phase, strategy, target,
        )
        touch(target)
        self._sleep_interruptibly(step.get('license_plate_key_wait_after', 0.15))
        return True

    def _license_plate_keyboard_phase(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        if not self._license_plate_keyboard_visible(nodes):
            return 'closed'
        values = set().union(*(self._node_values(node) for node in nodes)) if nodes else set()
        province_values = set(
            ''.join(''.join(row) for row in LICENSE_PLATE_PROVINCE_LAYOUT)
        ) | set(LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS)
        alphanumeric_values = set(
            ''.join(row for row, _offset in LICENSE_PLATE_ALPHANUMERIC_LAYOUT)
        )
        if len(values & province_values) >= 5:
            return 'province'
        if len(values & alphanumeric_values) >= 10:
            return 'alphanumeric'
        return 'unknown'

    def _skip_duplicate_license_plate_province(self, step: Dict[str, Any]) -> bool:
        """车牌首位完成后忽略重复省份重试，防止坐标兜底误点英文键盘。"""
        character = self._license_plate_key_character(step)
        if not character:
            return False
        nodes = self._dump_current_ui_nodes()
        if self._license_plate_keyboard_phase(nodes) != 'alphanumeric':
            return False
        diagnostics = {
            'skipped': True,
            'reason': 'duplicate-province-after-keyboard-switched',
            'character': character,
            'keyboard_phase': 'alphanumeric',
        }
        self._last_locator_diagnostics = {
            'step': step.get('name', step.get('type', '点击')),
            'selected_strategy': 'license_plate_duplicate_guard',
            'license_plate_key': diagnostics,
        }
        logger.info('跳过重复车牌省份按键: %s（键盘已切换为英文/数字）', character)
        return True

    def _dismiss_ios_system_keyboard(self, context: str = '车牌控件') -> Dict[str, Any]:
        """通过 WDA 收起 iOS 系统键盘，避免遮住 H5 自定义车牌控件。"""
        if self.platform != 'ios':
            return {'dismissed': False, 'reason': 'not-ios'}
        try:
            device = G.DEVICE
            driver = getattr(device, 'driver', None)
            session_http = getattr(driver, '_session_http', None)
            if session_http is None:
                return {'dismissed': False, 'reason': 'wda-session-http-unavailable'}
            session_http.post('/wda/keyboard/dismiss')
            logger.info('已尝试收起 iOS 系统键盘，准备点击%s', context)
            return {'dismissed': True, 'method': 'wda_keyboard_dismiss'}
        except Exception as exc:
            # 没有系统键盘时部分 WDA 版本会返回错误，不应阻断后续车牌点击。
            logger.debug('收起 iOS 系统键盘失败，继续尝试车牌控件: %s', exc)
            return {'dismissed': False, 'reason': str(exc)}

    @staticmethod
    def _node_values(node: Dict[str, Any]) -> set:
        return {
            re.sub(r'\s+', '', str(node.get(key) or '')).upper()
            for key in ('resource_id', 'text', 'content_desc')
            if node.get(key) not in (None, '')
        }

    def _license_plate_keyboard_visible(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        visible_nodes = [
            node for node in nodes
            if node.get('visible', True) is not False
        ]
        if not visible_nodes:
            return False
        width, height = parse_resolution(self._get_current_resolution())
        if width <= 0 or height <= 0:
            return False

        def is_keyboard_zone(node: Dict[str, Any]) -> bool:
            bounds = self._node_bounds_tuple(node)
            center = self._node_center_tuple(node)
            if not bounds or not center:
                return False
            _x1, y1, _x2, y2 = bounds
            _cx, cy = center
            return (
                cy >= height * 0.55
                or y1 >= height * 0.52
                or y2 >= height * 0.60
            )

        keyboard_nodes = [
            node for node in visible_nodes
            if is_keyboard_zone(node)
        ]
        keyboard_values = (
            set().union(*(self._node_values(node) for node in keyboard_nodes))
            if keyboard_nodes else set()
        )
        if '车牌号键盘' in keyboard_values:
            return True
        key_values = set(''.join(''.join(row) for row in LICENSE_PLATE_PROVINCE_LAYOUT)) | set(
            ''.join(row for row, _offset in LICENSE_PLATE_ALPHANUMERIC_LAYOUT)
        )
        return (
            len(keyboard_values & key_values) >= 12
            and bool(keyboard_values & {'确认', '取消', '完成'})
        )

    def _detected_license_plate_keyboard_region(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[tuple]:
        """根据当前虚拟键盘节点动态计算区域，兼容 Android/iOS 不同弹出高度。"""
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        if not self._license_plate_keyboard_visible(nodes):
            return None
        key_values = set(''.join(''.join(row) for row in LICENSE_PLATE_PROVINCE_LAYOUT)) | set(
            ''.join(row for row, _offset in LICENSE_PLATE_ALPHANUMERIC_LAYOUT)
        ) | set(LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS) | {
            '确认', '取消', '完成', '确定', '车牌号键盘', '中', '英',
        }
        bounds_list = []
        for node in nodes:
            if not (self._node_values(node) & key_values):
                continue
            bounds = node.get('bounds') or {}
            try:
                bounds_list.append((
                    int(bounds['x1']), int(bounds['y1']),
                    int(bounds['x2']), int(bounds['y2']),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if len(bounds_list) < 5:
            return None
        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            return None
        top = max(0, min(item[1] for item in bounds_list) - int(height * 0.04))
        bottom = min(height, max(item[3] for item in bounds_list) + int(height * 0.06))
        return 0, top, width, bottom

    def _license_plate_input_container_target(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple:
        """把“新能源”文字节点修正为整行输入框内的首格，提升 iOS 点击可靠性。"""
        nodes = nodes if nodes is not None else self._dump_current_ui_nodes()
        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            return None, {'matched': False, 'reason': 'invalid-resolution'}

        markers = [
            node for node in nodes
            if '新能源' in self._node_values(node)
        ]
        containing = []
        for marker in markers:
            marker_bounds = marker.get('bounds') or {}
            try:
                marker_x = (float(marker_bounds['x1']) + float(marker_bounds['x2'])) / 2
                marker_y = (float(marker_bounds['y1']) + float(marker_bounds['y2'])) / 2
            except (KeyError, TypeError, ValueError):
                continue
            for node in nodes:
                bounds = node.get('bounds') or {}
                try:
                    x1, y1 = float(bounds['x1']), float(bounds['y1'])
                    x2, y2 = float(bounds['x2']), float(bounds['y2'])
                except (KeyError, TypeError, ValueError):
                    continue
                node_width, node_height = x2 - x1, y2 - y1
                if not (x1 <= marker_x <= x2 and y1 <= marker_y <= y2):
                    continue
                if node_width < width * 0.45 or not (height * 0.035 <= node_height <= height * 0.20):
                    continue
                containing.append((node_width * node_height, (x1, y1, x2, y2), node))
        if containing:
            containing.sort(key=lambda item: item[0])
            _area, (x1, y1, x2, y2), node = containing[0]
            target = (
                int(round(x1 + (x2 - x1) * 0.08)),
                int(round((y1 + y2) / 2)),
            )
            return target, {
                'matched': True,
                'method': 'container-first-cell',
                'container_bounds': [int(x1), int(y1), int(x2), int(y2)],
                'container_class': node.get('class_name'),
            }

        labels = [node for node in nodes if '车牌号' in self._node_values(node)]
        if labels:
            bounds = labels[0].get('bounds') or {}
            try:
                target = (
                    int(width * 0.14),
                    min(height - 1, int(bounds['y2']) + int(height * 0.035)),
                )
                return target, {'matched': True, 'method': 'label-offset'}
            except (KeyError, TypeError, ValueError):
                pass
        return None, {'matched': False, 'reason': 'container-not-found'}

    def _wait_for_license_plate_keyboard(self, timeout: Any = 2.0) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            self.ensure_not_stopped()
            if self._license_plate_keyboard_visible():
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep_interruptibly(min(0.2, max(0.0, deadline - time.monotonic())))

    def _open_license_plate_keyboard(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """可靠打开车牌虚拟键盘；iOS 会先收起系统键盘并修正点击区域。"""
        dismiss_result = self._dismiss_ios_system_keyboard()
        if self.platform == 'ios':
            self._sleep_interruptibly(step.get('keyboard_dismiss_wait', 0.2))

        resolved_target = self._resolve_action_target(step)
        container_target, container_detail = self._license_plate_input_container_target()
        candidates = []
        if self.platform == 'ios' and container_target:
            candidates.append(('container', container_target))
        candidates.append(('resolved', resolved_target))
        if self.platform != 'ios' and container_target:
            candidates.append(('container', container_target))

        attempts = []
        seen = set()
        for method, target in candidates:
            normalized_target = tuple(target) if isinstance(target, (list, tuple)) else target
            if not normalized_target or str(normalized_target) in seen:
                continue
            seen.add(str(normalized_target))
            touch(normalized_target)
            opened = self._wait_for_license_plate_keyboard(
                step.get('keyboard_open_timeout', 2.0)
            )
            attempts.append({
                'method': method,
                'target': list(normalized_target),
                'opened': opened,
            })
            if opened:
                logger.info('车牌键盘已打开: method=%s target=%s', method, normalized_target)
                return {
                    'opened': True,
                    'method': method,
                    'target': list(normalized_target),
                    'dismiss_system_keyboard': dismiss_result,
                    'container': container_detail,
                    'attempts': attempts,
                }

        diagnostics = {
            'opened': False,
            'dismiss_system_keyboard': dismiss_result,
            'container': container_detail,
            'attempts': attempts,
        }
        self._last_locator_diagnostics = {
            **(self._last_locator_diagnostics or {}),
            'license_plate_keyboard': diagnostics,
        }
        raise RuntimeError('车牌输入框已点击，但虚拟键盘未打开，请重新采集车牌输入区域')

    @staticmethod
    def _normalize_license_plate(value: Any) -> str:
        """标准化并校验常见 7/8 位中国大陆车牌。"""
        plate = re.sub(r'\s+', '', str(value or '')).upper()
        if len(plate) not in (7, 8):
            raise ValueError('车牌号必须为 7 位普通车牌或 8 位新能源车牌')
        if not re.fullmatch(r'[\u4e00-\u9fff]', plate[0]):
            raise ValueError('车牌号首位必须为省份汉字')
        if not re.fullmatch(r'[A-Z]', plate[1]):
            raise ValueError('车牌号第二位必须为大写英文字母')
        for char in plate[2:]:
            if not (re.fullmatch(r'[A-Z0-9]', char) or char in LICENSE_PLATE_SPECIAL_TAIL):
                raise ValueError(f'车牌号包含不支持的字符: {char}')
        return plate

    def _license_plate_keyboard_region(self, step: Dict[str, Any]) -> tuple:
        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            raise RuntimeError('无法读取设备分辨率，不能定位车牌键盘')
        detected = self._detected_license_plate_keyboard_region()
        if detected:
            return detected
        configured = step.get('keyboard_region') or (
            [0.0, 0.55, 1.0, 1.0]
            if self.platform == 'ios'
            else [0.0, 0.70, 1.0, 0.98]
        )
        return self._resolve_video_region(configured, width, height)

    @staticmethod
    def _point_in_region(point: Any, region: tuple) -> bool:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return False
        x1, y1, x2, y2 = region
        return x1 <= float(point[0]) <= x2 and y1 <= float(point[1]) <= y2

    def _semantic_text_target(self, expected: str, region: tuple):
        """在键盘区域内优先选择可点击、面积更小的精确文本节点。"""
        candidates = []
        for node in self._dump_current_ui_nodes():
            center = node.get('center') or {}
            point = (center.get('x'), center.get('y'))
            if point[0] is None or point[1] is None or not self._point_in_region(point, region):
                continue
            values = {
                re.sub(r'\s+', '', str(node.get(key) or '')).upper()
                for key in ('text', 'content_desc')
            }
            resource_tail = str(node.get('resource_id') or '').rsplit('/', 1)[-1].upper()
            values.add(resource_tail)
            if expected.upper() not in values:
                continue
            bounds = node.get('bounds') or {}
            area = max(1, int(bounds.get('x2', 0)) - int(bounds.get('x1', 0))) * max(
                1, int(bounds.get('y2', 0)) - int(bounds.get('y1', 0))
            )
            candidates.append((
                0 if node.get('clickable') else 1,
                0 if 'button' in str(node.get('class_name') or '').lower() else 1,
                area,
                -int(point[1]),
                (int(point[0]), int(point[1])),
                node,
            ))
        if not candidates:
            return None, {'matched': False, 'candidate_count': 0}
        candidates.sort(key=lambda item: item[:4])
        selected = candidates[0]
        return selected[4], {
            'matched': True,
            'candidate_count': len(candidates),
            'selected_node': selected[5],
        }

    def _configured_license_plate_key_target(self, char: str, step: Dict[str, Any]):
        key_positions = step.get('key_positions') or {}
        if not isinstance(key_positions, dict):
            return None
        value = key_positions.get(char)
        if value is None:
            value = key_positions.get(char.upper())
        if isinstance(value, dict):
            x, y = value.get('x'), value.get('y')
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            x, y = value[0], value[1]
        else:
            return None
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            return None
        width, height = self._get_current_resolution()
        if width <= 0 or height <= 0:
            return None
        if abs(x) <= 1 and abs(y) <= 1:
            x, y = x * width, y * height
        return int(round(x)), int(round(y))

    def _coordinate_license_plate_key_target(
        self,
        char: str,
        phase: str,
        region: tuple,
        step: Dict[str, Any],
    ):
        """按常见车牌键盘布局提供最后一级归一化坐标兜底。"""
        configured = self._configured_license_plate_key_target(char, step)
        if configured:
            return configured, {'source': 'key_positions'}

        x1, y1, x2, y2 = region
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        row = column = None

        if phase == 'province':
            for row_index, row_chars in enumerate(LICENSE_PLATE_PROVINCE_LAYOUT):
                if char in row_chars:
                    row, column = row_index, row_chars.index(char)
                    break
            if row is None and char in LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS:
                row, column = 3, LICENSE_PLATE_PROVINCE_SPECIAL_COLUMNS[char]
        else:
            for row_index, (row_chars, offset) in enumerate(LICENSE_PLATE_ALPHANUMERIC_LAYOUT):
                if char in row_chars:
                    row = row_index
                    column = offset + row_chars.index(char)
                    break

        if row is None or column is None:
            return None, {'source': 'standard_layout', 'reason': 'character-not-mapped'}
        target = (
            int(round(x1 + width * ((float(column) + 0.5) / 10.0))),
            int(round(y1 + height * ((float(row) + 0.5) / 4.0))),
        )
        return target, {
            'source': 'standard_layout',
            'phase': phase,
            'row': row,
            'column': column,
            'region': list(region),
        }

    def _find_license_plate_key(
        self,
        char: str,
        phase: str,
        step: Dict[str, Any],
    ):
        region = self._license_plate_keyboard_region(step)
        raw_strategies = step.get('key_strategies') or ['semantic', 'ocr', 'coordinate']
        if isinstance(raw_strategies, str):
            raw_strategies = [item.strip() for item in raw_strategies.split(',') if item.strip()]
        strategies = [str(item).strip().lower() for item in raw_strategies]
        attempts = []

        deadline = time.monotonic() + max(0.1, float(step.get('key_timeout', 3)))
        while True:
            for strategy in strategies:
                target = None
                detail = {}
                if strategy == 'semantic':
                    target, detail = self._semantic_text_target(char, region)
                elif strategy == 'ocr':
                    try:
                        target, detail = self._ocr_target({
                            'text': char,
                            'match_mode': 'exact',
                            'min_confidence': float(step.get('ocr_min_confidence', 0.35)),
                            'languages': ['ch_sim', 'en'],
                            'region': region,
                        })
                    except Exception as exc:
                        detail = {'matched': False, 'error': str(exc)}
                elif strategy == 'coordinate':
                    if step.get('coordinate_fallback', True):
                        target, detail = self._coordinate_license_plate_key_target(
                            char, phase, region, step
                        )
                    else:
                        detail = {'matched': False, 'reason': 'coordinate-fallback-disabled'}
                else:
                    detail = {'matched': False, 'reason': 'unsupported-strategy'}
                attempts.append({
                    'strategy': strategy,
                    'matched': bool(target),
                    'detail': detail,
                })
                if target:
                    return target, strategy, attempts

            if 'coordinate' in strategies or time.monotonic() >= deadline:
                break
            self._sleep_interruptibly(min(0.2, max(0.0, deadline - time.monotonic())))

        return None, None, attempts

    def _detect_semantic_page(
        self,
        step: Optional[Dict[str, Any]] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """使用 UI 树文本、Activity 和可配置规则识别业务页面。"""
        step = step or {}
        state = state or {}
        nodes = self._last_page_nodes or self._dump_current_ui_nodes()
        text_items = []
        for node in nodes:
            for key in ('text', 'content_desc', 'resource_id'):
                value = str(node.get(key) or '').strip()
                if value:
                    text_items.append(value)
        haystack = ' '.join(text_items + [
            str(state.get('focus') or ''),
            str(state.get('package') or ''),
            str(state.get('activity') or ''),
        ]).casefold()

        configured_rules = step.get('page_rules')
        rules = configured_rules if isinstance(configured_rules, list) and configured_rules else DEFAULT_SEMANTIC_PAGE_RULES
        matches = []
        for raw_rule in rules:
            if not isinstance(raw_rule, dict):
                continue
            name = str(raw_rule.get('name') or raw_rule.get('page_name') or '').strip()
            if not name:
                continue
            keywords = raw_rule.get('keywords') or raw_rule.get('texts') or []
            if isinstance(keywords, str):
                keywords = [item.strip() for item in re.split(r'[,，\n]', keywords) if item.strip()]
            activity_patterns = raw_rule.get('activities') or raw_rule.get('activity_patterns') or []
            if isinstance(activity_patterns, str):
                activity_patterns = [activity_patterns]
            evidence = []
            score = 0
            for keyword in keywords:
                normalized = str(keyword or '').strip().casefold()
                if normalized and normalized in haystack:
                    score += 1
                    evidence.append(str(keyword))
            activity_text = str(state.get('activity') or state.get('focus') or '').casefold()
            for pattern in activity_patterns:
                normalized = str(pattern or '').strip().casefold()
                if normalized and normalized in activity_text:
                    score += 2
                    evidence.append(str(pattern))
            min_score = max(1, int(raw_rule.get('min_score', 1)))
            if score >= min_score:
                matches.append({
                    'name': name,
                    'label': str(raw_rule.get('label') or name),
                    'score': score,
                    'min_score': min_score,
                    'evidence': evidence,
                })

        matches.sort(key=lambda item: (item['score'], len(item['evidence'])), reverse=True)
        if not matches:
            return {
                'page_name': 'unknown',
                'page_label': '未知页面',
                'page_confidence': 0,
                'page_evidence': [],
                'page_candidates': [],
            }
        best = matches[0]
        confidence = min(1.0, best['score'] / max(best['min_score'] + 1, 3))
        return {
            'page_name': best['name'],
            'page_label': best['label'],
            'page_confidence': round(confidence, 3),
            'page_evidence': best['evidence'],
            'page_candidates': matches[:5],
        }

    def _detect_page_context(self) -> Dict[str, Any]:
        """记录当前原生/WebView 页面特征，便于执行报告诊断混合应用。"""
        device_class = G.DEVICE.__class__.__name__.lower()
        if device_class == 'ios':
            self._last_page_nodes = self._dump_current_ui_nodes()
            context_type = 'native'
            contexts = []
            if self._appium_webview is not None:
                try:
                    contexts = self._appium_webview.contexts()
                    current = str(getattr(self._appium_webview, 'current_context', '') or '')
                    if current.upper().startswith('WEBVIEW'):
                        context_type = 'webview'
                except Exception as exc:
                    logger.debug('读取 iOS Appium contexts 失败: %s', exc)
            return {
                'platform': 'ios',
                'type': context_type,
                'focus': '',
                'webview_count': len([
                    item for item in contexts if str(item).upper().startswith('WEBVIEW')
                ]),
            }

        focus = ''
        try:
            output = G.DEVICE.shell(
                "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'"
            )
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            focus = str(output or '').strip()
        except Exception as exc:
            logger.debug(f'检测当前 Activity 失败: {exc}')

        nodes = self._dump_current_ui_nodes()
        self._last_page_nodes = nodes
        webview_nodes = [
            node for node in nodes
            if 'webview' in str(node.get('class_name') or '').lower()
        ]
        return {
            'platform': 'android',
            'type': 'webview' if webview_nodes else 'native',
            'focus': focus,
            'webview_count': len(webview_nodes),
        }

    def _close_license_plate_keyboard(self, step: Dict[str, Any]) -> Dict[str, Any]:
        confirm_texts = step.get('confirm_texts') or ['确认', '完成', '确定']
        if isinstance(confirm_texts, str):
            confirm_texts = [item.strip() for item in confirm_texts.split(',') if item.strip()]
        key_region = self._license_plate_keyboard_region(step)
        width, height = self._get_current_resolution()
        region = (
            0,
            max(0, key_region[1] - int(height * 0.12)),
            width,
            key_region[3],
        )
        for text_value in confirm_texts:
            target, detail = self._semantic_text_target(str(text_value), region)
            if not target:
                try:
                    target, detail = self._ocr_target({
                        'text': str(text_value),
                        'match_mode': 'exact',
                        'min_confidence': float(step.get('ocr_min_confidence', 0.35)),
                        'languages': ['ch_sim', 'en'],
                        'region': region,
                    })
                except Exception as exc:
                    detail = {'matched': False, 'error': str(exc)}
            if target:
                touch(target)
                return {'method': 'confirm_text', 'text': str(text_value), 'detail': detail}

        if self.platform == 'ios':
            try:
                outside_target = (
                    width // 2,
                    max(20, key_region[1] - int(height * 0.08)),
                )
                touch(outside_target)
                return {'method': 'overlay_tap', 'target': list(outside_target)}
            except Exception as exc:
                logger.warning(f'点击遮罩关闭 iOS 车牌键盘失败: {exc}')
                return {'method': 'failed', 'error': str(exc)}

        try:
            from airtest.core.api import keyevent
            keyevent('BACK')
            return {'method': 'back_key'}
        except Exception as exc:
            logger.warning(f'关闭车牌键盘失败: {exc}')
            return {'method': 'failed', 'error': str(exc)}

    def _action_license_plate_input(self, step: Dict[str, Any]):
        """业务组件：自动完成省份、字母、数字输入并关闭车牌键盘。"""
        from apps.core.variable_resolver import resolve_variables

        plate = self._normalize_license_plate(resolve_variables(step.get('plate', '')))
        has_selector = bool(step.get('element_id') or step.get('selector'))
        if not has_selector:
            raise ValueError('车牌输入组件必须关联车牌输入框智能元素或配置定位值')

        page_context = self._detect_page_context()
        if self.platform == 'ios':
            open_result = self._open_license_plate_keyboard(step)
            input_target = open_result.get('target')
        else:
            webview_click = self._try_webview_action(step, 'click')
            if webview_click:
                input_target = {
                    'type': 'webview_dom',
                    'context': webview_click.get('context'),
                    'strategy': (
                        webview_click.get('action_detail') or {}
                    ).get('selected_strategy'),
                }
            else:
                input_target = self._resolve_action_target(step)
                touch(input_target)
            self._sleep_interruptibly(0.3)

        key_interval = max(0.0, float(step.get('key_interval', 0.15)))
        key_results = []
        for index, char in enumerate(plate):
            self.ensure_not_stopped()
            phase = 'province' if index == 0 else 'alphanumeric'
            target, strategy, attempts = self._find_license_plate_key(char, phase, step)
            if not target:
                self._last_locator_diagnostics = {
                    'step': step.get('name', '车牌输入'),
                    'plate': plate,
                    'failed_character': char,
                    'failed_index': index,
                    'page_context': page_context,
                    'key_results': key_results,
                    'attempts': attempts,
                }
                raise ValueError(
                    f"车牌输入失败：未找到第 {index + 1} 位字符 '{char}'，"
                    f"已尝试 {[item.get('strategy') for item in attempts]}"
                )
            touch(target)
            key_results.append({
                'index': index,
                'character': char,
                'phase': phase,
                'strategy': strategy,
                'target': list(target),
                'attempts': attempts,
            })
            if key_interval:
                self._sleep_interruptibly(key_interval)

        close_result = {'method': 'disabled'}
        if step.get('close_keyboard', True):
            close_result = self._close_license_plate_keyboard(step)

        result = {
            'plate': plate,
            'input_target': (
                list(input_target)
                if isinstance(input_target, tuple)
                else input_target
            ),
            'characters_entered': len(key_results),
            'key_results': key_results,
            'close_keyboard': close_result,
            'page_context': page_context,
        }
        save_as = str(step.get('save_as') or 'license_plate_input_result').strip()
        if save_as:
            self._set_variable(save_as, result, 'local')
        self.context['outputs']['last'] = result
        self._last_locator_diagnostics = {
            'step': step.get('name', '车牌输入'),
            'selected_strategy': 'business_component',
            **result,
        }
        logger.info(
            '车牌输入完成: plate=%s, context=%s, strategies=%s',
            plate,
            page_context.get('type'),
            [item['strategy'] for item in key_results],
        )
    
    def _action_long_press(self, step: Dict[str, Any]):
        """长按"""
        target = self._resolve_action_target(step)
        duration = step.get('duration', 2)
        
        logger.info(f"长按: {target}, 时长: {duration}秒")
        touch(target, duration=duration)
    
    def _action_drag(self, step: Dict[str, Any]):
        """拖拽：从起点拖拽到终点"""
        # 解析起点
        start_config = {
            'element_id': step.get('start_element_id'),
            'selector': step.get('start_selector'),
            'selector_type': step.get('start_selector_type', 'image'),
            'image_scope': step.get('image_scope', 'common')
        }
        start = self._resolve_selector(start_config)
        
        # 解析终点
        end_config = {
            'element_id': step.get('end_element_id'),
            'selector': step.get('end_selector'),
            'selector_type': step.get('end_selector_type', 'image'),
            'image_scope': step.get('image_scope', 'common')
        }
        end = self._resolve_selector(end_config)
        
        duration = step.get('duration', 0.8)
        
        if start and end:
            logger.info(f"拖拽: {start} -> {end}")
            swipe(start, end, duration=duration)
    
    def _action_swipe_to(self, step: Dict[str, Any]):
        """滑动直到目标元素出现"""
        target_config = {
            'element_id': step.get('target_element_id'),
            'selector': step.get('target_selector'),
            'selector_type': step.get('target_selector_type', 'image'),
            'image_scope': step.get('image_scope', 'common')
        }
        target = self._resolve_selector(target_config)
        
        direction = step.get('direction', 'up')
        max_swipes = step.get('max_swipes', 5)
        interval = step.get('interval', 0.5)
        
        for i in range(max_swipes):
            self.ensure_not_stopped(force=True)
            if exists(target):
                logger.info(f"找到目标元素，停止滑动")
                return
            
            logger.info(f"第 {i+1}/{max_swipes} 次滑动: {direction}")
            swipe_vector = G.DEVICE.get_current_resolution()
            
            if direction == 'up':
                swipe((swipe_vector[0]//2, swipe_vector[1]*0.7), 
                      (swipe_vector[0]//2, swipe_vector[1]*0.3))
            elif direction == 'down':
                swipe((swipe_vector[0]//2, swipe_vector[1]*0.3), 
                      (swipe_vector[0]//2, swipe_vector[1]*0.7))
            elif direction == 'left':
                swipe((swipe_vector[0]*0.7, swipe_vector[1]//2), 
                      (swipe_vector[0]*0.3, swipe_vector[1]//2))
            elif direction == 'right':
                swipe((swipe_vector[0]*0.3, swipe_vector[1]//2), 
                      (swipe_vector[0]*0.7, swipe_vector[1]//2))
            
            self._sleep_interruptibly(interval)
        
        logger.warning(f"滑动 {max_swipes} 次后仍未找到目标元素")
    
    def _action_image_exists_click(self, step: Dict[str, Any]):
        """主定位存在则点击主定位，否则点击备用定位"""
        # 主定位
        main_config = {
            'element_id': step.get('element_id'),
            'selector': step.get('selector'),
            'selector_type': step.get('selector_type', 'image'),
            'image_scope': step.get('image_scope', 'common'),
            'image_threshold': step.get('image_threshold', 0.85)
        }
        main_target = self._resolve_selector(main_config)
        
        # 备用定位
        fallback_config = {
            'element_id': step.get('fallback_element_id'),
            'selector': step.get('fallback_selector'),
            'selector_type': step.get('fallback_selector_type', 'image'),
            'image_scope': step.get('fallback_image_scope', 'common'),
            'image_threshold': step.get('fallback_image_threshold', 0.85)
        }
        fallback_target = self._resolve_selector(fallback_config)
        
        if main_target and exists(main_target):
            logger.info(f"主定位存在，点击主定位")
            touch(main_target)
        elif fallback_target:
            logger.info(f"主定位不存在，点击备用定位")
            touch(fallback_target)
    
    def _action_image_exists_click_chain(self, step: Dict[str, Any]):
        """主定位存在则依次点击主定位和备用定位，否则只点击备用定位"""
        # 主定位
        main_config = {
            'element_id': step.get('element_id'),
            'selector': step.get('selector'),
            'selector_type': step.get('selector_type', 'image'),
            'image_scope': step.get('image_scope', 'common')
        }
        main_target = self._resolve_selector(main_config)
        
        # 备用定位
        fallback_config = {
            'element_id': step.get('fallback_element_id'),
            'selector': step.get('fallback_selector'),
            'selector_type': step.get('fallback_selector_type', 'image'),
            'image_scope': step.get('fallback_image_scope', 'common')
        }
        fallback_target = self._resolve_selector(fallback_config)
        
        if main_target and exists(main_target):
            logger.info(f"主定位存在，依次点击主定位和备用定位")
            touch(main_target)
            self._sleep_interruptibly(0.5)
        
        if fallback_target:
            touch(fallback_target)
    
    def _action_unset_variable(self, step: Dict[str, Any]):
        """删除变量"""
        name = step.get('name')
        scope = step.get('scope', 'local')
        
        if name and scope in self.context and name in self.context[scope]:
            del self.context[scope][name]
            logger.info(f"删除变量: {scope}.{name}")
    
    def _action_extract_output(self, step: Dict[str, Any]):
        """
        从变量中按路径提取字段并保存为新变量。
        
        配置示例:
            source: "local.response"      # 来源变量，格式: scope.var_name
            path: "body.data.token"       # 提取路径，支持多级 key 和列表索引 [0]
            name: "token"                 # 保存为新变量名
            scope: "local"                # 保存到哪个作用域
        """
        source = step.get('source', '')
        path = step.get('path', '')
        name = step.get('name')
        scope = step.get('scope', 'local')
        
        if not name:
            raise ValueError("extract_output 需要 name 参数（保存的变量名）")
        if not source:
            raise ValueError("extract_output 需要 source 参数（来源变量）")
        
        # 解析 source: "scope.var_name" 或直接 "var_name"
        source_value = self._get_variable(source)
        if source_value is None:
            # 尝试按 scope.name 格式解析
            parts = source.split('.', 1)
            if len(parts) == 2 and parts[0] in self.context:
                source_value = self.context[parts[0]].get(parts[1])
        
        if source_value is None:
            raise ValueError(f"来源变量不存在: {source}")
        
        # 按 path 逐级提取
        if path:
            current = source_value
            for key in self._parse_path(path):
                if isinstance(current, dict):
                    current = current.get(key)
                elif isinstance(current, (list, tuple)):
                    try:
                        current = current[int(key)]
                    except (ValueError, IndexError):
                        current = None
                else:
                    current = None
                
                if current is None:
                    raise ValueError(f"提取失败: 路径 '{path}' 在 '{key}' 处找不到值")
            
            extracted = current
        else:
            extracted = source_value
        
        self._set_variable(name, extracted, scope)
        logger.info(f"提取成功: {source}.{path} -> {scope}.{name} = {extracted}")
    
    @staticmethod
    def _parse_path(path: str) -> List[str]:
        """解析提取路径，支持 'a.b.c' 和 'a[0].b' 格式"""
        keys = []
        for part in path.split('.'):
            if '[' in part:
                # 处理 "items[0]" -> ["items", "0"]
                base, rest = part.split('[', 1)
                if base:
                    keys.append(base)
                for idx_part in rest.split('['):
                    idx_part = idx_part.rstrip(']')
                    if idx_part:
                        keys.append(idx_part)
            else:
                if part:
                    keys.append(part)
        return keys
    
    def _action_screenshot(self, step: Dict[str, Any]):
        """截图（重命名自 _action_snapshot）"""
        self._action_snapshot(step)

    def _action_record_video(self, step: Dict[str, Any]):
        """录制当前设备画面并保存到本次执行截图目录。"""
        if not G.DEVICE:
            raise RuntimeError('设备未连接，无法录屏')
        duration = max(1.0, min(float(step.get('duration', 10)), 1800.0))
        fps = max(1, min(int(step.get('fps', 8)), 10))
        max_size = step.get('max_size', 1080)
        orientation = int(step.get('orientation', 0))
        filename = str(step.get('filename') or '').strip()
        if not filename:
            filename = f"record_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        if not filename.lower().endswith('.mp4'):
            filename += '.mp4'
        output_path = os.path.join(self.screenshots_dir, self._safe_filename(filename[:-4]) + '.mp4')

        device_class = G.DEVICE.__class__.__name__.lower()
        start_kwargs = {
            'max_time': int(duration) + 5,
            'output': output_path,
            'fps': fps,
            'orientation': orientation,
            'max_size': max_size,
        }
        if device_class != 'ios':
            start_kwargs['mode'] = step.get('record_mode', 'ffmpeg')

        logger.info('开始录屏: duration=%ss, output=%s', duration, output_path)
        started_path = G.DEVICE.start_recording(**start_kwargs)
        if started_path is None:
            raise RuntimeError('设备录屏启动失败，可能已有录屏任务正在运行')
        try:
            self._sleep_interruptibly(duration)
        finally:
            try:
                G.DEVICE.stop_recording()
            except Exception:
                logger.exception('停止设备录屏失败')

        if not os.path.exists(output_path):
            raise RuntimeError(f'录屏文件未生成: {output_path}')
        relative_path = os.path.relpath(output_path, settings.MEDIA_ROOT).replace(os.sep, '/')
        result = {
            'completed': True,
            'duration': duration,
            'fps': fps,
            'path': output_path,
            'url': f"{settings.MEDIA_URL.rstrip('/')}/{relative_path}",
            'size': os.path.getsize(output_path),
        }
        self._store_component_result(step, result)
    
    def _action_api_request(self, step: Dict[str, Any]):
        """
        执行HTTP请求，支持状态码校验、自动/手动响应解析和字段提取。
        
        配置项:
            method: GET / POST / PUT / DELETE / PATCH
            url: 请求地址（支持变量渲染）
            headers / params / json / data: 请求参数
            timeout: 超时秒数，默认10
            response_type: auto(自动判断) / json / text / binary
            expected_status: 期望的 HTTP 状态码，如 200
            save_as: 将完整响应结果保存为变量名
            scope: 变量保存的作用域，默认 local
            extracts: 字段提取列表
                - path: "body.data.token"
                  name: "token"
                  scope: "local"
        """
        import requests as req_lib
        
        method = step.get('method', 'GET').upper()
        url = self._render_value(step.get('url', ''))
        headers = step.get('headers', {})
        params = step.get('params', {})
        json_data = step.get('json', {})
        data = step.get('data', {})
        timeout = step.get('timeout', 10)
        save_as = step.get('save_as', '')
        scope = step.get('scope', 'local')
        response_type = step.get('response_type', 'auto')
        expected_status = step.get('expected_status')
        extracts = step.get('extracts', [])
        
        if not url:
            raise ValueError("api_request 缺少 url 参数")
        
        # 渲染 headers/params 中的变量
        if isinstance(headers, dict):
            headers = {k: self._render_value(v) if isinstance(v, str) else v
                       for k, v in headers.items()}
        if isinstance(params, dict):
            params = {k: self._render_value(v) if isinstance(v, str) else v
                      for k, v in params.items()}
        
        logger.info(f"HTTP请求: {method} {url}")
        
        try:
            response = req_lib.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data if json_data else None,
                data=data if data else None,
                timeout=timeout
            )
        except Exception as e:
            logger.error(f"HTTP请求失败: {str(e)}")
            raise
        
        # 解析响应体
        body = self._parse_response_body(response, response_type)
        
        result = {
            'status_code': response.status_code,
            'headers': dict(response.headers),
            'body': body
        }
        
        logger.info(f"HTTP响应: {response.status_code}")
        
        # 状态码校验
        if expected_status is not None:
            expected_status = int(expected_status)
            if response.status_code != expected_status:
                raise AssertionError(
                    f"HTTP状态码断言失败: 期望 {expected_status}, "
                    f"实际 {response.status_code}"
                )
        
        # 保存完整结果
        if save_as:
            self._set_variable(save_as, result, scope)
        
        # 字段提取
        if extracts and isinstance(extracts, list):
            for extract in extracts:
                if not isinstance(extract, dict):
                    continue
                e_path = extract.get('path', '')
                e_name = extract.get('name', '')
                e_scope = extract.get('scope', scope)
                if not e_name:
                    logger.warning(f"extracts 配置缺少 name，跳过: {extract}")
                    continue
                
                # 从 result 中按路径提取
                current = result
                try:
                    for key in self._parse_path(e_path):
                        if isinstance(current, dict):
                            current = current[key]
                        elif isinstance(current, (list, tuple)):
                            current = current[int(key)]
                        else:
                            raise KeyError(key)
                except (KeyError, IndexError, ValueError, TypeError) as e:
                    raise ValueError(
                        f"api_request extracts 提取失败: path='{e_path}' "
                        f"在 '{key}' 处出错: {e}"
                    )
                self._set_variable(e_name, current, e_scope)
                logger.info(f"api_request 提取: {e_path} -> {e_scope}.{e_name} = {current}")
    
    @staticmethod
    def _parse_response_body(response, response_type: str = 'auto'):
        """根据 response_type 解析响应体"""
        if response_type == 'json':
            try:
                return response.json()
            except Exception:
                return response.text
        elif response_type == 'text':
            return response.text
        elif response_type == 'binary':
            import base64
            return base64.b64encode(response.content).decode('ascii')
        else:
            # auto: 根据 Content-Type 自动判断
            content_type = response.headers.get('Content-Type', '')
            if 'json' in content_type or 'javascript' in content_type:
                try:
                    return response.json()
                except Exception:
                    return response.text
            return response.text
    
    def _action_if(self, step: Dict[str, Any]):
        """条件分支，支持丰富的操作符"""
        left = self._render_value(step.get('left', ''))
        right = self._render_value(step.get('right', ''))
        operator = step.get('operator', '==')
        then_steps = step.get('then_steps', [])
        else_steps = step.get('else_steps', [])
        
        condition = self._eval_condition(left, operator, right)
        
        logger.info(f"条件判断: {left} {operator} {right} = {condition}")
        
        # 执行分支
        if condition:
            for sub_step in then_steps:
                self._execute_step(sub_step)
        else:
            for sub_step in else_steps:
                self._execute_step(sub_step)
    
    @staticmethod
    def _eval_condition(left, operator: str, right) -> bool:
        """
        评估条件表达式。
        
        支持的操作符:
            ==, !=, >, >=, <, <=,
            in, not in, not_in,
            contains, notcontains, not_contains,
            regex, match,
            truthy, exists,
            falsy, not_exists,
            startswith, endswith
        """
        op = operator.strip().lower()
        
        # 相等判断
        if op == '==':
            return str(left) == str(right)
        if op == '!=':
            return str(left) != str(right)
        
        # 数值比较
        if op in ('>', '>=', '<', '<='):
            try:
                l_val, r_val = float(left), float(right)
            except (ValueError, TypeError):
                return False
            if op == '>':
                return l_val > r_val
            if op == '>=':
                return l_val >= r_val
            if op == '<':
                return l_val < r_val
            return l_val <= r_val
        
        # 包含判断
        if op == 'in':
            return str(left) in str(right)
        if op in ('not in', 'not_in'):
            return str(left) not in str(right)
        if op == 'contains':
            return str(right) in str(left)
        if op in ('notcontains', 'not_contains'):
            return str(right) not in str(left)
        
        # 正则匹配
        if op in ('regex', 'match'):
            try:
                return bool(re.search(str(right), str(left)))
            except re.error:
                return False
        
        # 真值 / 假值
        if op in ('truthy', 'exists'):
            return bool(left)
        if op in ('falsy', 'not_exists'):
            return not bool(left)
        
        # 前缀 / 后缀
        if op == 'startswith':
            return str(left).startswith(str(right))
        if op == 'endswith':
            return str(left).endswith(str(right))
        
        logger.warning(f"未知的条件操作符: {operator}，默认返回 False")
        return False
    
    def _action_loop(self, step: Dict[str, Any]):
        """循环：支持计数/条件/遍历三种模式"""
        mode = step.get('mode', 'count')
        steps = step.get('steps', [])
        max_loops = step.get('max_loops', 10)
        interval = step.get('interval', 0)
        
        if mode == 'count':
            # 计数循环
            times = step.get('times', 1)
            logger.info(f"计数循环: {times} 次")
            for i in range(times):
                self.ensure_not_stopped(force=True)
                logger.info(f"循环第 {i+1}/{times} 次")
                for sub_step in steps:
                    self._execute_step(sub_step)
                if interval > 0:
                    self._sleep_interruptibly(interval)
        
        elif mode == 'foreach':
            # 遍历循环
            items = step.get('items', [])
            item_var = step.get('item_var', 'item')
            item_scope = step.get('item_scope', 'local')
            
            logger.info(f"遍历循环: {len(items)} 个元素")
            for idx, item in enumerate(items):
                self.ensure_not_stopped(force=True)
                logger.info(f"循环第 {idx+1}/{len(items)} 次, {item_var}={item}")
                self._set_variable(item_var, item, item_scope)
                for sub_step in steps:
                    self._execute_step(sub_step)
                if interval > 0:
                    self._sleep_interruptibly(interval)
        
        elif mode == 'condition':
            # 条件循环
            left = step.get('left', '')
            operator = step.get('operator', '==')
            right = step.get('right', '')
            
            logger.info(f"条件循环: {left} {operator} {right}")
            loop_count = 0
            while loop_count < max_loops:
                self.ensure_not_stopped(force=True)
                left_val = self._render_value(left)
                right_val = self._render_value(right)
                
                # 评估条件
                condition = False
                if operator == '==':
                    condition = left_val == right_val
                elif operator == '!=':
                    condition = left_val != right_val
                
                if not condition:
                    break
                
                loop_count += 1
                logger.info(f"条件循环第 {loop_count} 次")
                for sub_step in steps:
                    self._execute_step(sub_step)
                if interval > 0:
                    self._sleep_interruptibly(interval)
    
    def _action_sequence(self, step: Dict[str, Any]):
        """顺序执行子步骤"""
        steps = step.get('steps', [])
        logger.info(f"顺序执行 {len(steps)} 个子步骤")
        for sub_step in steps:
            self.ensure_not_stopped(force=True)
            self._execute_step(sub_step)
    
    def _action_try(self, step: Dict[str, Any]):
        """异常处理：try/catch/finally"""
        try_steps = step.get('try_steps', [])
        catch_steps = step.get('catch_steps', [])
        finally_steps = step.get('finally_steps', [])
        error_var = step.get('error_var', 'error')
        error_scope = step.get('error_scope', 'local')
        
        logger.info("执行 try 块")
        try:
            for sub_step in try_steps:
                self._execute_step(sub_step)
        except Exception as e:
            logger.warning(f"捕获异常: {str(e)}")
            self._set_variable(error_var, str(e), error_scope)
            for sub_step in catch_steps:
                self._execute_step(sub_step)
        finally:
            logger.info("执行 finally 块")
            for sub_step in finally_steps:
                self._execute_step(sub_step)
    
    def _get_ocr_helper(self):
        """获取或创建 OCR Helper 实例"""
        if self._ocr_helper is None:
            if not OCR_AVAILABLE:
                raise RuntimeError("OCR 功能不可用，请安装: pip install easyocr opencv-python")
            self._ocr_helper = get_ocr_helper(languages=['ch_sim', 'en'], use_gpu=False)
        return self._ocr_helper
    
    
    def _action_foreach_assert(self, step: Dict[str, Any]):
        """循环点击断言（OCR）"""
        if not OCR_AVAILABLE:
            raise RuntimeError("foreach_assert 需要 OCR 支持，请安装 easyocr")
        
        try:
            # 从 config 中获取配置
            config = step.get('config', {})
            
            # 解析参数
            expected_list = config.get('expected_list', [])
            max_loops = config.get('max_loops', 5)
            interval = config.get('interval', 0.5)
            timeout = config.get('timeout', 5)
            match_mode = config.get('match_mode', 'contains')
            assert_type = config.get('assert_type', 'text')
            
            # 点击选择器
            click_selector_type = config.get('click_selector_type', 'image')
            click_selector = config.get('click_selector')
            click_config = {
                'element_id': config.get('click_element_id'),
                'selector_type': click_selector_type,
                'selector': click_selector,
                'image_scope': config.get('image_scope', 'common'),
                'image_threshold': config.get('image_threshold', 0.85),
                'timeout': timeout,
            }
            
            # OCR 区域选择器
            ocr_selector_type = config.get('ocr_selector_type', 'region')
            ocr_selector = config.get('ocr_selector')
            
            if not click_config.get('element_id') and not click_selector:
                raise ValueError("foreach_assert 需要有效的 click_selector")
            if not ocr_selector and not config.get('ocr_element_id'):
                raise ValueError("foreach_assert 需要 ocr_selector 参数")
            
            # 解析 OCR 区域坐标
            if ocr_selector_type == 'region':
                ocr_region = self._parse_ocr_region({
                    **config,
                    'ocr_selector': ocr_selector,
                    'ocr_element_id': config.get('ocr_element_id'),
                })
            else:
                raise ValueError(f"foreach_assert 仅支持 ocr_selector_type=region")
            
            # OCR 识别
            ocr = self._get_ocr_helper()
            
            # 循环点击并断言
            matched_count = 0
            min_match = int(step.get('min_match', 1) or 0)
            for i in range(max_loops):
                self.ensure_not_stopped(force=True)
                logger.info(f"循环点击断言 第 {i+1}/{max_loops} 次")
                
                # 点击
                click_target = self._resolve_action_target(click_config)
                touch(click_target)
                self._sleep_interruptibly(interval)
                
                # OCR 识别
                if assert_type == 'number':
                    actual_value = ocr.recognize_region_number(ocr_region)
                else:
                    actual_value = ocr.recognize_region_text(ocr_region)
                
                # 检查是否匹配期望列表中的任何值
                matched = False
                for expected in expected_list:
                    if assert_type == 'number':
                        expected_num = int(str(expected).replace(',', ''))
                        if actual_value == expected_num:
                            matched = True
                            break
                    else:
                        if match_mode == 'exact':
                            if actual_value == expected:
                                matched = True
                                break
                        elif match_mode == 'contains':
                            if expected in actual_value:
                                matched = True
                                break
                
                if matched:
                    matched_count += 1
                    logger.info(f"第 {i+1} 次匹配成功: {actual_value} 在期望列表中")
                else:
                    logger.warning(f"第 {i+1} 次未匹配: {actual_value} 不在期望列表中")
            
            logger.info(f"循环点击断言完成: 共 {max_loops} 次，匹配 {matched_count} 次")
            if matched_count < min_match:
                raise AssertionError(
                    f"循环点击断言失败: 期望至少匹配 {min_match} 次，实际 {matched_count} 次"
                )
            
        except Exception as e:
            logger.error(f"foreach_assert 执行失败: {str(e)}")
            raise
