# -*- coding: utf-8 -*-
from unittest import mock

import numpy as np
from django.test import SimpleTestCase

from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


class UiFlowRunnerActionAliasTests(SimpleTestCase):
    @mock.patch('apps.app_automation.runners.ui_flow_runner.ALLURE_AVAILABLE', False)
    def test_assertion_failure_is_soft_even_when_stop_on_error_is_enabled(self):
        runner = UiFlowRunner()
        runner._execute_step = mock.Mock(side_effect=[AssertionError('期望文本未命中'), None])
        runner._terminal_webview_page_state = mock.Mock(return_value={'matched': False})

        result = runner.run(
            [
                {'type': 'assert_text', 'id': 'assert-1', 'name': '校验结果'},
                {'type': 'wait', 'id': 'after-assert', 'name': '断言后继续'},
            ],
            runtime={'stop_on_error': True},
        )

        self.assertEqual(runner._execute_step.call_count, 2)
        self.assertEqual(result['passed'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['assertion_total'], 1)
        self.assertEqual(result['assertion_failed'], 1)
        self.assertFalse(result['assertions'][0]['assertion_matched'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.ALLURE_AVAILABLE', False)
    def test_assertion_failure_inside_custom_component_is_soft(self):
        runner = UiFlowRunner()
        runner._execute_step = mock.Mock(side_effect=[AssertionError('子断言未命中'), None])

        runner._execute_custom_component({
            'type': 'custom_login',
            'kind': 'custom',
            'steps': [
                {'type': 'assert_text', 'id': 'nested-assert', 'name': '子断言'},
                {'type': 'wait', 'id': 'nested-after', 'name': '子步骤继续'},
            ],
        })

        self.assertEqual(runner._execute_step.call_count, 2)
        self.assertEqual(runner.context['assertions'][0]['step_id'], 'nested-assert')
        self.assertFalse(runner.context['assertions'][0]['matched'])
        self.assertEqual(runner._nested_assertion_failures, 1)

    def test_run_stops_after_terminal_webview_result_page(self):
        runner = UiFlowRunner(platform='ios', device_id='ios-device')
        runner._execute_step = mock.Mock()
        runner._terminal_webview_page_state = mock.Mock(return_value={
            'matched': True,
            'location': {
                'href': 'https://example.test/result',
                'pathname': '/result',
            },
            'terminal_paths': ['/result'],
        })

        result = runner.run([
            {'type': 'touch', 'name': '提交申请'},
            {'type': 'smart_click', 'name': '智能点击-单位名称'},
        ])

        runner._execute_step.assert_called_once_with({'type': 'touch', 'name': '提交申请'})
        self.assertEqual(result['passed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(result['terminated'])
        self.assertEqual(
            result['outputs']['terminal_page']['location']['pathname'],
            '/result',
        )

    def test_hash_result_route_matches_terminal_webview_path(self):
        runner = UiFlowRunner()

        matched = runner._webview_url_matches_terminal_path(
            {
                'href': 'https://example.test/app#/result?from=apply',
                'pathname': '/app',
                'hash': '#/result?from=apply',
            },
            ['/result'],
        )

        self.assertTrue(matched)

    def test_standard_input_and_video_feed_aliases_are_dispatched(self):
        runner = UiFlowRunner()
        runner._action_input = mock.Mock()
        runner._action_short_video_feed = mock.Mock()

        runner._dispatch_step({'type': 'input_text'}, 'input_text')
        runner._dispatch_step({'type': 'fill'}, 'fill')
        runner._dispatch_step({'type': 'type'}, 'type')
        runner._dispatch_step({'type': 'send_keys'}, 'send_keys')
        runner._dispatch_step({'type': 'video_feed'}, 'video_feed')

        self.assertEqual(runner._action_input.call_count, 4)
        runner._action_short_video_feed.assert_called_once_with({'type': 'video_feed'})

    def test_popup_select_aliases_are_dispatched(self):
        runner = UiFlowRunner()
        runner._action_popup_select_text = mock.Mock()

        runner._dispatch_step({'type': 'popup_pick'}, 'popup_pick')
        runner._dispatch_step({'type': 'popup_select'}, 'popup_select')

        self.assertEqual(runner._action_popup_select_text.call_count, 2)

    def test_enter_key_aliases_are_dispatched(self):
        runner = UiFlowRunner()
        runner._action_press_enter = mock.Mock()

        runner._dispatch_step({'type': 'press_enter'}, 'press_enter')
        runner._dispatch_step({'type': 'enter'}, 'enter')
        runner._dispatch_step({'type': 'enter_key'}, 'enter_key')

        self.assertEqual(runner._action_press_enter.call_count, 3)

    @mock.patch('airtest.core.api.keyevent')
    def test_press_enter_sends_android_keycode_enter(self, keyevent_mock):
        runner = UiFlowRunner(platform='android')
        runner._sleep_interruptibly = mock.Mock()

        runner._action_press_enter({'type': 'press_enter', 'wait_after': 0.1})

        keyevent_mock.assert_called_once_with('KEYCODE_ENTER')
        runner._sleep_interruptibly.assert_called_once_with(0.1)
        self.assertEqual(runner.context['outputs']['last']['pressed'], 'ENTER')
        self.assertEqual(runner.context['outputs']['last']['method'], 'airtest_keyevent')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    def test_press_enter_uses_ios_text_enter(self, airtest_text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()

        runner._action_press_enter({'type': 'press_enter', 'wait_after': 0})

        airtest_text_mock.assert_called_once_with('', enter=True)
        runner._sleep_interruptibly.assert_called_once_with(0)
        self.assertEqual(runner.context['outputs']['last']['method'], 'ios_text_enter')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_ios_back_uses_edge_swipe_instead_of_android_keyevent(self, swipe_mock):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._sleep_interruptibly = mock.Mock()

        runner._action_back({'type': 'back', 'wait_after': 0.2})

        swipe_mock.assert_called_once_with((24, 1311), (940, 1311), duration=0.45)
        runner._sleep_interruptibly.assert_called_once_with(0.2)
        self.assertEqual(runner.context['outputs']['last']['method'], 'ios_edge_swipe')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_bottom_submit_button_retaps_above_text(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(603, 1923))
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._sleep_interruptibly = mock.Mock()

        runner._action_touch({'name': '自愈点击-同意', 'text': '同意并申请'})

        self.assertEqual(touch_mock.call_args_list[0].args[0], (603, 1923))
        self.assertEqual(touch_mock.call_args_list[1].args[0], (603, 1831))
        self.assertEqual(
            runner._last_locator_diagnostics['ios_primary_button_retap']['reason'],
            'bottom-primary-submit-button',
        )

    def test_signature_step_blocks_coordinate_fallback(self):
        runner = UiFlowRunner(platform='ios')

        reason = runner._coordinate_fallback_block_reason(
            {
                'name': '点击签署',
                'config': {
                    'resource_id': '去签署',
                    'text': '去签署',
                },
            },
            [{'strategy': 'resource_id', 'matched': False}],
        )

        self.assertEqual(reason, 'signature-target-coordinate-fallback-disabled')

    def test_picker_entry_step_blocks_coordinate_fallback(self):
        runner = UiFlowRunner(platform='ios')

        reason = runner._coordinate_fallback_block_reason(
            {
                'type': 'smart_click',
                'name': '智能点击申请地区',
                'config': {
                    'selector_type': 'image',
                    'selector': 'element_backup_6a5860d9.png',
                },
            },
            [
                {'strategy': 'resource_id', 'matched': False},
                {'strategy': 'ocr', 'matched': False},
                {'strategy': 'image', 'matched': False},
            ],
        )

        self.assertEqual(reason, 'picker-entry-coordinate-fallback-disabled')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_supplement_completion_requires_signature_button(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(603, 1920))
        runner._wait_for_visible_text = mock.Mock(return_value=False)

        with self.assertRaisesRegex(AssertionError, '未出现'):
            runner._action_touch({
                'name': '补充信息完成',
                'config': {'text': '完成补充'},
                'post_click_timeout': 0,
            })

        touch_mock.assert_called_once_with((603, 1920))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_signature_click_requires_page_to_change(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(603, 1917))
        runner._wait_until_text_gone = mock.Mock(return_value=False)

        with self.assertRaisesRegex(AssertionError, '仍然可见'):
            runner._action_touch({
                'name': '点击签署',
                'config': {'text': '去签署'},
                'post_click_timeout': 0,
            })

        touch_mock.assert_called_once_with((603, 1917))


class UiFlowRunnerPopupSelectionTests(SimpleTestCase):
    @staticmethod
    def _node(text, x1, x2):
        return {
            'text': text,
            'class_name': 'android.view.View',
            'visible': True,
            'enabled': True,
            'depth': 4,
            'bounds': {'x1': x1, 'y1': 1200, 'x2': x2, 'y2': 1280},
        }

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_popup_select_text_uses_input_path_without_element_ids(self, touch_mock):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1080, 1920))
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            self._node('浙江省', 40, 320),
            self._node('杭州市', 400, 680),
            self._node('拱墅区', 760, 1040),
        ])

        runner._action_popup_select_text({
            'value': '浙江省/杭州市/拱墅区',
            'max_swipes': 0,
            'save_as': 'area_result',
        })

        self.assertEqual(touch_mock.call_count, 3)
        self.assertEqual(
            runner.context['local']['area_result']['parts'],
            ['浙江省', '杭州市', '拱墅区'],
        )
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'popup_text_path',
        )


class UiFlowRunnerIOSPhotoUploadTests(SimpleTestCase):
    def test_page_file_inputs_are_not_misclassified_as_system_upload_menu(self):
        runner = UiFlowRunner(platform='ios')
        nodes = [{
            'resource_id': '选取文件',
            'text': '未选择文件',
            'content_desc': '选取文件',
            'visible': True,
        }]

        self.assertEqual(runner._detect_ios_system_overlay(nodes), '')

    def test_file_upload_menu_requires_both_visible_actions(self):
        runner = UiFlowRunner(platform='ios')
        nodes = [
            {'text': '照片图库', 'visible': True},
            {'text': '选取文件', 'visible': True},
        ]

        self.assertEqual(
            runner._detect_ios_system_overlay(nodes),
            'file-upload-menu',
        )

    def test_photo_picker_markers_take_priority_over_stale_upload_menu_text(self):
        runner = UiFlowRunner(platform='ios')
        nodes = [
            {'text': '照片图库', 'visible': True},
            {'text': '选取文件', 'visible': True},
            {'text': '精选集', 'visible': True},
            {'text': '私密访问照片', 'visible': True},
        ]

        self.assertEqual(
            runner._detect_ios_system_overlay(nodes),
            'photo-picker',
        )

    def test_photo_asset_selection_step_taps_photo_library_in_upload_menu(self):
        runner = UiFlowRunner(platform='ios')
        nodes = [{
            'text': '照片图库',
            'visible': True,
            'bounds': {'x1': 120, 'y1': 1680, 'x2': 1080, 'y2': 1800},
        }]

        target, detail = runner._ios_photo_upload_overlay_target(
            {'type': 'smart_click', 'name': '智能点击-选择图片'},
            nodes,
            'file-upload-menu',
        )

        self.assertEqual(target, (600, 1740))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'file-upload-menu-photo-library')

    def test_photo_asset_selection_step_taps_first_photo_grid_cell(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        nodes = [
            {
                'text': '私密访问照片',
                'visible': True,
                'class_name': 'XCUIElementTypeOther',
                'bounds': {'x1': 60, 'y1': 460, 'x2': 1140, 'y2': 760},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 0, 'y1': 900, 'x2': 398, 'y2': 1298},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 404, 'y1': 900, 'x2': 802, 'y2': 1298},
            },
        ]

        target, detail = runner._ios_photo_upload_overlay_target(
            {'type': 'smart_click', 'name': '智能点击-选择照片'},
            nodes,
            'photo-picker',
        )

        self.assertEqual(target, (199, 1099))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'first-photo-grid-cell')

    def test_photo_asset_selection_step_prefers_recorded_grid_position(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        nodes = [
            {'text': '精选集', 'visible': True},
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 84, 'y1': 1275, 'x2': 582, 'y2': 1662},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 624, 'y1': 1275, 'x2': 1122, 'y2': 1662},
            },
        ]

        target, detail = runner._ios_photo_upload_overlay_target(
            {
                'type': 'click',
                'name': 'iOS 录制点击 1',
                'config': {
                    'selector_type': 'pos',
                    'selector': '346,362',
                    'source_resolution': {'width': 402, 'height': 874},
                    'normalized_position': {'x': 0.860697, 'y': 0.414188},
                    'fingerprint': {
                        'resource_id': 'PXGGridLayout-Info',
                        'text': '照片, 6月26日, 17:56',
                        'content_desc': '照片, 6月26日, 17:56',
                        'class_name': 'XCUIElementTypeImage',
                    },
                },
            },
            nodes,
            'photo-picker',
        )

        self.assertEqual(target, (873, 1468))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'recorded-photo-grid-cell')
        self.assertEqual(detail['recorded_target'], [1038, 1086])

    def test_identity_front_upload_selects_second_gallery_photo(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._ios_pending_photo_upload_slot = 'front'
        nodes = [
            {'text': '精选集', 'visible': True},
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 84, 'y1': 1275, 'x2': 582, 'y2': 1662},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 624, 'y1': 1275, 'x2': 1122, 'y2': 1662},
            },
        ]

        target, detail = runner._ios_photo_upload_overlay_target(
            {'type': 'click', 'name': '选择图片'},
            nodes,
            'photo-picker',
        )

        self.assertEqual(target, (603, 1075))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'identity-front-second-photo-grid-estimate')
        self.assertEqual(detail['upload_slot'], 'front')

    def test_identity_back_upload_selects_first_gallery_photo(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._ios_pending_photo_upload_slot = 'back'
        nodes = [
            {'text': '精选集', 'visible': True},
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 84, 'y1': 1275, 'x2': 582, 'y2': 1662},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 624, 'y1': 1275, 'x2': 1122, 'y2': 1662},
            },
        ]

        target, detail = runner._ios_photo_upload_overlay_target(
            {'type': 'click', 'name': '选择图片'},
            nodes,
            'photo-picker',
        )

        self.assertEqual(target, (199, 1075))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'identity-back-first-photo-grid-estimate')
        self.assertEqual(detail['upload_slot'], 'back')

    def test_pending_identity_upload_uses_photo_picker_before_semantic_locator(self):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((276, 1042), {'matched': True}))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '精选集', 'visible': True},
            {'text': '私密访问照片', 'visible': True},
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 0, 'y1': 856, 'x2': 398, 'y2': 1254},
            },
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 404, 'y1': 856, 'x2': 802, 'y2': 1254},
            },
        ])
        runner._ios_pending_photo_upload_slot = 'front'

        target = runner._resolve_action_target({
            'type': 'click',
            'name': 'iOS 录制点击 1',
            'config': {
                'selector_type': 'pos',
                'selector': '92,347',
                'source_resolution': {'width': 402, 'height': 874},
                'normalized_position': {'x': 0.228856, 'y': 0.397025},
                'fingerprint': {
                    'resource_id': '借款人信息',
                    'text': '借款人信息',
                    'content_desc': '借款人信息',
                    'class_name': 'XCUIElementTypeStaticText',
                },
            },
        })

        self.assertEqual(target, (603, 1075))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_photo_upload_overlay',
        )
        self.assertEqual(
            runner._last_locator_diagnostics['attempts'][0]['detail']['method'],
            'identity-front-second-photo-grid-estimate',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_pending_front_upload_reopens_menu_from_top_upload_frame(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((333, 2060), {'matched': True}))
        runner._current_page_state = mock.Mock(return_value={})
        runner._detect_semantic_page = mock.Mock(return_value={})
        runner._ios_pending_photo_upload_slot = 'front'
        page_nodes = [
            {'text': '身份证正面', 'visible': True},
            {'text': '身份证反面', 'visible': True},
            {
                'text': '示例正面',
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 96, 'y1': 1960, 'x2': 560, 'y2': 2320},
            },
        ]
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            page_nodes,
            page_nodes,
            [
                {
                    'text': '照片图库',
                    'content_desc': '照片图库',
                    'visible': True,
                    'bounds': {'x1': 24, 'y1': 1377, 'x2': 774, 'y2': 1506},
                },
                {'text': '选取文件', 'visible': True},
            ],
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': '选择图片',
            'photo_upload_menu_open_timeout': 0.5,
        })

        self.assertEqual(target, (399, 1442))
        touch_mock.assert_called_once_with((345, 1440))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_photo_upload_menu_reopen',
        )
        detail = runner._last_locator_diagnostics['attempts'][0]['detail']
        self.assertEqual(detail['upload_slot'], 'front')
        self.assertEqual(detail['upload_target'], [345, 1440])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_pending_back_upload_reopens_menu_from_top_upload_frame(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((870, 2060), {'matched': True}))
        runner._current_page_state = mock.Mock(return_value={})
        runner._detect_semantic_page = mock.Mock(return_value={})
        runner._ios_pending_photo_upload_slot = 'back'
        page_nodes = [
            {'text': '身份证正面', 'visible': True},
            {'text': '身份证反面', 'visible': True},
            {
                'text': '示例反面',
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 650, 'y1': 1960, 'x2': 1110, 'y2': 2320},
            },
        ]
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            page_nodes,
            page_nodes,
            [
                {
                    'text': '照片图库',
                    'content_desc': '照片图库',
                    'visible': True,
                    'bounds': {'x1': 24, 'y1': 1377, 'x2': 774, 'y2': 1506},
                },
                {'text': '选取文件', 'visible': True},
            ],
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': '选择图片',
            'photo_upload_menu_open_timeout': 0.5,
        })

        self.assertEqual(target, (399, 1442))
        touch_mock.assert_called_once_with((891, 1452))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_photo_upload_menu_reopen',
        )
        detail = runner._last_locator_diagnostics['attempts'][0]['detail']
        self.assertEqual(detail['upload_slot'], 'back')
        self.assertEqual(detail['upload_target'], [891, 1452])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_pending_upload_blocks_semantic_fallback_when_menu_does_not_open(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((276, 1058), {'matched': True}))
        runner._current_page_state = mock.Mock(return_value={})
        runner._detect_semantic_page = mock.Mock(return_value={})
        runner._ios_pending_photo_upload_slot = 'front'
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '身份证正面', 'visible': True},
            {'text': '身份证反面', 'visible': True},
            {
                'text': '示例正面',
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 96, 'y1': 1960, 'x2': 560, 'y2': 2320},
            },
        ])

        with self.assertRaises(ValueError):
            runner._resolve_action_target({
                'type': 'click',
                'name': 'iOS 录制点击 1',
                'photo_upload_menu_open_timeout': 0.5,
                'config': {
                    'fingerprint': {
                        'resource_id': 'PXGGridLayout-Info',
                        'text': '照片, 6月26日, 17:56',
                        'content_desc': '照片, 6月26日, 17:56',
                        'class_name': 'XCUIElementTypeImage',
                    },
                },
            })

        touch_mock.assert_called_once_with((345, 1440))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['blocked_reason'],
            'ios-identity-photo-flow-blocked-ordinary-fallback',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_pending_upload_menu_opener_retries_upload_frame_when_menu_does_not_open(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((276, 1058), {'matched': True}))
        runner._current_page_state = mock.Mock(return_value={})
        runner._detect_semantic_page = mock.Mock(return_value={})
        runner._ios_pending_photo_upload_slot = 'front'
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '身份证正面', 'visible': True},
            {'text': '身份证反面', 'visible': True},
            {
                'text': '示例正面',
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 96, 'y1': 1180, 'x2': 560, 'y2': 1540},
            },
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': '选择图片',
            'photo_upload_menu_open_timeout': 0.5,
        })

        self.assertEqual(target, (345, 1440))
        touch_mock.assert_called_once_with((345, 1440))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_identity_upload_frame_retry',
        )
        self.assertEqual(
            runner._last_locator_diagnostics['attempts'][0]['detail']['reason'],
            'photo-upload-menu-not-opened',
        )
        self.assertEqual(
            runner._last_locator_diagnostics['attempts'][1]['detail']['reason'],
            'defer-menu-open-to-explicit-upload-frame-tap',
        )

    def test_identity_upload_step_prefers_top_frame_center_before_semantic_locator(self):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=((333, 1468), {'matched': True}))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '身份证正面', 'visible': True},
            {'text': '身份证反面', 'visible': True},
            {'text': '标准', 'visible': True},
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': '点击身份证正面',
        })

        self.assertEqual(target, (345, 1440))
        runner._semantic_target.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_identity_upload_frame',
        )

    def test_photo_asset_selection_step_uses_grid_estimate_without_nodes(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))

        target, detail = runner._ios_photo_upload_overlay_target(
            {'type': 'smart_click', 'name': '智能点击-选择照片'},
            [],
            'photo-picker',
        )

        self.assertEqual(target, (199, 1075))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'first-photo-grid-estimate')

    def test_photo_picker_asset_selection_allows_recorded_coordinate_fallback(self):
        runner = UiFlowRunner(platform='ios')
        runner._last_page_nodes = [
            {'text': '精选集', 'visible': True},
            {
                'resource_id': 'PXGGridLayout-Info',
                'text': '照片, 6月26日, 17:56',
                'content_desc': '照片, 6月26日, 17:56',
                'visible': True,
            },
        ]

        reason = runner._coordinate_fallback_block_reason(
            {
                'type': 'click',
                'name': 'iOS 录制点击 1',
                'selector_type': 'pos',
                'fingerprint': {
                    'resource_id': 'PXGGridLayout-Info',
                    'text': '照片, 6月26日, 17:56',
                    'content_desc': '照片, 6月26日, 17:56',
                    'class_name': 'XCUIElementTypeImage',
                },
            },
            [{'strategy': 'semantic_fingerprint', 'matched': False}],
        )

        self.assertEqual(reason, '')

    def test_file_upload_menu_still_blocks_unknown_coordinate_fallback(self):
        runner = UiFlowRunner(platform='ios')
        runner._last_page_nodes = [
            {'text': '照片图库', 'visible': True},
            {'text': '选取文件', 'visible': True},
        ]

        reason = runner._coordinate_fallback_block_reason(
            {
                'type': 'click',
                'name': 'iOS 录制点击 1',
                'selector_type': 'pos',
            },
            [{'strategy': 'semantic_fingerprint', 'matched': False}],
        )

        self.assertEqual(reason, 'ios-system-overlay:file-upload-menu')

    def test_recorded_photo_asset_click_reopens_photo_library_when_menu_still_visible(self):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=(None, {'matched': False}))
        runner._current_page_state = mock.Mock(return_value={})
        runner._detect_semantic_page = mock.Mock(return_value={})
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'resource_id': '照片图库',
                'text': '照片图库',
                'content_desc': '照片图库',
                'visible': True,
                'bounds': {'x1': 24, 'y1': 1377, 'x2': 774, 'y2': 1506},
            },
            {
                'resource_id': '选取文件',
                'text': '选取文件',
                'content_desc': '选取文件',
                'visible': True,
                'bounds': {'x1': 24, 'y1': 1503, 'x2': 774, 'y2': 1632},
            },
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': 'iOS 录制点击 1',
            'config': {
                'selector_type': 'pos',
                'selector': '346,362',
                'source_resolution': {'width': 402, 'height': 874},
                'normalized_position': {'x': 0.860697, 'y': 0.414188},
                'coordinate_mode': 'normalized',
                'fingerprint': {
                    'resource_id': 'PXGGridLayout-Info',
                    'text': '照片, 6月26日, 17:56',
                    'content_desc': '照片, 6月26日, 17:56',
                    'class_name': 'XCUIElementTypeImage',
                },
            },
        })

        self.assertEqual(target, (399, 1442))
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_photo_upload_overlay',
        )
        runner._semantic_target.assert_not_called()

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_recorded_photo_asset_click_selects_photo_after_reopening_library(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '照片图库', 'visible': True},
            {'text': '选取文件', 'visible': True},
            {'text': '精选集', 'visible': True},
            {'text': '私密访问照片', 'visible': True},
            {
                'visible': True,
                'class_name': 'XCUIElementTypeImage',
                'bounds': {'x1': 0, 'y1': 900, 'x2': 398, 'y2': 1298},
            },
        ])
        runner._last_locator_diagnostics = {
            'selected_strategy': 'ios_photo_upload_overlay',
            'attempts': [{
                'strategy': 'ios_photo_upload_overlay',
                'matched': True,
                'detail': {'overlay': 'file-upload-menu'},
            }],
        }

        detail = runner._select_ios_photo_after_upload_menu_if_needed({
            'type': 'click',
            'name': 'iOS 录制点击 1',
            'config': {
                'selector_type': 'pos',
                'fingerprint': {
                    'resource_id': 'PXGGridLayout-Info',
                    'text': '照片, 6月26日, 17:56',
                    'content_desc': '照片, 6月26日, 17:56',
                    'class_name': 'XCUIElementTypeImage',
                },
            },
        })

        self.assertTrue(detail['matched'])
        self.assertEqual(detail['target'], [199, 1099])
        self.assertEqual(
            detail['detail']['method'],
            'first-photo-grid-cell',
        )
        touch_mock.assert_called_once_with((199, 1099))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_photo_library_menu_step_does_not_auto_toggle_gallery_photo(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._ios_pending_photo_upload_slot = 'front'
        runner._last_locator_diagnostics = {
            'selected_strategy': 'ios_photo_upload_overlay',
            'attempts': [{
                'strategy': 'ios_photo_upload_overlay',
                'matched': True,
                'detail': {'overlay': 'file-upload-menu'},
            }],
        }

        detail = runner._select_ios_photo_after_upload_menu_if_needed({
            'type': 'click',
            'name': '选择图片',
        })

        self.assertFalse(detail['matched'])
        self.assertEqual(detail['reason'], 'not-recorded-photo-asset-step')
        touch_mock.assert_not_called()

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_picker_entry_step_skips_when_ios_dialog_already_open(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '网页对话框', 'visible': True},
            {'text': '取消', 'visible': True},
            {'text': '确认', 'visible': True},
            {'text': '北京市', 'visible': True},
        ])

        runner._action_touch({
            'type': 'smart_click',
            'name': '智能点击申请地区',
        })

        touch_mock.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'picker_already_open',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_picker_entry_dismisses_license_plate_keyboard_before_clicking(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._clear_restored_form_state_if_needed = mock.Mock(return_value={'matched': False})
        runner._license_plate_keyboard_visible = mock.Mock(side_effect=[True, False])
        runner._close_license_plate_keyboard = mock.Mock(return_value={
            'method': 'confirm_text',
            'text': '确认',
        })
        runner._wait_for_ios_keyboard_hidden = mock.Mock(return_value=([], True))
        runner._ios_picker_dialog_visible = mock.Mock(return_value=False)
        runner._resolve_action_target = mock.Mock(return_value=(777, 1836))
        runner._sleep_interruptibly = mock.Mock()
        runner._verify_click_postcondition = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'resource_id': '车牌号键盘',
                'text': '车牌号键盘',
                'content_desc': '车牌号键盘',
                'visible': True,
                'bounds': {'x1': 0, 'y1': 1482, 'x2': 1206, 'y2': 2328},
            }
        ])

        runner._action_touch({
            'type': 'click',
            'name': 'iOS 录制点击申请地区',
            'fingerprint': {
                'resource_id': '申请地区 申请地区',
                'text': '申请地区 申请地区',
                'content_desc': '申请地区 申请地区',
                'class_name': 'XCUIElementTypeButton',
            },
        })

        runner._close_license_plate_keyboard.assert_called_once()
        runner._wait_for_ios_keyboard_hidden.assert_called_once()
        runner._resolve_action_target.assert_called_once()
        touch_mock.assert_called_once_with((777, 1836))
        runner._verify_click_postcondition.assert_called_once()

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_picker_confirm_step_is_not_skipped_when_ios_dialog_open(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '网页对话框', 'visible': True},
            {'text': '取消', 'visible': True},
            {'text': '确认', 'visible': True},
            {'text': '北京市', 'visible': True},
        ])
        runner._resolve_action_target = mock.Mock(return_value=(1110, 1464))
        runner._sleep_interruptibly = mock.Mock()
        runner._verify_click_postcondition = mock.Mock()

        runner._action_touch({
            'type': 'smart_click',
            'name': '智能点击-申请地区-确认',
        })

        touch_mock.assert_called_once_with((1110, 1464))
        runner._verify_click_postcondition.assert_called_once()

    def test_form_select_entry_uses_visible_label_row(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))

        target, detail = runner._form_select_entry_target(
            {'type': 'smart_click', 'name': '智能点击申请地区'},
            [
                {
                    'text': '申请地区',
                    'visible': True,
                    'bounds': {'x1': 84, 'y1': 1516, 'x2': 330, 'y2': 1696},
                },
                {
                    'text': '请选择',
                    'visible': True,
                    'bounds': {'x1': 820, 'y1': 1516, 'x2': 1040, 'y2': 1696},
                },
            ],
        )

        self.assertEqual(target, (603, 1606))
        self.assertTrue(detail['matched'])
        self.assertEqual(detail['method'], 'form-select-label-row')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_restored_form_state_is_cleared_before_sensitive_steps(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [
                {
                    'text': '已为您恢复上一次填写的信息',
                    'visible': True,
                    'bounds': {'x1': 120, 'y1': 20, 'x2': 760, 'y2': 90},
                },
                {
                    'text': '一键清空',
                    'visible': True,
                    'bounds': {'x1': 850, 'y1': 20, 'x2': 1100, 'y2': 90},
                },
            ],
            [
                {
                    'text': '上一步',
                    'visible': True,
                    'bounds': {'x1': 980, 'y1': 1740, 'x2': 1160, 'y2': 1940},
                },
            ],
        ])

        detail = runner._clear_restored_form_state_if_needed({
            'type': 'smart_click',
            'name': '智能点击申请地区',
        })

        self.assertTrue(detail['matched'])
        self.assertEqual(touch_mock.call_args_list[0].args[0], (975, 55))
        self.assertEqual(touch_mock.call_args_list[1].args[0], (1070, 1840))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_downstream_supplement_page_returns_to_recorded_flow(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [
                {'text': '详细地址', 'visible': True},
                {'text': '工作信息', 'visible': True},
                {'text': '职业类型', 'visible': True},
            ],
            [
                {'text': '详细地址', 'visible': True},
                {'text': '工作信息', 'visible': True},
            ],
        ])

        detail = runner._clear_restored_form_state_if_needed({
            'type': 'smart_click',
            'name': '智能点击申请地区',
        })

        self.assertTrue(detail['matched'])
        self.assertTrue(detail['downstream_supplement_page'])
        touch_mock.assert_called_once_with((1097, 1835))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_photo_picker_done_waits_for_web_upload_mask_to_clear(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        element = mock.Mock()
        element.config = {
            'resource_id': 'PUOneUpBarButtonItemIdentifierAssetExplorerReviewScreenDone',
            'fingerprint': {
                'resource_id': 'PUOneUpBarButtonItemIdentifierAssetExplorerReviewScreenDone',
            },
        }
        runner._load_element = mock.Mock(return_value=element)
        runner._resolve_action_target = mock.Mock(return_value=(1092, 330))
        runner._sleep_interruptibly = mock.Mock()
        runner.ensure_not_stopped = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [{'text': '加载中...', 'visible': True}],
            [{'text': '借款人信息', 'visible': True}],
            [{'text': '借款人信息', 'visible': True}],
        ])

        runner._action_touch({
            'element_id': 165,
            'name': '身份证正面-确认',
        })

        touch_mock.assert_called_once_with((1092, 330))
        self.assertEqual(runner._dump_current_ui_nodes.call_count, 3)
        self.assertTrue(
            runner._last_locator_diagnostics['ios_photo_upload_wait']['settled']
        )
        self.assertTrue(
            runner._last_locator_diagnostics['ios_photo_upload_wait']['saw_loading']
        )


