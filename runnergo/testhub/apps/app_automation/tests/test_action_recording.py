# -*- coding: utf-8 -*-
import os
import tempfile

from django.test import SimpleTestCase

from apps.app_automation.views.device_views import (
    _merge_recorded_step_overrides,
    _repair_action_recording_steps,
    build_ai_recording_enhancement,
    parse_action_recording,
)


class ActionRecordingParserTests(SimpleTestCase):
    def test_recorded_step_name_overrides_are_merged_by_id(self):
        steps = [
            {'id': 'step-1', 'type': 'click', 'name': 'iOS 录制点击 1', 'config': {}},
            {'id': 'step-2', 'type': 'input', 'name': 'iOS 录制输入 2', 'config': {'value': 'abc'}},
        ]

        merged = _merge_recorded_step_overrides(
            steps,
            [
                {'id': 'step-1', 'name': 'iOS 录制点击 1'},
                {'id': 'step-2', 'name': '输入手机号'},
            ],
        )

        self.assertEqual(merged[0]['name'], 'iOS 录制点击 1')
        self.assertEqual(merged[1]['name'], '输入手机号')

    def test_recorded_steps_missing_from_request_are_removed(self):
        steps = [
            {'id': 'step-1', 'type': 'click', 'name': 'iOS 录制点击 1', 'config': {}},
            {'id': 'step-2', 'type': 'input', 'name': 'iOS 录制输入 2', 'config': {'value': 'abc'}},
            {'id': 'step-3', 'type': 'click', 'name': 'iOS 录制点击 3', 'config': {}},
        ]

        merged = _merge_recorded_step_overrides(
            steps,
            [{'id': 'step-2', 'name': '输入手机号'}],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['id'], 'step-2')
        self.assertEqual(merged[0]['name'], '输入手机号')

    def test_recorded_input_step_is_repaired_from_previous_text_field_click(self):
        steps = [
            {
                'id': 'step-1',
                'type': 'click',
                'name': 'iOS 录制点击 2',
                'config': {
                    'selector_type': 'pos',
                    'selector': '244,382',
                    'fingerprint': {
                        'resource_id': '单位地址',
                        'text': '请填写包含门牌号的地址',
                        'content_desc': '单位地址',
                        'class_name': 'XCUIElementTypeTextField',
                    },
                },
            },
            {
                'id': 'step-2',
                'type': 'click',
                'name': 'iOS 录制点击 1',
                'config': {
                    'selector_type': 'pos',
                    'selector': '347,380',
                    'fingerprint': {
                        'text': '体育中心',
                        'class_name': 'XCUIElementTypeStaticText',
                    },
                },
            },
            {
                'id': 'step-3',
                'type': 'input',
                'name': '单位地址',
                'config': {
                    'selector_type': 'image',
                    'selector': '',
                    'value': '杭州体育中心',
                },
            },
        ]

        repaired_steps, repairs = _repair_action_recording_steps(steps)

        self.assertEqual(repaired_steps[2]['config']['selector_type'], 'pos')
        self.assertEqual(repaired_steps[2]['config']['selector'], '244,382')
        self.assertEqual(
            repaired_steps[2]['config']['fingerprint']['content_desc'],
            '单位地址',
        )
        self.assertTrue(repaired_steps[2]['config']['_recording_input_target_repaired'])
        self.assertEqual(repairs[0]['reason'], 'blank-input-selector-inherited')

    def test_recorded_input_step_without_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '不可回放的输入定位'):
            _repair_action_recording_steps([
                {
                    'id': 'step-1',
                    'type': 'input',
                    'name': '单位地址',
                    'config': {
                        'selector_type': 'image',
                        'selector': '',
                        'value': '杭州体育中心',
                    },
                },
            ])

    def test_keyboard_events_are_merged_into_ordered_input_steps(self):
        raw_events = """\
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID 00000000
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_X 00000064
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_Y 000000c8
[ 1.000000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 1.100000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID ffffffff
[ 1.100000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 2.000000] /dev/input/event12: EV_KEY KEY_1 DOWN
[ 2.000100] /dev/input/event12: EV_KEY KEY_1 UP
[ 2.100000] /dev/input/event12: EV_KEY KEY_2 DOWN
[ 2.100100] /dev/input/event12: EV_KEY KEY_2 UP
[ 3.000000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID 00000000
[ 3.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_X 0000012c
[ 3.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_Y 00000190
[ 3.000000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 3.100000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID ffffffff
[ 3.100000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 4.000000] /dev/input/event12: EV_KEY KEY_3 DOWN
[ 4.000100] /dev/input/event12: EV_KEY KEY_3 UP
"""
        metadata = {
            'x_min': 0,
            'x_max': 1080,
            'y_min': 0,
            'y_max': 2400,
            'screen_width': 1080,
            'screen_height': 2400,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = os.path.join(temp_dir, 'recording.log')
            with open(raw_path, 'w', encoding='utf-8') as handle:
                handle.write(raw_events)

            steps = parse_action_recording(raw_path, metadata)

        self.assertEqual([step['type'] for step in steps], ['click', 'input', 'click', 'input'])
        self.assertEqual(steps[1]['config']['value'], '12')
        self.assertEqual(steps[3]['config']['value'], '3')

    def test_ctrl_v_uses_captured_text_and_reuses_clicked_input_locator(self):
        raw_events = """\
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID 00000000
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_X 000000ed
[ 1.000000] /dev/input/event1: EV_ABS ABS_MT_POSITION_Y 000000ce
[ 1.000000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 1.100000] /dev/input/event1: EV_ABS ABS_MT_TRACKING_ID ffffffff
[ 1.100000] /dev/input/event1: EV_SYN SYN_REPORT 00000000
[ 2.000000] /dev/input/event12: EV_KEY KEY_LEFTCTRL DOWN
[ 2.010000] /dev/input/event12: EV_KEY KEY_V DOWN
[ 2.020000] /dev/input/event12: EV_KEY KEY_V UP
[ 2.030000] /dev/input/event12: EV_KEY KEY_LEFTCTRL UP
[ 3.000000] /dev/input/event12: EV_KEY KEY_ENTER DOWN
[ 3.010000] /dev/input/event12: EV_KEY KEY_ENTER UP
"""
        metadata = {
            'x_min': 0,
            'x_max': 1080,
            'y_min': 0,
            'y_max': 2400,
            'screen_width': 1080,
            'screen_height': 2400,
        }
        fingerprint = {
            'version': 1,
            'resource_id': 'com.android.chrome:id/url_bar',
            'class_name': 'android.widget.EditText',
            'package': 'com.android.chrome',
            'clickable': True,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = os.path.join(temp_dir, 'recording.log')
            with open(raw_path, 'w', encoding='utf-8') as handle:
                handle.write(raw_events)

            steps = parse_action_recording(
                raw_path,
                metadata,
                gesture_fingerprints=[{'fingerprint': fingerprint}],
                paste_events=[{'time': 2.01, 'value': 'http://172.16.0.88:9527/submit'}],
            )

        self.assertEqual([step['type'] for step in steps], ['click', 'input'])
        self.assertEqual(steps[1]['config']['value'], 'http://172.16.0.88:9527/submit')
        self.assertTrue(steps[1]['config']['send_enter'])
        self.assertEqual(
            steps[1]['config']['fingerprint']['resource_id'],
            'com.android.chrome:id/url_bar',
        )

    def test_ai_recording_enhancement_builds_search_scenario(self):
        steps = [
            {
                'id': 'step_click_search_box',
                'type': 'click',
                'name': '录制点击 1',
                'config': {
                    'selector_type': 'pos',
                    'selector': '120,180',
                    'coordinate_mode': 'normalized',
                    'fingerprint': {
                        'version': 1,
                        'resource_id': 'com.example:id/search_box',
                        'class_name': 'android.widget.EditText',
                        'clickable': True,
                    },
                },
            },
            {
                'id': 'step_input_keyword',
                'type': 'input',
                'name': '录制输入 1',
                'config': {
                    'value': '手机',
                    'send_enter': False,
                },
            },
            {
                'id': 'step_click_search',
                'type': 'click',
                'name': '录制点击 2',
                'config': {
                    'selector_type': 'pos',
                    'selector': '900,180',
                    'coordinate_mode': 'normalized',
                    'fingerprint': {
                        'version': 1,
                        'text': '搜索',
                        'class_name': 'android.widget.Button',
                        'clickable': True,
                    },
                },
            },
        ]

        enhancement = build_ai_recording_enhancement(steps)

        self.assertEqual(enhancement['name'], 'AI Test Recorder')
        self.assertEqual(enhancement['scenario'], '搜索商品')
        self.assertIn('手机', enhancement['goal'])
        self.assertEqual(enhancement['test_steps'][0]['action'], '打开APP')
        self.assertEqual(enhancement['test_steps'][-1]['action'], '校验搜索结果')
        self.assertEqual(
            [item['action'] for item in enhancement['recorded_actions']],
            ['点击search box', '输入"手机"', '点击搜索'],
        )
