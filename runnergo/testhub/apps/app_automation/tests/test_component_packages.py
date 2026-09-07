# -*- coding: utf-8 -*-
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.app_automation.models import AppComponent, AppComponentPackage, AppCustomComponent


class ComponentPackageImportTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='component-package-tester',
            password='test-password',
        )
        self.client.force_authenticate(self.user)
        self.url = '/api/app-automation/component-packages/'

    def upload_json(self, name, payload, *, bom=False):
        content = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if bom:
            content = b'\xef\xbb\xbf' + content
        upload = SimpleUploadedFile(name, content, content_type='application/json')
        return self.client.post(
            self.url,
            {'file': upload, 'overwrite': '1'},
            format='multipart',
        )

    def test_recorded_steps_are_imported_as_custom_component(self):
        response = self.upload_json('recorded-steps.json', [
            {'type': 'click', 'name': '点击', 'config': {'selector_type': 'pos', 'selector': '10,20'}},
        ])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['data']['kind'], 'recorded_steps')
        component = AppCustomComponent.objects.get(type='recorded_steps')
        self.assertEqual(component.name, 'recorded-steps')
        self.assertEqual(component.steps[0]['type'], 'click')

    def test_recorded_input_steps_are_repaired_on_import(self):
        response = self.upload_json('recorded-steps.json', [
            {
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
                'type': 'input',
                'name': '单位地址',
                'config': {
                    'selector_type': 'image',
                    'selector': '',
                    'value': '杭州体育中心',
                },
            },
        ])

        self.assertEqual(response.status_code, 201)
        component = AppCustomComponent.objects.get(type='recorded_steps')
        self.assertEqual(component.steps[1]['config']['selector_type'], 'pos')
        self.assertEqual(component.steps[1]['config']['selector'], '244,382')
        self.assertTrue(component.steps[1]['config']['_recording_input_target_repaired'])

    def test_wrapped_recording_uses_exported_name_and_type(self):
        response = self.upload_json('airtest_steps_device_1.json', {
            'kind': 'runnergo_action_recording',
            'version': 1,
            'name': '模拟器登录流程',
            'type': 'recorded_device_1_login',
            'device_id': 'emulator-5554',
            'screen': {'width': 1080, 'height': 2400},
            'steps': [
                {'type': 'click', 'name': '点击登录', 'config': {'selector_type': 'pos', 'selector': '500,1800'}},
            ],
        })

        self.assertEqual(response.status_code, 201)
        component = AppCustomComponent.objects.get(type='recorded_device_1_login')
        self.assertEqual(component.name, '模拟器登录流程')
        self.assertEqual(len(component.steps), 1)

    def test_package_list_repairs_legacy_recording_without_custom_component(self):
        AppComponentPackage.objects.create(
            name='历史录制操作',
            version='1',
            source='upload',
            manifest={
                'kind': 'runnergo_action_recording',
                'version': 1,
                'name': '历史录制操作',
                'type': 'recorded_legacy_actions',
                'steps': [
                    {'type': 'click', 'name': '点击', 'config': {'selector_type': 'pos', 'selector': '10,20'}},
                ],
            },
            created_by=self.user,
        )
        self.assertFalse(AppCustomComponent.objects.filter(type='recorded_legacy_actions').exists())

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['repaired_custom_components'], 1)
        component = AppCustomComponent.objects.get(type='recorded_legacy_actions')
        self.assertEqual(component.name, '历史录制操作')
        self.assertEqual(component.steps[0]['type'], 'click')

    def test_deleting_recorded_custom_component_removes_package(self):
        component = AppCustomComponent.objects.create(
            name='录制操作',
            type='recorded_delete_me',
            steps=[{'type': 'click', 'name': '点击', 'config': {}}],
        )
        AppComponentPackage.objects.create(
            name='录制操作',
            version='1',
            source='upload',
            manifest={
                'kind': 'runnergo_action_recording',
                'version': 1,
                'name': '录制操作',
                'type': 'recorded_delete_me',
                'steps': [
                    {'type': 'click', 'name': '点击', 'config': {}},
                ],
            },
            created_by=self.user,
        )

        response = self.client.delete(f'/api/app-automation/custom-components/{component.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AppCustomComponent.objects.filter(type='recorded_delete_me').exists())
        self.assertEqual(AppComponentPackage.objects.count(), 0)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['repaired_custom_components'], 0)
        self.assertFalse(AppCustomComponent.objects.filter(type='recorded_delete_me').exists())

    def test_utf8_bom_component_package_can_be_imported(self):
        response = self.upload_json('component-pack.json', {
            'name': '测试组件包',
            'version': '1.0.0',
            'components': [{
                'type': 'test_component_package_click',
                'name': '测试点击',
                'category': 'action',
                'schema': {},
                'default_config': {},
            }],
        }, bom=True)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(AppComponent.objects.filter(type='test_component_package_click').exists())

    def test_short_video_components_are_installed_by_migration(self):
        components = AppComponent.objects.filter(
            type__in=['short_video_feed', 'wait_video_play', 'watch_video', 'random_action'],
            category='short_video',
            enabled=True,
        )

        self.assertEqual(components.count(), 4)
        feed = components.get(type='short_video_feed')
        self.assertEqual(feed.default_config['count'], 100)
        self.assertEqual(feed.default_config['watch_time'], {'min': 5, 'max': 15})
        self.assertTrue(feed.default_config['exception_detection'])

    def test_license_plate_industry_component_is_installed_by_migration(self):
        component = AppComponent.objects.get(
            type='license_plate_input',
            category='industry',
            enabled=True,
        )

        self.assertEqual(component.name, '车牌输入')
        self.assertEqual(component.default_config['plate'], '浙A12345')
        self.assertEqual(
            component.default_config['key_strategies'],
            ['semantic', 'ocr', 'coordinate'],
        )

    def test_checkbox_toggle_component_is_installed_by_migration(self):
        component = AppComponent.objects.get(
            type='checkbox_toggle',
            category='base_element',
            enabled=True,
        )

        self.assertEqual(component.name, '复选框设置')
        self.assertEqual(component.default_config['desired_state'], 'checked')
        self.assertTrue(component.default_config['verify_state'])
        self.assertEqual(
            component.schema['properties']['desired_state']['enum'],
            ['checked', 'unchecked', 'toggle'],
        )

    def test_popup_select_text_component_is_installed_by_migration(self):
        component = AppComponent.objects.get(
            type='popup_select_text',
            category='page_interaction',
            enabled=True,
        )

        self.assertEqual(component.name, '弹框选择')
        self.assertEqual(component.default_config['max_swipes'], 8)
        self.assertEqual(
            component.default_config['popup_region'],
            [0.0, 0.35, 1.0, 0.98],
        )
        self.assertIn('value', component.schema['required'])

    def test_second_stage_composite_components_are_installed_by_migration(self):
        expected_categories = {
            'login': 'page_interaction',
            'fill_form': 'page_interaction',
            'create_order': 'ecommerce',
            'comment_video': 'short_video',
        }

        components = AppComponent.objects.filter(type__in=expected_categories)

        self.assertEqual(components.count(), len(expected_categories))
        self.assertEqual(
            {item.type: item.category for item in components},
            expected_categories,
        )
        self.assertEqual(
            components.get(type='login').default_config['mode'],
            'password',
        )

    def test_third_stage_visual_and_self_healing_components_are_installed(self):
        component_types = [
            'record_video', 'visual_locate', 'visual_click', 'visual_assert',
            'self_heal_click', 'self_heal_input',
        ]

        components = AppComponent.objects.filter(type__in=component_types, enabled=True)

        self.assertEqual(components.count(), len(component_types))
        self.assertEqual(
            components.get(type='self_heal_click').default_config['self_heal_mode'],
            'report',
        )
        self.assertEqual(
            components.get(type='visual_locate').default_config['strategy_order'],
            ['semantic', 'ocr', 'image', 'position'],
        )

    def test_completed_platform_components_are_installed_by_migration(self):
        expected_categories = {
            'clear_text': 'base_element',
            'verify_code': 'finance',
            'identity_verify': 'finance',
            'risk_popup_handle': 'finance',
            'payment_flow': 'finance',
            'upload_video': 'short_video',
            'search_user': 'social',
            'send_message': 'social',
            'follow_user': 'social',
            'publish_post': 'social',
            'upload_image': 'social',
            'share_content': 'social',
            'comment': 'social',
        }

        components = AppComponent.objects.filter(type__in=expected_categories, enabled=True)

        self.assertEqual(components.count(), len(expected_categories))
        self.assertEqual(
            {item.type: item.category for item in components},
            expected_categories,
        )
        self.assertFalse(
            components.get(type='payment_flow').default_config['allow_real_payment']
        )
        self.assertIn(
            'locator_strategies',
            AppComponent.objects.get(type='smart_click').schema['properties'],
        )
        self.assertTrue(
            AppComponent.objects.get(type='input').default_config['clear_first']
        )