class UiFlowRunnerChromeToolbarTests(SimpleTestCase):
    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_hidden_chrome_toolbar_is_revealed_and_relocated(self, swipe_mock):
        runner = UiFlowRunner()
        fingerprint = {
            'version': 1,
            'resource_id': 'com.android.chrome:id/home_button',
            'package': 'com.android.chrome',
            'class_name': 'android.widget.ImageButton',
            'clickable': True,
        }
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1080, 2400))
        runner._semantic_target = mock.Mock(return_value=((63, 203), {'matched': True}))

        target, diagnostics = runner._reveal_chrome_toolbar(fingerprint)

        self.assertEqual(target, (63, 203))
        swipe_mock.assert_called_once()
        self.assertEqual(diagnostics['recovery'], 'reveal-chrome-toolbar')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_hidden_chrome_toolbar_never_uses_stale_page_coordinate(self, swipe_mock):
        runner = UiFlowRunner()
        step = {
            'name': 'Chrome Home',
            'selector_type': 'pos',
            'selector': '60,203',
            'source_resolution': {'width': 1080, 'height': 2400},
            'fingerprint': {
                'version': 1,
                'resource_id': 'com.android.chrome:id/home_button',
                'package': 'com.android.chrome',
                'class_name': 'android.widget.ImageButton',
                'clickable': True,
            },
            'timeout': 0.1,
        }

        runner._wait_for_page_stable = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1080, 2400))
        runner._semantic_target = mock.Mock(return_value=(None, {'matched': False}))
        runner._reveal_chrome_toolbar = mock.Mock(return_value=(None, {'matched': False}))
        runner.runtime['semantic_locator_timeout'] = 0.1

        with self.assertRaisesRegex(ValueError, '定位失败'):
            runner._resolve_action_target(step)

        swipe_mock.assert_not_called()
        self.assertIsNone(runner._last_locator_diagnostics['selected_strategy'])


class UiFlowRunnerShortVideoTests(SimpleTestCase):
    def test_wait_video_play_requires_continuous_motion_samples(self):
        runner = UiFlowRunner()
        runner._sleep_interruptibly = mock.Mock()
        frames = [
            np.zeros((16, 16), dtype=np.uint8),
            np.full((16, 16), 20, dtype=np.uint8),
            np.full((16, 16), 40, dtype=np.uint8),
        ]
        runner._take_video_frame = mock.Mock(side_effect=frames)

        result = runner._wait_for_video_play({
            'timeout': 1,
            'sample_interval': 0.1,
            'motion_threshold': 0.01,
            'required_motion_samples': 2,
        })

        self.assertTrue(result['playing'])
        self.assertEqual(result['motion_samples'], 2)

    def test_short_video_feed_loops_watch_swipe_and_verify(self):
        runner = UiFlowRunner()
        runner._handle_video_exception = mock.Mock(return_value=None)
        runner._wait_for_video_play = mock.Mock(return_value={'playing': True})
        runner._watch_video_for_duration = mock.Mock(return_value={'playing': True})
        runner._perform_random_video_actions = mock.Mock(return_value=[])
        runner._swipe_to_next_video = mock.Mock(return_value={'verified': True})

        runner._action_short_video_feed({
            'count': 3,
            'watch_time': {'min': 2, 'max': 5},
            'random_stay': False,
            'capture_each_video': False,
            'exception_detection': False,
            'save_as': 'feed_result',
        })

        result = runner.context['local']['feed_result']
        self.assertEqual(result['watched_count'], 3)
        self.assertEqual(result['swipe_count'], 2)
        self.assertEqual(result['watch_durations'], [2.0, 2.0, 2.0])
        self.assertEqual(runner._watch_video_for_duration.call_count, 3)
        self.assertEqual(runner._swipe_to_next_video.call_count, 2)
        self.assertEqual(runner._wait_for_video_play.call_count, 3)

    def test_video_swipe_points_are_resolution_aware(self):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1080, 2400))

        start, end, duration = runner._video_swipe_points({
            'direction': 'up',
            'swipe_distance': 0.8,
            'random_swipe': False,
        })

        self.assertEqual(start[0], 540)
        self.assertEqual(end[0], 540)
        self.assertGreater(start[1], end[1])
        self.assertGreaterEqual(start[1], 0)
        self.assertLess(start[1], 2400)
        self.assertEqual(duration, 0.36)

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_random_action_uses_normalized_target_and_probability(self, touch_mock):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1000, 2000))
        runner._sleep_interruptibly = mock.Mock()

        with mock.patch('apps.app_automation.runners.ui_flow_runner.random.random', return_value=0):
            actions = runner._perform_random_video_actions({
                'like_probability': 1,
                'favorite_probability': 0,
                'comment_probability': 0,
                'follow_probability': 0,
                'action_targets': {'like': [0.9, 0.5]},
                'action_interval': 0,
            })

        touch_mock.assert_called_once_with((900, 1000))
        self.assertEqual(actions[0]['action'], 'like')
        self.assertTrue(actions[0]['executed'])


class UiFlowRunnerLicensePlateTests(SimpleTestCase):
    def test_normalize_license_plate_supports_standard_and_new_energy(self):
        self.assertEqual(
            UiFlowRunner._normalize_license_plate(' 浙a12345 '),
            '浙A12345',
        )
        self.assertEqual(
            UiFlowRunner._normalize_license_plate('浙ad12345'),
            '浙AD12345',
        )

        with self.assertRaisesRegex(ValueError, '首位'):
            UiFlowRunner._normalize_license_plate('AA12345')

    def test_coordinate_fallback_maps_province_and_alphanumeric_keys(self):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1000, 2000))
        region = (0, 1000, 1000, 2000)

        province_target, province_detail = runner._coordinate_license_plate_key_target(
            '浙', 'province', region, {}
        )
        number_target, number_detail = runner._coordinate_license_plate_key_target(
            '1', 'alphanumeric', region, {}
        )

        self.assertEqual(province_target, (350, 1375))
        self.assertEqual(province_detail['row'], 1)
        self.assertEqual(number_target, (50, 1125))
        self.assertEqual(number_detail['row'], 0)

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_license_plate_component_opens_keyboard_inputs_every_key_and_closes(self, touch_mock):
        runner = UiFlowRunner()
        runner._sleep_interruptibly = mock.Mock()
        runner._detect_page_context = mock.Mock(return_value={
            'platform': 'android',
            'type': 'webview',
            'focus': 'com.demo/.MainActivity',
            'webview_count': 1,
        })
        runner._resolve_action_target = mock.Mock(return_value=(500, 600))
        runner._find_license_plate_key = mock.Mock(side_effect=[
            ((100 + index, 1500), 'semantic', [{'strategy': 'semantic', 'matched': True}])
            for index in range(7)
        ])
        runner._close_license_plate_keyboard = mock.Mock(return_value={
            'method': 'confirm_text',
            'text': '确认',
        })

        runner._action_license_plate_input({
            'name': '输入车牌',
            'plate': '浙A12345',
            'selector_type': 'pos',
            'selector': '500,600',
            'key_interval': 0,
            'save_as': 'plate_result',
        })

        self.assertEqual(touch_mock.call_count, 8)
        touch_mock.assert_any_call((500, 600))
        self.assertEqual(runner._find_license_plate_key.call_count, 7)
        result = runner.context['local']['plate_result']
        self.assertEqual(result['plate'], '浙A12345')
        self.assertEqual(result['characters_entered'], 7)
        self.assertEqual(result['page_context']['type'], 'webview')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_plate_click_dismisses_system_keyboard_and_prefers_container(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._dismiss_ios_system_keyboard = mock.Mock(return_value={
            'dismissed': True,
            'method': 'wda_keyboard_dismiss',
        })
        runner._sleep_interruptibly = mock.Mock()
        runner._resolve_action_target = mock.Mock(return_value=(1023, 1650))
        runner._license_plate_input_container_target = mock.Mock(return_value=(
            (178, 1650),
            {'matched': True, 'method': 'container-first-cell'},
        ))
        runner._wait_for_license_plate_keyboard = mock.Mock(return_value=True)

        runner._action_click({
            'name': '点击车牌号',
            'selector_type': 'text',
            'selector': '新能源',
            'ocr_text': '车牌号',
        })

        runner._dismiss_ios_system_keyboard.assert_called_once_with()
        touch_mock.assert_called_once_with((178, 1650))
        self.assertTrue(
            runner._last_locator_diagnostics['license_plate_keyboard']['opened']
        )

    def test_ios_plate_keyboard_confirm_is_not_treated_as_plate_input_trigger(self):
        runner = UiFlowRunner(platform='ios')
        element = mock.Mock()
        element.name = '车牌号键盘-确认'
        element.element_type = 'self_heal_click'
        element.config = {
            'class_name': 'XCUIElementTypeStaticText',
            'resource_id': '确认',
            'fingerprint': {
                'class_name': 'XCUIElementTypeStaticText',
                'resource_id': '确认',
                'content_desc': '确认',
                'text': '确认',
            },
        }
        runner._load_element = mock.Mock(return_value=element)
        runner._open_license_plate_keyboard = mock.Mock()
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._action_touch = mock.Mock()
        step = {
            'element_id': 217,
            'name': '自愈点击车牌号键盘-确认',
            'selector_type': 'image',
            'selector': 'element_backup.png',
        }

        runner._action_click(step)

        runner._open_license_plate_keyboard.assert_not_called()
        runner._action_touch.assert_called_once_with(step)

    def test_detected_keyboard_region_includes_ios_top_number_row(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        nodes = [{
            'resource_id': '车牌号键盘',
            'text': '车牌号键盘',
            'content_desc': '车牌号键盘',
            'bounds': {'x1': 492, 'y1': 1506, 'x2': 714, 'y2': 1572},
        }]
        for index, value in enumerate('1234567890浙A'):
            nodes.append({
                'resource_id': value,
                'text': value,
                'content_desc': value,
                'bounds': {
                    'x1': index * 90,
                    'y1': 1680 if index < 10 else 1845,
                    'x2': index * 90 + 54,
                    'y2': 1755 if index < 10 else 1920,
                },
            })

        region = runner._detected_license_plate_keyboard_region(nodes)

        self.assertLess(region[1], 1680)
        self.assertGreater(region[3], 1920)

    def test_license_plate_summary_is_not_detected_as_keyboard(self):
        runner = UiFlowRunner(platform='ios')
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        nodes = []
        for index, value in enumerate('浙A24D3M'):
            nodes.append({
                'resource_id': value,
                'text': value,
                'content_desc': value,
                'visible': True,
                'bounds': {
                    'x1': 136 + index * 112,
                    'y1': 1293,
                    'x2': 230 + index * 112,
                    'y2': 1428,
                },
            })
        nodes.extend([
            {
                'resource_id': '新能源',
                'text': '新能源',
                'content_desc': '新能源',
                'visible': True,
                'bounds': {'x1': 976, 'y1': 1293, 'x2': 1070, 'y2': 1428},
            },
            {
                'resource_id': '车牌号键盘',
                'text': '车牌号键盘',
                'content_desc': '车牌号键盘',
                'visible': False,
                'bounds': {'x1': 0, 'y1': 1506, 'x2': 1206, 'y2': 2622},
            },
        ])

        self.assertFalse(runner._license_plate_keyboard_visible(nodes))
        self.assertIsNone(runner._detected_license_plate_keyboard_region(nodes))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_duplicate_province_click_is_skipped_after_english_switch(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._license_plate_key_character = mock.Mock(return_value='浙')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])
        runner._license_plate_keyboard_phase = mock.Mock(return_value='alphanumeric')

        runner._action_click({
            'name': '重复点击浙',
            'selector_type': 'text',
            'selector': '浙',
        })

        touch_mock.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'license_plate_duplicate_guard',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_recorded_license_plate_key_uses_live_keyboard_lookup(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])
        runner._license_plate_keyboard_phase = mock.Mock(return_value='alphanumeric')
        runner._find_license_plate_key = mock.Mock(return_value=(
            (320, 1880),
            'semantic',
            [{'strategy': 'semantic', 'matched': True}],
        ))
        runner._sleep_interruptibly = mock.Mock()
        runner._action_touch = mock.Mock()

        runner._action_click({
            'type': 'click',
            'name': 'iOS 录制点击 10',
            'fingerprint': {
                'resource_id': 'A',
                'text': 'A',
                'content_desc': 'A',
                'class_name': 'XCUIElementTypeStaticText',
                'parent': {
                    'resource_id': '网页对话框',
                    'text': '网页对话框',
                    'content_desc': '网页对话框',
                },
            },
        })

        runner._find_license_plate_key.assert_called_once_with('A', 'alphanumeric', mock.ANY)
        touch_mock.assert_called_once_with((320, 1880))
        runner._action_touch.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'license_plate_recorded_key',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_recorded_license_plate_key_opens_keyboard_when_closed(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])
        runner._license_plate_keyboard_phase = mock.Mock(side_effect=['closed', 'province'])
        runner._open_license_plate_keyboard = mock.Mock(return_value={'opened': True})
        runner._find_license_plate_key = mock.Mock(return_value=(
            (180, 1690),
            'coordinate',
            [{'strategy': 'coordinate', 'matched': True}],
        ))
        runner._sleep_interruptibly = mock.Mock()

        runner._action_click({
            'type': 'click',
            'name': 'iOS 录制点击 9',
            'fingerprint': {
                'resource_id': '浙',
                'text': '浙',
                'content_desc': '浙',
                'class_name': 'XCUIElementTypeStaticText',
                'parent': {'text': '网页对话框'},
            },
        })

        runner._open_license_plate_keyboard.assert_called_once()
        runner._find_license_plate_key.assert_called_once_with('浙', 'province', mock.ANY)
        touch_mock.assert_called_once_with((180, 1690))
        self.assertEqual(
            runner._last_locator_diagnostics['license_plate_key']['keyboard_open'],
            {'opened': True},
        )


class UiFlowRunnerWebViewTests(SimpleTestCase):
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_click_prefers_webview_dom_and_skips_airtest_touch(self, touch_mock):
        runner = UiFlowRunner(
            device_id='emulator-5554',
            package_name='com.demo',
            platform='android',
        )
        client = mock.Mock()
        client.switch_to_webview.return_value = (
            'WEBVIEW_com.demo',
            {'matched': True},
        )
        client.click.return_value = {
            'selected_strategy': 'webview_css',
            'action': 'click',
        }
        client.switch_to_native.return_value = 'NATIVE_APP'
        runner._appium_webview = client

        runner._action_click({
            'name': '提交',
            'webview_css': '#submit',
            'context_preference': 'webview',
        })

        touch_mock.assert_not_called()
        client.click.assert_called_once()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'webview_dom',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    def test_input_prefers_webview_send_keys(self, airtest_text_mock):
        runner = UiFlowRunner(
            device_id='emulator-5554',
            package_name='com.demo',
            platform='android',
        )
        client = mock.Mock()
        client.switch_to_webview.return_value = (
            'WEBVIEW_com.demo',
            {'matched': True},
        )
        client.input_text.return_value = {
            'selected_strategy': 'webview_css',
            'action': 'input',
        }
        client.switch_to_native.return_value = 'NATIVE_APP'
        runner._appium_webview = client

        runner._action_input({
            'name': '手机号',
            'webview_css': 'input[name="mobile"]',
            'context_preference': 'webview',
            'value': '13800138000',
        })

        airtest_text_mock.assert_not_called()
        client.input_text.assert_called_once()
        self.assertEqual(client.input_text.call_args.args[1], '13800138000')

    def test_element_probe_returns_webview_locator_diagnostics(self):
        runner = UiFlowRunner(
            device_id='emulator-5554',
            package_name='com.demo',
            platform='android',
        )
        client = mock.Mock()
        client.switch_to_webview.return_value = (
            'WEBVIEW_com.demo',
            {'matched': True},
        )
        client.find_element.return_value = (
            'element-1',
            {'selected_strategy': 'webview_css'},
        )
        runner._appium_webview = client

        runner._action_element_probe({
            'webview_css': '#submit',
            'context_preference': 'webview',
            'save_as': 'probe',
        })

        result = runner.context['local']['probe']
        self.assertTrue(result['found'])
        self.assertEqual(result['element_id'], 'element-1')
        self.assertEqual(result['context'], 'WEBVIEW_com.demo')


class UiFlowRunnerInputLocatorTests(SimpleTestCase):
    @staticmethod
    def _label_node(text='详细地址'):
        return {
            'resource_id': text,
            'text': text,
            'content_desc': text,
            'class_name': 'XCUIElementTypeStaticText',
            'enabled': True,
            'visible': True,
            'bounds': {'x1': 80, 'y1': 900, 'x2': 250, 'y2': 980},
            'center': {'x': 165, 'y': 940},
            'parent': {'class_name': 'XCUIElementTypeOther'},
        }

    @staticmethod
    def _input_node():
        return {
            'resource_id': '',
            'text': '',
            'content_desc': '',
            'placeholder': '请填写包含门牌号的地址',
            'class_name': 'XCUIElementTypeTextView',
            'enabled': True,
            'visible': True,
            'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
            'center': {'x': 650, 'y': 940},
            'parent': {'class_name': 'XCUIElementTypeOther'},
        }

    def _runner_with_failed_semantic_and_position(self, nodes):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=(None, {'matched': False}))
        runner._dump_current_ui_nodes = mock.Mock(return_value=nodes)
        runner._step_locator_candidates = mock.Mock(return_value=[
            {
                'strategy': 'text',
                'kind': 'semantic',
                'fingerprint': {'text': '详细地址'},
                'minimum_score': 30,
            },
            {
                'strategy': 'position',
                'kind': 'static',
                'target': (777, 1027),
            },
        ])
        return runner

    def test_input_fallback_uses_labeled_text_control_before_coordinates(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node(),
            self._input_node(),
        ])

        target = runner._resolve_action_target({
            'type': 'smart_input',
            'name': '输入详细地址',
            'text': '详细地址',
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (650, 940))
        self.assertEqual(runner._last_locator_diagnostics['selected_strategy'], 'input_control')
        input_attempt = runner._last_locator_diagnostics['attempts'][-1]
        self.assertTrue(input_attempt['matched'])
        self.assertEqual(input_attempt['detail']['method'], 'label_association')

    def test_input_action_redirects_semantic_placeholder_to_native_control(self):
        runner = UiFlowRunner(platform='ios')
        runner._wait_for_page_stable = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._semantic_target = mock.Mock(return_value=(
            (796, -134),
            {'matched': True, 'score': 75},
        ))
        input_node = self._input_node()
        input_node.update({
            'resource_id': '单位地址',
            'content_desc': '单位地址',
            'bounds': {'x1': 483, 'y1': 2094, 'x2': 1071, 'y2': 2175},
            'center': {'x': 777, 'y': 2134},
        })
        runner._dump_current_ui_nodes = mock.Mock(return_value=[input_node])
        runner._step_locator_candidates = mock.Mock(return_value=[{
            'strategy': 'resource_id',
            'kind': 'semantic',
            'fingerprint': {
                'resource_id': '请填写包含门牌号的地址',
                'class_name': 'XCUIElementTypeStaticText',
            },
            'minimum_score': 45,
        }])

        target = runner._resolve_action_target({
            'type': 'smart_input',
            'name': '智能输入-单位地址',
            'text': '单位地址',
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (777, 2134))
        self.assertEqual(runner._last_locator_diagnostics['selected_strategy'], 'input_control')
        self.assertEqual(
            runner._last_locator_diagnostics['attempts'][-1]['detail']['method'],
            'input_identity',
        )

    def test_recorded_click_placeholder_redirects_to_native_input_control(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._input_node(),
            {
                'resource_id': '年收入',
                'text': '',
                'content_desc': '年收入',
                'placeholder': '请填写税前年收入',
                'class_name': 'XCUIElementTypeTextField',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 520, 'y1': 1180, 'x2': 980, 'y2': 1260},
                'center': {'x': 750, 'y': 1220},
                'parent': {'class_name': 'XCUIElementTypeOther'},
            },
        ])

        target = runner._resolve_action_target({
            'type': 'click',
            'name': 'iOS 录制点击 32',
            'config': {
                'selector_type': 'pos',
                'selector': '282,508',
                'fingerprint': {
                    'resource_id': '请填写包含门牌号的地址',
                    'text': '请填写包含门牌号的地址',
                    'content_desc': '请填写包含门牌号的地址',
                    'class_name': 'XCUIElementTypeStaticText',
                },
            },
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (650, 940))
        self.assertEqual(runner._last_locator_diagnostics['selected_strategy'], 'input_control')
        self.assertEqual(
            runner._last_locator_diagnostics['attempts'][-1]['detail']['method'],
            'input_identity',
        )

    def test_select_placeholder_does_not_mark_step_as_text_input(self):
        runner = UiFlowRunner(platform='ios')

        self.assertFalse(runner._step_targets_text_input({
            'type': 'click',
            'config': {
                'fingerprint': {
                    'resource_id': '请选择',
                    'text': '请选择',
                    'content_desc': '请选择',
                    'class_name': 'XCUIElementTypeStaticText',
                },
            },
        }))

    def test_input_step_blocks_coordinate_fallback_when_no_input_control_matches(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('上传身份证照片智能录入'),
        ])

        with self.assertRaisesRegex(ValueError, '已阻止坐标兜底'):
            runner._resolve_action_target({
                'type': 'smart_input',
                'name': '输入详细地址',
                'text': '详细地址',
                'timeout': 0.1,
                'guard_security_challenge': False,
            })

        self.assertEqual(
            runner._last_locator_diagnostics['blocked_reason'],
            'input-action-coordinate-fallback-disabled',
        )
        self.assertFalse(runner._last_locator_diagnostics['attempts'][-1]['matched'])

    def test_input_control_does_not_match_generic_address_for_unit_address(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('地址'),
            {
                'resource_id': '地址',
                'text': '北京市海淀区中关村南大街5号',
                'content_desc': '地址',
                'placeholder': '请输入',
                'class_name': 'XCUIElementTypeTextView',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 480, 'y1': 1960, 'x2': 1080, 'y2': 2056},
                'center': {'x': 780, 'y': 2008},
                'parent': {'class_name': 'XCUIElementTypeOther'},
            },
        ])

        with self.assertRaisesRegex(ValueError, '已阻止坐标兜底'):
            runner._resolve_action_target({
                'type': 'smart_input',
                'name': '智能输入-单位地址',
                'text': '单位地址',
                'timeout': 0.1,
                'guard_security_challenge': False,
            })

        input_attempt = next(
            item for item in runner._last_locator_diagnostics['attempts']
            if item.get('strategy') == 'input_control'
        )
        self.assertEqual(input_attempt['strategy'], 'input_control')
        self.assertFalse(input_attempt['matched'])
        self.assertEqual(
            input_attempt['detail']['reason'],
            'no-input-control-matched',
        )

    def test_input_control_allows_detail_address_rendered_as_address_label(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('地址'),
            self._input_node(),
        ])

        target = runner._resolve_action_target({
            'type': 'smart_input',
            'name': '输入详细地址',
            'text': '详细地址',
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (650, 940))
        self.assertEqual(runner._last_locator_diagnostics['selected_strategy'], 'input_control')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_input_fallback_scrolls_to_labeled_text_control(self, swipe_mock):
        runner = self._runner_with_failed_semantic_and_position([])
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [],
            [self._label_node('上传身份证照片智能录入')],
            [self._label_node(), self._input_node()],
            [self._label_node(), self._input_node()],
        ])

        target = runner._resolve_action_target({
            'type': 'smart_input',
            'name': '输入详细地址',
            'text': '详细地址',
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (650, 940))
        swipe_mock.assert_called_once()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'scroll_to_input_control',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_input_scroll_starts_above_fixed_ios_primary_button(self, swipe_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [
                {
                    'resource_id': '同意并申请',
                    'text': '同意并申请',
                    'content_desc': '同意并申请',
                    'class_name': 'XCUIElementTypeButton',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 48, 'y1': 1839, 'x2': 1158, 'y2': 1983},
                    'center': {'x': 603, 'y': 1911},
                },
            ],
            [
                self._label_node('单位名称'),
                {
                    'resource_id': '单位名称',
                    'text': '',
                    'content_desc': '单位名称',
                    'placeholder': '请填写单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                    'center': {'x': 650, 'y': 940},
                    'parent': {'class_name': 'XCUIElementTypeOther'},
                },
            ],
            [
                self._label_node('单位名称'),
                {
                    'resource_id': '单位名称',
                    'text': '',
                    'content_desc': '单位名称',
                    'placeholder': '请填写单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                    'center': {'x': 650, 'y': 940},
                    'parent': {'class_name': 'XCUIElementTypeOther'},
                },
            ],
            [
                self._label_node('单位名称'),
                {
                    'resource_id': '单位名称',
                    'text': '',
                    'content_desc': '单位名称',
                    'placeholder': '请填写单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                    'center': {'x': 650, 'y': 940},
                    'parent': {'class_name': 'XCUIElementTypeOther'},
                },
            ],
            [
                self._label_node('单位名称'),
                {
                    'resource_id': '单位名称',
                    'text': '',
                    'content_desc': '单位名称',
                    'placeholder': '请填写单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                    'center': {'x': 650, 'y': 940},
                    'parent': {'class_name': 'XCUIElementTypeOther'},
                },
            ],
        ])

        target, detail = runner._scroll_to_input_control_target({
            'type': 'smart_click',
            'name': '智能点击-单位名称',
            'text': '单位名称',
            'class_name': 'XCUIElementTypeTextField',
            'input_scroll_attempts': 1,
        })

        self.assertEqual(target, (650, 940))
        start, end = swipe_mock.call_args.args[:2]
        self.assertLess(start[1], 1839)
        self.assertLess(end[1], start[1])
        self.assertEqual(
            detail['fixed_primary_scroll_avoidance']['label'],
            '同意并申请',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_input_scroll_uses_webview_dom_scroll_when_native_swipes_miss(self, swipe_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        client = mock.Mock()
        client.switch_to_webview.return_value = ('WEBVIEW_1', {'matched': True})
        client.execute_script.return_value = {
            'matched': True,
            'source': 'label',
            'scrollY': 1280,
        }
        client.switch_to_native.return_value = 'NATIVE_APP'
        runner._get_appium_webview_client = mock.Mock(return_value=client)
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [],
            [],
            [
                self._label_node('单位名称'),
                {
                    'resource_id': '单位名称',
                    'text': '',
                    'content_desc': '单位名称',
                    'placeholder': '请填写单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                    'center': {'x': 650, 'y': 940},
                    'parent': {'class_name': 'XCUIElementTypeOther'},
                },
            ],
        ])

        target, detail = runner._scroll_to_input_control_target({
            'type': 'smart_click',
            'name': '智能点击-单位名称',
            'text': '单位名称',
            'class_name': 'XCUIElementTypeTextField',
            'input_scroll_attempts': 1,
        })

        self.assertEqual(target, (650, 940))
        client.switch_to_webview.assert_called_once()
        client.execute_script.assert_called_once()
        client.switch_to_native.assert_called_once()
        self.assertTrue(detail['webview_scroll']['matched'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_input_target_recovers_by_tapping_ios_primary_transition(self, touch_mock):
        runner = self._runner_with_failed_semantic_and_position([])
        runner._wait_for_page_stable = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._clear_optional_blocking_modal_if_needed = mock.Mock(
            return_value={'matched': False}
        )
        runner._guard_ios_input_no_blocking_modal = mock.Mock()
        runner._scroll_to_input_control_target = mock.Mock(return_value=(
            None,
            {'matched': False, 'reason': 'not-visible'},
        ))
        unit_nodes = [
            self._label_node('单位名称'),
            {
                'resource_id': '单位名称',
                'text': '',
                'content_desc': '单位名称',
                'placeholder': '请填写单位名称',
                'class_name': 'XCUIElementTypeTextField',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 320, 'y1': 900, 'x2': 980, 'y2': 980},
                'center': {'x': 650, 'y': 940},
                'parent': {'class_name': 'XCUIElementTypeOther'},
            },
        ]
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [],
            [
                {
                    'resource_id': '同意并申请',
                    'text': '同意并申请',
                    'content_desc': '同意并申请',
                    'class_name': 'XCUIElementTypeButton',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 48, 'y1': 1839, 'x2': 1158, 'y2': 1983},
                    'center': {'x': 603, 'y': 1911},
                },
            ],
            unit_nodes,
            unit_nodes,
        ])

        target = runner._resolve_action_target({
            'type': 'smart_click',
            'name': '智能点击-单位名称',
            'text': '单位名称',
            'timeout': 0.1,
            'guard_security_challenge': False,
            'class_name': 'XCUIElementTypeTextField',
        })

        self.assertEqual(target, (650, 940))
        touch_mock.assert_called_once_with((603, 1911))
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'ios_primary_transition',
        )
        transition_attempt = runner._last_locator_diagnostics['attempts'][-1]
        self.assertEqual(transition_attempt['strategy'], 'ios_primary_transition')
        self.assertTrue(transition_attempt['matched'])
        self.assertEqual(
            transition_attempt['detail']['transition']['label'],
            '同意并申请',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_picker_entry_resolve_scrolls_to_label_row_before_stale_coordinate(self, swipe_mock):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('教育'),
        ])
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [self._label_node('教育')],
            [self._label_node('教育')],
            [self._label_node('申请地区')],
        ])

        target = runner._resolve_action_target({
            'type': 'smart_click',
            'name': '智能点击申请地区',
            'timeout': 0.1,
            'guard_security_challenge': False,
        })

        self.assertEqual(target, (601, 940))
        swipe_mock.assert_called_once()
        self.assertEqual(
            runner._last_locator_diagnostics['selected_strategy'],
            'scroll_to_picker_entry',
        )
        scroll_attempt = runner._last_locator_diagnostics['attempts'][-1]
        self.assertTrue(scroll_attempt['matched'])
        self.assertEqual(
            scroll_attempt['detail']['attempts'][0]['form_select']['label'],
            '申请地区',
        )

    def test_picker_entry_resolve_blocks_stale_coordinate_after_semantic_miss(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('教育'),
        ])

        with self.assertRaisesRegex(ValueError, 'picker-entry-coordinate-fallback-disabled'):
            runner._resolve_action_target({
                'type': 'smart_click',
                'name': '智能点击申请地区',
                'timeout': 0.1,
                'guard_security_challenge': False,
                'picker_entry_scroll_attempts': 0,
            })

        self.assertEqual(
            runner._last_locator_diagnostics['blocked_reason'],
            'picker-entry-coordinate-fallback-disabled',
        )

    def test_android_textview_click_element_is_not_treated_as_text_input_on_ios(self):
        runner = UiFlowRunner(platform='ios')
        element = mock.Mock()
        element.config = {
            'text': '浙',
            'class_name': 'android.widget.TextView',
            'fingerprint': {
                'text': '浙',
                'class_name': 'android.widget.TextView',
            },
        }
        runner._load_element = mock.Mock(return_value=element)

        self.assertFalse(runner._step_targets_text_input({
            'type': 'smart_click',
            'name': '智能点击浙',
            'element_id': 86,
        }))

    def test_ios_textview_input_element_is_still_treated_as_text_input(self):
        runner = UiFlowRunner(platform='ios')

        self.assertTrue(runner._step_targets_text_input({
            'type': 'smart_click',
            'name': '单位地址',
            'class_name': 'XCUIElementTypeTextView',
        }))

    def test_input_control_prefers_semantic_target_inside_duplicate_input(self):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'resource_id': '联系人手机号',
                'text': '联系人手机号',
                'content_desc': '联系人手机号',
                'class_name': 'XCUIElementTypeStaticText',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 135, 'y1': 750, 'x2': 435, 'y2': 828},
                'center': {'x': 285, 'y': 789},
                'parent': {'class_name': 'XCUIElementTypeOther', 'section': 'one'},
            },
            {
                'resource_id': '联系人手机号',
                'text': '16665730010',
                'content_desc': '联系人手机号',
                'placeholder': '请填写联系人手机号',
                'class_name': 'XCUIElementTypeTextField',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 483, 'y1': 750, 'x2': 1071, 'y2': 834},
                'center': {'x': 777, 'y': 792},
                'parent': {'class_name': 'XCUIElementTypeOther', 'section': 'one'},
            },
            {
                'resource_id': '联系人手机号',
                'text': '联系人手机号',
                'content_desc': '联系人手机号',
                'class_name': 'XCUIElementTypeStaticText',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 135, 'y1': 1506, 'x2': 435, 'y2': 1584},
                'center': {'x': 285, 'y': 1545},
                'parent': {'class_name': 'XCUIElementTypeOther', 'section': 'two'},
            },
            {
                'resource_id': '联系人手机号',
                'text': '请填写联系人手机号',
                'content_desc': '联系人手机号',
                'placeholder': '请填写联系人手机号',
                'class_name': 'XCUIElementTypeTextField',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 483, 'y1': 1506, 'x2': 1071, 'y2': 1590},
                'center': {'x': 777, 'y': 1548},
                'parent': {'class_name': 'XCUIElementTypeOther', 'section': 'two'},
            },
        ])

        target, detail = runner._input_control_target({
            'type': 'smart_input',
            'name': '智能输入-联系人2手机号',
            'resource_id': '联系人手机号',
        }, fallback_target=(777, 1548))

        self.assertEqual(target, (777, 1548))
        self.assertEqual(detail['method'], 'coordinate_inside_input')
        self.assertEqual(detail['text'], '请填写联系人手机号')

    def test_input_control_ignores_ios_safari_address_field(self):
        runner = self._runner_with_failed_semantic_and_position([
            self._label_node('请填写'),
            {
                'resource_id': 'TabBarItemTitle',
                'text': '\u200e172.16.0.88',
                'content_desc': '',
                'placeholder': '',
                'class_name': 'XCUIElementTypeTextField',
                'enabled': True,
                'visible': True,
                'bounds': {'x1': 270, 'y1': 2380, 'x2': 940, 'y2': 2510},
                'center': {'x': 603, 'y': 2450},
                'parent': {'class_name': 'XCUIElementTypeOther'},
            },
        ])

        with self.assertRaisesRegex(ValueError, '已阻止坐标兜底'):
            runner._resolve_action_target({
                'type': 'smart_input',
                'name': '智能输入-单位地址',
                'text': '单位地址',
                'placeholder': '请填写包含门牌号的地址',
                'timeout': 0.1,
                'guard_security_challenge': False,
            })

        input_attempt = next(
            item for item in runner._last_locator_diagnostics['attempts']
            if item.get('strategy') == 'input_control'
        )
        self.assertEqual(input_attempt['strategy'], 'input_control')
        self.assertFalse(input_attempt['matched'])
        self.assertEqual(input_attempt['detail']['excluded_browser_input_count'], 1)


class UiFlowRunnerFormPostconditionTests(SimpleTestCase):
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_optional_vehicle_modal_is_closed_before_non_modal_step(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '此身份证未找到符合条件的车辆信息',
                'visible': True,
                'bounds': {'x1': 207, 'y1': 972, 'x2': 999, 'y2': 1047},
            },
            {
                'text': '上传行驶证',
                'visible': True,
                'bounds': {'x1': 150, 'y1': 1240, 'x2': 1050, 'y2': 1360},
            },
            {
                'text': '重新输入车牌号',
                'visible': True,
                'bounds': {'x1': 150, 'y1': 1460, 'x2': 1050, 'y2': 1580},
            },
        ])

        result = runner._clear_optional_blocking_modal_if_needed({
            'type': 'smart_click',
            'name': '智能点击-详细地址',
            'text': '详细地址',
        })

        self.assertTrue(result['matched'])
        self.assertEqual(result['method'], 'semantic-modal-top-right-close')
        self.assertEqual(result['target'], [1079, 656])
        touch_mock.assert_called_once_with((1079, 656))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_required_agreement_modal_returns_before_next_recorded_step(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '请先阅读并同意签署协议',
                'visible': True,
                'bounds': {'x1': 297, 'y1': 1005, 'x2': 909, 'y2': 1086},
            },
            {
                'text': '返回',
                'content_desc': '返回',
                'visible': True,
                'class_name': 'XCUIElementTypeButton',
                'bounds': {'x1': 87, 'y1': 1239, 'x2': 606, 'y2': 1395},
                'center': {'x': 346, 'y': 1317},
            },
            {
                'text': '同意签署',
                'content_desc': '同意签署',
                'visible': True,
                'class_name': 'XCUIElementTypeButton',
                'bounds': {'x1': 603, 'y1': 1239, 'x2': 1119, 'y2': 1395},
                'center': {'x': 861, 'y': 1317},
            },
        ])

        result = runner._clear_optional_blocking_modal_if_needed({
            'type': 'smart_click',
            'name': '智能点击申请地区',
        })

        self.assertTrue(result['matched'])
        self.assertEqual(result['method'], 'visible-close-node')
        self.assertEqual(result['text'], '返回')
        touch_mock.assert_called_once_with((346, 1317))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_optional_vehicle_modal_is_kept_for_modal_target_step(self, touch_mock):
        runner = UiFlowRunner(platform='ios')

        result = runner._clear_optional_blocking_modal_if_needed({
            'type': 'smart_click',
            'name': '上传行驶证',
            'text': '上传行驶证',
        })

        self.assertFalse(result['matched'])
        self.assertEqual(result['reason'], 'step-targets-modal')
        touch_mock.assert_not_called()

    def test_vehicle_verification_modal_blocks_ios_input_targets(self):
        runner = UiFlowRunner(platform='ios')
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '此身份证未找到符合条件的车辆信息',
                'visible': True,
            },
            {
                'text': '上传行驶证',
                'visible': True,
            },
        ])

        with self.assertRaisesRegex(AssertionError, 'vehicle-verification-modal'):
            runner._guard_ios_input_no_blocking_modal(
                {'type': 'smart_click', 'name': '智能点击-单位名称', 'text': '单位名称'},
                'before_locate',
            )

        self.assertEqual(
            runner._last_locator_diagnostics['input_blocked']['modal'],
            'vehicle-verification-modal',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_primary_transition_stops_on_vehicle_verification_modal(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._sleep_interruptibly = mock.Mock()
        runner._wait_for_page_stable = mock.Mock()
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [
                {
                    'resource_id': '同意并申请',
                    'text': '同意并申请',
                    'content_desc': '同意并申请',
                    'class_name': 'XCUIElementTypeButton',
                    'enabled': True,
                    'visible': True,
                    'bounds': {'x1': 48, 'y1': 1545, 'x2': 1158, 'y2': 1689},
                    'center': {'x': 603, 'y': 1617},
                },
            ],
            [
                {
                    'text': '此身份证未找到符合条件的车辆信息',
                    'visible': True,
                },
                {
                    'text': '重新输入车牌号',
                    'visible': True,
                },
            ],
        ])

        with self.assertRaisesRegex(AssertionError, 'vehicle-verification-modal'):
            runner._recover_input_target_via_primary_transition(
                {
                    'type': 'smart_click',
                    'name': '智能点击-单位名称',
                    'text': '单位名称',
                    'class_name': 'XCUIElementTypeTextField',
                },
                [],
            )

        touch_mock.assert_called_once_with((603, 1617))

    def test_confirmed_select_field_fails_when_placeholder_remains(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '贷款用途',
                'visible': True,
                'bounds': {'x1': 120, 'y1': 1520, 'x2': 330, 'y2': 1600},
            },
            {
                'text': '请选择',
                'visible': True,
                'bounds': {'x1': 840, 'y1': 1520, 'x2': 1020, 'y2': 1600},
            },
        ])

        with self.assertRaisesRegex(AssertionError, '贷款用途确认后字段仍为'):
            runner._verify_click_postcondition({
                'type': 'smart_click',
                'name': '智能点击-贷款用途-确认',
                'form_select_post_timeout': 0,
            })

    def test_confirmed_select_field_passes_when_placeholder_is_gone(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '贷款用途',
                'visible': True,
                'bounds': {'x1': 120, 'y1': 1520, 'x2': 330, 'y2': 1600},
            },
            {
                'text': '消费',
                'visible': True,
                'bounds': {'x1': 840, 'y1': 1520, 'x2': 1020, 'y2': 1600},
            },
        ])

        runner._verify_click_postcondition({
            'type': 'smart_click',
            'name': '智能点击-贷款用途-确认',
            'form_select_post_timeout': 0,
        })

    def test_confirmed_select_field_passes_when_label_and_value_share_node(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '申请地区 浙江省-杭州市-拱墅区 ',
                'visible': True,
                'bounds': {'x1': 84, 'y1': 1780, 'x2': 1122, 'y2': 1950},
            },
        ])

        runner._verify_click_postcondition({
            'type': 'smart_click',
            'name': '智能点击-申请地区-确认',
            'form_select_post_timeout': 0,
        })

    def test_confirmed_select_field_fails_when_field_is_not_confirmable(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'text': '确认',
                'visible': True,
                'bounds': {'x1': 920, 'y1': 1420, 'x2': 1120, 'y2': 1500},
            },
            {
                'text': '取消',
                'visible': True,
                'bounds': {'x1': 80, 'y1': 1420, 'x2': 260, 'y2': 1500},
            },
            {
                'text': '上传身份证照片智能录入',
                'visible': True,
                'bounds': {'x1': 260, 'y1': 760, 'x2': 930, 'y2': 840},
            },
        ])

        with self.assertRaisesRegex(AssertionError, '未在当前页面确认到字段及其已选值'):
            runner._verify_click_postcondition({
                'type': 'smart_click',
                'name': '智能点击-贷款用途-确认',
                'form_select_post_timeout': 0,
            })


class UiFlowRunnerUniversalComponentTests(SimpleTestCase):
    @staticmethod
    def _checkbox_node(value):
        return {
            'resource_id': 'agreement-checkbox',
            'text': '本人已阅读并同意',
            'content_desc': '本人已阅读并同意',
            'class_name': 'XCUIElementTypeSwitch',
            'traits': 'ToggleButton, Button',
            'value': value,
            'visible': True,
            'enabled': True,
            'bounds': {'x1': 50, 'y1': 150, 'x2': 500, 'y2': 250},
            'center': {'x': 275, 'y': 200},
        }

    def test_smart_click_routes_switch_element_to_safe_checkbox_action(self):
        runner = UiFlowRunner(platform='ios')
        element = mock.Mock()
        element.config = {
            'class_name': 'XCUIElementTypeSwitch',
            'fingerprint': {'class_name': 'XCUIElementTypeSwitch'},
        }
        runner._load_element = mock.Mock(return_value=element)
        runner._action_checkbox_toggle = mock.Mock()
        runner._action_click = mock.Mock()

        runner._action_smart_click({'element_id': 141, 'name': '智能点击'})

        runner._action_click.assert_not_called()
        checkbox_step = runner._action_checkbox_toggle.call_args.args[0]
        self.assertEqual(checkbox_step['desired_state'], 'checked')

    def test_smart_click_keeps_normal_button_click_path(self):
        runner = UiFlowRunner(platform='ios')
        element = mock.Mock()
        element.config = {'class_name': 'XCUIElementTypeButton'}
        runner._load_element = mock.Mock(return_value=element)
        runner._action_checkbox_toggle = mock.Mock()
        runner._action_click = mock.Mock()
        step = {'element_id': 126, 'name': '同意并申请'}

        runner._action_smart_click(step)

        runner._action_checkbox_toggle.assert_not_called()
        runner._action_click.assert_called_once_with(step)

    def test_ios_wide_switch_click_target_uses_leading_checkbox(self):
        runner = UiFlowRunner(platform='ios')
        node = self._checkbox_node('0')
        node['bounds'] = {'x1': 96, 'y1': 2016, 'x2': 1107, 'y2': 2145}
        node['center'] = {'x': 602, 'y': 2080}

        target = runner._checkbox_click_target(node, (602, 2080))

        self.assertEqual(target, (122, 2048))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_checkbox_component_checks_unchecked_switch_and_verifies(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock(return_value=(100, 200))
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [self._checkbox_node('0')],
            [self._checkbox_node('1')],
        ])

        runner._action_checkbox_toggle({
            'selector_type': 'pos',
            'selector': '100,200',
            'desired_state': 'checked',
            'verify_state': True,
            'save_as': 'agreement_checked',
        })

        touch_mock.assert_called_once_with((70, 175))
        result = runner.context['local']['agreement_checked']
        self.assertFalse(result['before'])
        self.assertTrue(result['after'])
        self.assertTrue(result['verified'])
        self.assertEqual(result['target'], [70, 175])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_checkbox_dismisses_keyboard_before_clicking_switch(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._dismiss_ios_system_keyboard = mock.Mock(return_value={
            'dismissed': True,
            'method': 'wda_keyboard_dismiss',
        })
        runner._resolve_action_target = mock.Mock(return_value=(602, 1514))
        obscured_checkbox = self._checkbox_node('0')
        obscured_checkbox['bounds'] = {
            'x1': 96, 'y1': 1449, 'x2': 1107, 'y2': 1578,
        }
        obscured_checkbox['center'] = {'x': 602, 'y': 1514}
        visible_checkbox = self._checkbox_node('0')
        visible_checkbox['bounds'] = {
            'x1': 96, 'y1': 2016, 'x2': 1107, 'y2': 2145,
        }
        visible_checkbox['center'] = {'x': 602, 'y': 2080}
        checked_checkbox = dict(visible_checkbox, value='1')
        keyboard = {
            'class_name': 'XCUIElementTypeKeyboard',
            'resource_id': '车牌号键盘',
            'text': '车牌号键盘',
            'content_desc': '车牌号键盘',
            'visible': True,
            'bounds': {'x1': 0, 'y1': 1482, 'x2': 1206, 'y2': 2328},
        }
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [obscured_checkbox, keyboard],
            [visible_checkbox],
            [checked_checkbox],
        ])

        runner._action_checkbox_toggle({
            'selector_type': 'image',
            'selector': 'agreement.png',
            'class_name': 'XCUIElementTypeSwitch',
            'desired_state': 'checked',
            'verify_state': True,
        })

        runner._dismiss_ios_system_keyboard.assert_called_once_with(context='复选框')
        touch_mock.assert_called_once_with((122, 2048))
        result = runner.context['outputs']['last']
        self.assertTrue(result['verified'])
        self.assertTrue(result['keyboard_dismissal']['dismissed'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_native_checkbox_cannot_pass_when_state_is_unreadable(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock(return_value=(602, 1514))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])

        with self.assertRaisesRegex(AssertionError, '点击前后均未读取到状态'):
            runner._action_checkbox_toggle({
                'selector_type': 'image',
                'selector': 'agreement.png',
                'class_name': 'XCUIElementTypeSwitch',
                'desired_state': 'checked',
                'verify_state': True,
                'state_timeout': 0.01,
                'retry_interval': 0.01,
            })

        touch_mock.assert_called_once_with((602, 1514))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_checkbox_component_does_not_click_when_state_already_matches(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock(return_value=(100, 200))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            self._checkbox_node('1'),
        ])

        runner._action_checkbox_toggle({
            'selector_type': 'pos',
            'selector': '100,200',
            'desired_state': 'checked',
        })

        touch_mock.assert_not_called()
        result = runner.context['outputs']['last']
        self.assertFalse(result['clicked'])
        self.assertTrue(result['verified'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_checkbox_component_text_locator_clicks_control_not_label(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock()
        node = self._checkbox_node('0')
        runner._dump_current_ui_nodes = mock.Mock(side_effect=[
            [node],
            [self._checkbox_node('1')],
        ])

        runner._action_checkbox_toggle({
            'selector_type': 'text',
            'selector': '本人已阅读并同意',
            'desired_state': 'checked',
            'verify_state': True,
        })

        runner._resolve_action_target.assert_not_called()
        touch_mock.assert_called_once_with((70, 175))
        result = runner.context['outputs']['last']
        self.assertTrue(result['verified'])
        self.assertEqual(result['target'], [70, 175])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_checkbox_component_refuses_unresolved_text_target(self, touch_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock(return_value=(600, 2080))
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])

        with self.assertRaisesRegex(ValueError, '拒绝点击旁边文字或协议链接'):
            runner._action_checkbox_toggle({
                'selector_type': 'text',
                'selector': '隐私政策',
                'desired_state': 'checked',
            })

        touch_mock.assert_not_called()

    def test_direct_id_selector_builds_semantic_candidate(self):
        runner = UiFlowRunner()

        candidates = runner._step_locator_candidates({
            'selector_type': 'id',
            'selector': 'com.demo:id/login',
        })

        self.assertEqual(candidates[0]['strategy'], 'resource_id')
        self.assertEqual(
            candidates[0]['fingerprint']['resource_id'],
            'com.demo:id/login',
        )

    def test_missing_element_reference_falls_back_to_inline_position(self):
        runner = UiFlowRunner()
        runner._load_element = mock.Mock(return_value=None)
        runner._get_current_resolution = mock.Mock(return_value=(1206, 2622))

        candidates = runner._step_locator_candidates({
            'name': '联系人1手机号',
            'element_id': 225,
            'selector_type': 'pos',
            'selector': '777, 366',
            'locator_strategies': [],
        })

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['strategy'], 'position')
        self.assertEqual(candidates[0]['target'], (777, 366))

    def test_smart_input_normalizes_financial_and_identity_values(self):
        self.assertEqual(
            UiFlowRunner._normalize_smart_input_value('6222 0200-1234', 'bank_card'),
            '622202001234',
        )
        self.assertEqual(
            UiFlowRunner._normalize_smart_input_value('￥1,234.567', 'money'),
            '1234.56',
        )
        self.assertEqual(
            UiFlowRunner._normalize_smart_input_value('  浙a12345 ', 'license_plate'),
            '浙A12345',
        )

    def test_smart_input_auto_infers_type_from_field_semantics_and_value(self):
        self.assertEqual(
            UiFlowRunner._infer_smart_input_kind(
                {'name': '支付金额', 'input_kind': 'auto'}, '￥1,234.50'
            ),
            'money',
        )
        self.assertEqual(
            UiFlowRunner._infer_smart_input_kind(
                {'selector': 'mobile_phone', 'input_kind': 'auto'}, '13800138000'
            ),
            'phone',
        )
        self.assertEqual(
            UiFlowRunner._infer_smart_input_kind(
                {'name': '车牌号码', 'input_kind': 'auto'}, '浙a12345'
            ),
            'license_plate',
        )

    def test_smart_license_plate_input_routes_custom_control_to_plate_component(self):
        runner = UiFlowRunner(platform='ios')
        runner._license_plate_target_is_native_text = mock.Mock(return_value=False)
        runner._action_license_plate_input = mock.Mock()
        runner._action_input = mock.Mock()

        runner._action_smart_input({
            'name': '输入车牌号码',
            'selector_type': 'text',
            'selector': '车牌号',
            'value': ' 浙a12345 ',
            'input_kind': 'auto',
        })

        runner._action_license_plate_input.assert_called_once()
        routed = runner._action_license_plate_input.call_args.args[0]
        self.assertEqual(routed['plate'], '浙A12345')
        runner._action_input.assert_not_called()

    def test_direct_smart_locator_strategies_keep_configured_fallback_order(self):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1000, 2000))

        candidates = runner._step_locator_candidates({
            'locator_strategies': [
                {'type': 'resource_id', 'value': 'com.demo:id/login'},
                {'type': 'ocr', 'value': '登录'},
                {'type': 'position', 'value': {'x': 0.5, 'y': 0.8}},
            ],
        })

        self.assertEqual(
            [item['strategy'] for item in candidates],
            ['resource_id', 'ocr', 'position'],
        )
        self.assertEqual(candidates[-1]['target'], (500, 1600))

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_native_input_honors_clear_first(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='android')
        runner._resolve_action_target = mock.Mock(return_value=(200, 300))
        runner._clear_focused_text = mock.Mock(return_value={'cleared': True})
        runner._sleep_interruptibly = mock.Mock()

        runner._action_input({
            'selector_type': 'text',
            'selector': '手机号',
            'value': '13800138000',
            'clear_first': True,
        })

        touch_mock.assert_called_once_with((200, 300))
        runner._clear_focused_text.assert_called_once_with(128)
        text_mock.assert_called_once_with('13800138000', enter=False)

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_input_does_not_send_implicit_enter_and_verifies_value(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(259, 369))
        runner._clear_focused_text = mock.Mock(return_value={'cleared': True})
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[{
            'resource_id': '手机',
            'content_desc': '手机',
            'class_name': 'XCUIElementTypeTextField',
            'value': '13800138000',
            'placeholder': '请输入手机号',
            'focused': True,
            'bounds': {'x1': 161, 'y1': 356, 'x2': 357, 'y2': 383},
        }])

        runner._action_input({
            'selector_type': 'text',
            'selector': '手机',
            'value': '13800138000',
            'clear_first': True,
        })

        touch_mock.assert_called_once_with((259, 369))
        text_mock.assert_called_once_with('13800138000', enter=False)
        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_input_fails_when_placeholder_remains(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(259, 369))
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[{
            'resource_id': '手机',
            'content_desc': '手机',
            'class_name': 'XCUIElementTypeTextField',
            'value': '请输入手机号',
            'placeholder': '请输入手机号',
            'focused': True,
            'bounds': {'x1': 161, 'y1': 356, 'x2': 357, 'y2': 383},
        }])

        with self.assertRaisesRegex(AssertionError, 'iOS 输入校验失败'):
            runner._action_input({
                'selector_type': 'text',
                'selector': '手机',
                'value': '13800138000',
                'input_verify_timeout': 0.1,
            })

        text_mock.assert_called_once_with('13800138000', enter=False)
        self.assertTrue(
            runner._last_locator_diagnostics['input_verification']['placeholder_visible']
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_input_verification_rejects_wrong_focused_field_identity(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(776, 906))
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'resource_id': '姓名',
                'content_desc': '姓名',
                'class_name': 'XCUIElementTypeTextField',
                'value': '1234施哲鼎',
                'placeholder': '',
                'focused': True,
                'bounds': {'x1': 483, 'y1': 850, 'x2': 1071, 'y2': 960},
            },
            {
                'resource_id': '地址',
                'content_desc': '地址',
                'class_name': 'XCUIElementTypeTextView',
                'value': '北京市海淀区中关村南大街5号',
                'placeholder': '请输入',
                'focused': False,
                'bounds': {'x1': 483, 'y1': 1220, 'x2': 1071, 'y2': 1320},
            },
        ])

        with self.assertRaisesRegex(AssertionError, 'iOS 输入校验失败'):
            runner._action_input({
                'selector_type': 'text',
                'selector': '详细地址',
                'text': '详细地址',
                'value': '1234',
                'input_verify_timeout': 0.1,
            })

        text_mock.assert_called_once_with('1234', enter=False)
        self.assertNotEqual(
            runner._last_locator_diagnostics['input_verification']['field'],
            '姓名',
        )

    def test_plain_input_value_does_not_match_by_substring(self):
        self.assertFalse(UiFlowRunner._input_value_matches({
            'resource_id': '姓名',
            'class_name': 'XCUIElementTypeTextField',
            'value': '1234施哲鼎',
        }, '1234'))

    def test_ios_input_verification_allows_identity_scoped_inserted_text(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[{
            'resource_id': '地址',
            'content_desc': '地址',
            'class_name': 'XCUIElementTypeTextView',
            'value': '1234中关村南大街5号',
            'placeholder': '请输入',
            'focused': True,
            'bounds': {'x1': 429, 'y1': 1218, 'x2': 1122, 'y2': 1299},
        }])

        runner._verify_ios_input({
            'selector_type': 'text',
            'selector': '详细地址',
            'text': '详细地址',
        }, '1234', (776, 1258))

        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])
        self.assertEqual(
            runner._last_locator_diagnostics['input_verification']['field'],
            '地址',
        )

    def test_ios_input_verification_prefers_target_input_when_duplicate_fields_exist(self):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {
                'resource_id': '联系人手机号',
                'content_desc': '联系人手机号',
                'text': '请填写联系人手机号',
                'class_name': 'XCUIElementTypeTextField',
                'value': '请填写联系人手机号',
                'placeholder': '请填写联系人手机号',
                'bounds': {'x1': 483, 'y1': 810, 'x2': 1071, 'y2': 891},
            },
            {
                'resource_id': '联系人手机号',
                'content_desc': '联系人手机号',
                'text': '17727425941',
                'class_name': 'XCUIElementTypeTextField',
                'value': '17727425941',
                'placeholder': '请填写联系人手机号',
                'bounds': {'x1': 483, 'y1': 1566, 'x2': 1071, 'y2': 1647},
            },
        ])

        runner._verify_ios_input({
            'selector_type': 'pos',
            'selector': '257,612',
            'text': '联系人手机号',
            'fingerprint': {
                'resource_id': '联系人手机号',
                'text': '请填写联系人手机号',
                'content_desc': '联系人手机号',
                'class_name': 'XCUIElementTypeTextField',
            },
        }, '17727425941', (777, 1606))

        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])
        self.assertEqual(
            runner._last_locator_diagnostics['input_verification']['value_length'],
            11,
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    def test_ios_recorded_input_name_does_not_block_focused_field_verification(self, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[{
            'resource_id': '手机',
            'content_desc': '手机',
            'class_name': 'XCUIElementTypeTextField',
            'value': '18877570423',
            'placeholder': '请输入手机号',
            'focused': True,
            'bounds': {'x1': 483, 'y1': 1650, 'x2': 1071, 'y2': 1731},
        }])

        runner._action_input({
            'type': 'input',
            'name': 'iOS 录制输入 3',
            'selector_type': 'image',
            'selector': '',
            'value': '18877570423',
            'clear_first': True,
        })

        self.assertEqual(text_mock.call_args_list[-1], mock.call('18877570423', enter=False))
        runner._resolve_action_target.assert_not_called()
        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])
        self.assertEqual(
            runner._last_locator_diagnostics['input_verification']['field'],
            '手机',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_recorded_input_inherits_preceding_tapped_input_locator(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._clear_restored_form_state_if_needed = mock.Mock(return_value={'matched': False})
        runner._verify_click_postcondition = mock.Mock()
        runner._resolve_action_target = mock.Mock(side_effect=[
            (684, 1430),
            (684, 1430),
        ])
        runner._sleep_interruptibly = mock.Mock()
        runner._guard_ios_input_no_blocking_modal = mock.Mock()
        runner._clear_focused_text = mock.Mock()
        runner._verify_ios_input = mock.Mock()

        runner._action_touch({
            'type': 'click',
            'name': 'iOS 录制点击 5',
            'selector_type': 'pos',
            'selector': '244,476',
            'normalized_position': {'x': 0.606965, 'y': 0.544622},
            'fingerprint': {
                'resource_id': '验证码',
                'text': '请输入验证码',
                'content_desc': '验证码',
                'class_name': 'XCUIElementTypeTextField',
            },
        })

        runner._action_input({
            'type': 'input',
            'name': 'iOS 录制输入 6',
            'selector_type': 'image',
            'selector': '',
            'value': '123456',
            'clear_first': True,
        })

        input_target_step = runner._resolve_action_target.call_args_list[1].args[0]
        self.assertEqual(input_target_step['selector_type'], 'pos')
        self.assertEqual(input_target_step['selector'], '244,476')
        self.assertEqual(input_target_step['fingerprint']['content_desc'], '验证码')
        self.assertTrue(input_target_step['prefer_input_control'])
        self.assertIn('_inherited_ios_recording_input_target', input_target_step)
        self.assertEqual(touch_mock.call_args_list, [
            mock.call((684, 1430)),
            mock.call((684, 1430)),
        ])
        runner._clear_focused_text.assert_called_once_with(128)
        text_mock.assert_called_once_with('123456', enter=False)
        runner._verify_ios_input.assert_called_once_with(
            mock.ANY,
            '123456',
            (684, 1430),
        )
        self.assertIsNone(runner._last_ios_recorded_input_target_config)

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_named_empty_selector_input_keeps_previous_text_field_through_suggestion_click(
        self,
        touch_mock,
        text_mock,
    ):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._clear_restored_form_state_if_needed = mock.Mock(return_value={'matched': False})
        runner._verify_click_postcondition = mock.Mock()
        runner._resolve_action_target = mock.Mock(side_effect=[
            (684, 1430),
            (930, 1430),
            (684, 1430),
        ])
        runner._sleep_interruptibly = mock.Mock()
        runner._guard_ios_input_no_blocking_modal = mock.Mock()
        runner._clear_focused_text = mock.Mock()
        runner._verify_ios_input = mock.Mock()

        runner._action_touch({
            'type': 'click',
            'name': 'iOS 录制点击 2',
            'selector_type': 'pos',
            'selector': '244,382',
            'fingerprint': {
                'resource_id': '请填写包含门牌号的地址',
                'text': '请填写包含门牌号的地址',
                'content_desc': '请填写包含门牌号的地址',
                'class_name': 'XCUIElementTypeTextField',
            },
        })
        runner._action_touch({
            'type': 'click',
            'name': 'iOS 录制点击 1',
            'selector_type': 'pos',
            'selector': '347,380',
            'fingerprint': {
                'resource_id': '体育中心',
                'text': '体育中心',
                'content_desc': '体育中心',
                'class_name': 'XCUIElementTypeStaticText',
            },
        })
        runner._action_input({
            'type': 'input',
            'name': '单位地址',
            'selector_type': 'image',
            'selector': '',
            'value': '杭州体育中心',
            'clear_first': True,
            'send_enter': True,
        })

        input_target_step = runner._resolve_action_target.call_args_list[2].args[0]
        self.assertEqual(input_target_step['selector_type'], 'pos')
        self.assertEqual(input_target_step['selector'], '244,382')
        self.assertEqual(input_target_step['fingerprint']['content_desc'], '请填写包含门牌号的地址')
        self.assertEqual(touch_mock.call_args_list, [
            mock.call((684, 1430)),
            mock.call((930, 1430)),
            mock.call((684, 1430)),
        ])
        self.assertEqual(text_mock.call_args_list, [
            mock.call('杭州体育中心', enter=False),
            mock.call('', enter=True),
        ])
        runner._verify_ios_input.assert_called_once_with(
            mock.ANY,
            '杭州体育中心',
            (684, 1430),
        )
        self.assertIsNone(runner._last_ios_recorded_input_target_config)

    @mock.patch('apps.app_automation.runners.ui_flow_runner.G')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.OCR_AVAILABLE', True)
    def test_ios_recorded_input_verification_uses_ocr_when_wda_has_no_input_node(self, airtest_g):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])
        runner._get_ocr_helper = mock.Mock()
        runner._get_ocr_helper.return_value.recognize_text.return_value = (
            '输入信息 获取评测额度 手机 18877570423 验证码 点击发送'
        )
        airtest_g.DEVICE.snapshot.return_value = object()

        runner._verify_ios_input({
            'type': 'input',
            'name': 'iOS 录制输入 3',
            'selector_type': 'image',
            'selector': '',
        }, '18877570423', None)

        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])
        self.assertEqual(
            runner._last_locator_diagnostics['input_verification']['method'],
            'screen_ocr',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.G')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.OCR_AVAILABLE', True)
    def test_ios_exploratory_input_verification_uses_ocr_when_wda_value_unavailable(self, airtest_g):
        runner = UiFlowRunner(platform='ios')
        runner.ensure_not_stopped = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[])
        runner._get_ocr_helper = mock.Mock()
        runner._get_ocr_helper.return_value.recognize_text.return_value = (
            '联系人姓名 runnergo-test 年收入 172.160889527 万元'
        )
        airtest_g.DEVICE.snapshot.return_value = object()

        runner._verify_ios_input({
            'type': 'input',
            'name': '探索输入：请填写',
            'selector_type': 'pos',
            'selector': [777, 505],
            'exploratory': True,
        }, 'runnergo-test', (777, 505))

        self.assertTrue(runner._last_locator_diagnostics['input_verification']['matched'])
        self.assertEqual(
            runner._last_locator_diagnostics['input_verification']['method'],
            'screen_ocr',
        )

    def test_ios_recorded_input_ocr_allows_one_missing_cjk_character(self):
        detail = UiFlowRunner._recorded_input_ocr_match_detail(
            '西湖景区',
            '详细 西湖 景 婚姻 状况 教育 程度',
        )

        self.assertTrue(detail['matched'])
        self.assertEqual(detail['match_mode'], 'cjk_ordered_similarity')
        self.assertEqual(detail['matched_chars'], 3)

    def test_ios_recorded_input_ocr_rejects_weak_cjk_similarity(self):
        detail = UiFlowRunner._recorded_input_ocr_match_detail(
            '西湖景区',
            '详细 湖 区 婚姻 状况 教育 程度',
        )

        self.assertFalse(detail['matched'])
        self.assertEqual(detail['match_mode'], 'none')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_input_blocks_when_picker_dialog_is_open(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._try_webview_action = mock.Mock(return_value=None)
        runner._resolve_action_target = mock.Mock(return_value=(776, 2008))
        runner._sleep_interruptibly = mock.Mock()
        runner._dump_current_ui_nodes = mock.Mock(return_value=[
            {'text': '取消', 'class_name': 'XCUIElementTypeButton', 'visible': True},
            {'text': '确认', 'class_name': 'XCUIElementTypeButton', 'visible': True},
            {'value': '汉', 'class_name': 'XCUIElementTypePickerWheel', 'visible': True},
        ])

        with self.assertRaisesRegex(AssertionError, 'iOS 输入被阻止'):
            runner._action_input({
                'selector_type': 'text',
                'selector': '详细地址',
                'value': '1234',
            })

        runner._resolve_action_target.assert_not_called()
        touch_mock.assert_not_called()
        text_mock.assert_not_called()
        self.assertEqual(
            runner._last_locator_diagnostics['input_blocked']['modal'],
            'picker-dialog',
        )

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    def test_ios_clear_does_not_append_enter(self, text_mock):
        runner = UiFlowRunner(platform='ios')

        result = runner._clear_focused_text(3)

        text_mock.assert_called_once_with('\b\b\b', enter=False)
        self.assertTrue(result['cleared'])

    @mock.patch('apps.app_automation.runners.ui_flow_runner.airtest_text')
    @mock.patch('apps.app_automation.runners.ui_flow_runner.touch')
    def test_ios_send_enter_happens_after_value_verification(self, touch_mock, text_mock):
        runner = UiFlowRunner(platform='ios')
        runner._resolve_action_target = mock.Mock(return_value=(777, 1108))
        runner._verify_ios_input = mock.Mock()
        runner._verify_ios_input.side_effect = lambda *_args: self.assertEqual(
            text_mock.call_args_list,
            [mock.call('13800138000', enter=False)],
        )
        runner._sleep_interruptibly = mock.Mock()

        runner._action_input({
            'selector_type': 'text',
            'selector': '手机',
            'value': '13800138000',
            'send_enter': True,
        })

        self.assertEqual(
            text_mock.call_args_list,
            [mock.call('13800138000', enter=False), mock.call('', enter=True)],
        )
        runner._verify_ios_input.assert_called_once_with(
            mock.ANY, '13800138000', (777, 1108)
        )

    def test_page_detect_classifies_login_page_from_ui_tree(self):
        runner = UiFlowRunner()
        runner._current_page_state = mock.Mock(return_value={
            'type': 'native', 'package': 'com.demo', 'activity': '.LoginActivity',
        })
        runner._last_page_nodes = [
            {'text': '手机号', 'content_desc': '', 'resource_id': 'mobile'},
            {'text': '登录', 'content_desc': '', 'resource_id': 'login'},
        ]

        runner._action_page_detect({'save_as': 'page'})

        result = runner.context['local']['page']
        self.assertEqual(result['page_name'], 'login')
        self.assertGreater(result['page_confidence'], 0)

    def test_coordinate_fallback_is_blocked_on_security_challenge(self):
        runner = UiFlowRunner()
        runner._wait_for_page_stable = mock.Mock()
        runner._step_locator_candidates = mock.Mock(return_value=[{
            'strategy': 'position', 'kind': 'static', 'target': (100, 200),
        }])
        runner._current_page_state = mock.Mock(return_value={
            'type': 'webview',
            'package': 'com.demo',
            'activity': 'YodaRouterTransparentActivity',
        })
        runner._detect_semantic_page = mock.Mock(return_value={
            'page_name': 'security_verify',
            'page_label': '安全验证页',
        })
        runner._get_current_resolution = mock.Mock(return_value=(1080, 2400))

        with self.assertRaisesRegex(RuntimeError, '已阻止坐标兜底点击'):
            runner._resolve_action_target({'name': '点击美食', 'timeout': 0.1})

        self.assertEqual(
            runner._last_locator_diagnostics['blocked_reason'],
            'security-challenge-coordinate-fallback',
        )

    def test_page_assert_supports_webview_and_activity(self):
        runner = UiFlowRunner()
        runner._current_page_state = mock.Mock(return_value={
            'type': 'webview',
            'package': 'com.demo',
            'activity': 'com.demo.MainActivity',
        })

        runner._action_assert_page({
            'page_type': 'webview',
            'expected_package': 'com.demo',
            'expected_activity': 'MainActivity',
            'match_mode': 'contains',
        })

        self.assertEqual(runner.context['outputs']['last']['type'], 'webview')

    @mock.patch('apps.app_automation.runners.ui_flow_runner.swipe')
    def test_directional_swipe_uses_current_resolution(self, swipe_mock):
        runner = UiFlowRunner()
        runner._get_current_resolution = mock.Mock(return_value=(1000, 2000))
        runner._wait_for_page_stable = mock.Mock()

        runner._action_swipe({
            'direction': 'up',
            'distance': 0.5,
            'duration': 0.4,
        })

        swipe_mock.assert_called_once_with((500, 1500), (500, 500), duration=0.4)


class UiFlowRunnerCompositeComponentTests(SimpleTestCase):
    def test_fill_form_reuses_smart_input_and_masks_password_metadata(self):
        runner = UiFlowRunner()
        runner._input_component_target = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()

        runner._action_fill_form({
            'fields': [
                {
                    'name': '手机号',
                    'value': '13800138000',
                    'input_kind': 'phone',
                    'target': {'selector_type': 'id', 'selector': 'mobile'},
                },
                {
                    'name': '密码',
                    'value': 'secret',
                    'input_kind': 'password',
                    'target': {'selector_type': 'id', 'selector': 'password'},
                },
            ],
            'save_as': 'form_result',
        })

        self.assertEqual(runner._input_component_target.call_count, 2)
        self.assertTrue(runner._input_component_target.call_args_list[1].args[-1])
        result = runner.context['local']['form_result']
        self.assertEqual(result['field_count'], 2)
        self.assertNotIn('secret', str(result))

    def test_verification_code_login_runs_get_code_input_and_submit(self):
        runner = UiFlowRunner()
        runner._input_component_target = mock.Mock()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._sleep_interruptibly = mock.Mock()

        runner._action_login({
            'mode': 'verification_code',
            'account': '13800138000',
            'verification_code': '123456',
            'account_target': {'selector_type': 'id', 'selector': 'mobile'},
            'get_code_target': {'selector_type': 'text', 'selector': '获取验证码'},
            'verification_code_target': {'selector_type': 'id', 'selector': 'code'},
            'submit_target': {'selector_type': 'text', 'selector': '登录'},
            'save_as': 'login_result',
        })

        self.assertEqual(runner._input_component_target.call_count, 2)
        self.assertEqual(runner._click_component_target.call_count, 2)
        self.assertEqual(runner.context['local']['login_result']['mode'], 'verification_code')

    def test_create_order_composes_specs_address_and_submit(self):
        runner = UiFlowRunner()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._click_target_list = mock.Mock(return_value=2)
        runner._action_fill_form = mock.Mock()
        runner._probe_component_target = mock.Mock(return_value={'found': True})

        runner._action_create_order({
            'spec_targets': [{'selector': '红色'}, {'selector': 'L'}],
            'buy_target': {'selector': '立即购买'},
            'address_fields': [{'name': '姓名', 'value': '测试用户'}],
            'submit_order_target': {'selector': '提交订单'},
            'order_success_target': {'selector': '待支付'},
            'save_as': 'order_result',
        })

        runner._action_fill_form.assert_called_once()
        self.assertEqual(runner._click_target_list.call_count, 1)
        self.assertEqual(runner.context['local']['order_result']['selected_specs'], 2)
        self.assertEqual(runner.context['local']['order_result']['address_field_count'], 1)

    def test_comment_video_composes_entry_input_and_submit(self):
        runner = UiFlowRunner()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._input_component_target = mock.Mock()

        runner._action_comment_video({
            'comment': '自动化评论',
            'comment_entry_target': {'selector': '评论'},
            'comment_input_target': {'selector': '说点什么'},
            'submit_target': {'selector': '发送'},
            'save_as': 'comment_result',
        })

        self.assertEqual(runner._click_component_target.call_count, 2)
        runner._input_component_target.assert_called_once()
        self.assertEqual(runner.context['local']['comment_result']['comment_length'], 5)

    def test_verification_code_component_requests_waits_inputs_and_submits(self):
        runner = UiFlowRunner()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._input_component_target = mock.Mock()
        runner._sleep_interruptibly = mock.Mock()

        runner._action_verify_code({
            'verification_code': '123456',
            'code_wait': 3,
            'get_code_target': {'selector': '获取验证码'},
            'verification_code_target': {'selector': '验证码'},
            'submit_target': {'selector': '确定'},
            'save_as': 'code_result',
        })

        self.assertEqual(runner._click_component_target.call_count, 2)
        runner._sleep_interruptibly.assert_called_once_with(3.0)
        runner._input_component_target.assert_called_once()
        self.assertEqual(runner.context['local']['code_result']['code_length'], 6)

    def test_payment_flow_defaults_to_safe_dry_run(self):
        runner = UiFlowRunner()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._click_target_list = mock.Mock(return_value=1)
        runner._input_component_target = mock.Mock()

        runner._action_payment_flow({
            'method_targets': [{'selector': '银行卡'}],
            'payment_password': '123456',
            'password_target': {'selector': '支付密码'},
            'confirm_payment_target': {'selector': '确认支付'},
            'allow_real_payment': False,
            'save_as': 'payment_result',
        })

        result = runner.context['local']['payment_result']
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['submitted'])
        clicked_names = [call.args[2] for call in runner._click_component_target.call_args_list]
        self.assertNotIn('确认支付', clicked_names)

    def test_identity_verify_composes_standard_fields_and_face(self):
        runner = UiFlowRunner()
        runner._action_fill_form = mock.Mock()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._probe_component_target = mock.Mock(return_value={'found': True})

        runner._action_identity_verify({
            'full_name': '测试用户',
            'full_name_target': {'selector': '姓名'},
            'id_card': '330000000000000000',
            'id_card_target': {'selector': '身份证'},
            'face_target': {'selector': '人脸认证'},
            'submit_target': {'selector': '提交'},
            'save_as': 'identity_result',
        })

        fields = runner._action_fill_form.call_args.args[0]['fields']
        self.assertEqual(len(fields), 2)
        self.assertTrue(runner.context['local']['identity_result']['face_started'])

    def test_send_message_composes_input_attachment_and_send(self):
        runner = UiFlowRunner()
        runner._click_component_target = mock.Mock(return_value=True)
        runner._input_component_target = mock.Mock()
        runner._probe_component_target = mock.Mock(return_value=None)

        runner._action_send_message({
            'conversation_target': {'selector': '张三'},
            'message': '自动化消息',
            'message_input_target': {'selector': '输入消息'},
            'attachment_target': {'selector': '图片'},
            'send_target': {'selector': '发送'},
            'save_as': 'message_result',
        })

        runner._input_component_target.assert_called_once()
        self.assertEqual(runner._click_component_target.call_count, 3)
        self.assertTrue(runner.context['local']['message_result']['sent'])

    def test_upload_video_composes_media_selection_edit_and_publish(self):
        runner = UiFlowRunner()
        runner._push_media_to_device = mock.Mock(return_value={'pushed': True})
        runner._click_component_target = mock.Mock(return_value=True)
        runner._click_target_list = mock.Mock(return_value=2)
        runner._input_component_target = mock.Mock()
        runner._probe_component_target = mock.Mock(return_value={'found': True})

        runner._action_upload_video({
            'media_path': '/tmp/demo.mp4',
            'media_target': {'selector': 'demo.mp4'},
            'edit_targets': [{'selector': '下一步'}, {'selector': '完成'}],
            'caption': '自动化发布',
            'caption_target': {'selector': '作品描述'},
            'publish_target': {'selector': '发布'},
            'save_as': 'video_upload_result',
        })

        result = runner.context['local']['video_upload_result']
        self.assertTrue(result['published'])
        self.assertEqual(result['edit_step_count'], 2)


class UiFlowRunnerVisualHealingTests(SimpleTestCase):
    def test_visual_locator_falls_back_from_semantic_to_ocr(self):
        runner = UiFlowRunner()
        runner._semantic_target = mock.Mock(return_value=(None, {'matched': False}))
        runner._ocr_target = mock.Mock(return_value=((320, 640), {'matched': True}))
        runner._get_current_resolution = mock.Mock(return_value=(1080, 1920))

        target, diagnostics = runner._visual_locate_target({
            'description': '确认支付',
            'strategy_order': ['semantic', 'ocr'],
            'timeout': 1,
        })

        self.assertEqual(target, (320, 640))
        self.assertEqual(diagnostics['selected_strategy'], 'ocr')
        self.assertFalse(diagnostics['attempts'][0]['matched'])
        self.assertTrue(diagnostics['attempts'][1]['matched'])

    def test_self_heal_report_detects_fallback_without_database_write(self):
        runner = UiFlowRunner()

        detail = runner._record_self_heal(
            {'self_heal_mode': 'report'},
            'ocr',
            [
                {'strategy': 'resource_id', 'matched': False},
                {'strategy': 'ocr', 'matched': True},
            ],
        )

        self.assertTrue(detail['triggered'])
        self.assertFalse(detail['persisted'])
        self.assertEqual(detail['from_strategy'], 'resource_id')
        self.assertEqual(detail['to_strategy'], 'ocr')

    @mock.patch('apps.app_automation.models.AppElement.objects.filter')
    def test_self_heal_persist_promotes_successful_non_coordinate_strategy(self, filter_mock):
        runner = UiFlowRunner(execution_id=12)
        element = mock.Mock()
        element.element_type = 'appium'
        element.config = {
            'resource_id': 'com.demo:id/login',
            'ocr_text': '登录',
            'locator_strategies': [
                {'type': 'resource_id', 'value': 'com.demo:id/login', 'enabled': True},
                {'type': 'ocr', 'value': '登录', 'enabled': True},
            ],
        }
        filter_mock.return_value.first.return_value = element

        detail = runner._record_self_heal(
            {'element_id': 9, 'self_heal_mode': 'persist'},
            'ocr',
            [
                {'strategy': 'resource_id', 'matched': False},
                {'strategy': 'ocr', 'matched': True},
            ],
        )

        self.assertTrue(detail['persisted'])
        self.assertEqual(element.config['locator_strategies'][0]['type'], 'ocr')
        self.assertEqual(element.config['self_heal']['heal_count'], 1)
        element.save.assert_called_once()

    @mock.patch('apps.app_automation.runners.ui_flow_runner.G')
    def test_record_video_starts_stops_and_returns_media_result(self, g_mock):
        runner = UiFlowRunner()
        runner._sleep_interruptibly = mock.Mock()
        device = mock.Mock()

        def create_video(**kwargs):
            with open(kwargs['output'], 'wb') as video_file:
                video_file.write(b'video')
            return kwargs['output']

        device.start_recording.side_effect = create_video
        g_mock.DEVICE = device

        runner._action_record_video({
            'duration': 2,
            'fps': 6,
            'record_mode': 'ffmpeg',
            'save_as': 'video_result',
        })

        result = runner.context['local']['video_result']
        self.assertTrue(result['completed'])
        self.assertEqual(result['size'], 5)
        device.stop_recording.assert_called_once()
