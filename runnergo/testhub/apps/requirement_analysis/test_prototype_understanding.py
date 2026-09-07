# -*- coding: utf-8 -*-
import json
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.requirement_analysis.models import PrototypeUnderstandingAsset


User = get_user_model()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PrototypeUnderstandingApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='prototype-user',
            password='test-password',
        )
        self.client.force_authenticate(self.user)

    def upload_file(self, name, content, content_type='text/plain'):
        return self.client.post('/api/requirement-analysis/prototype-understanding/', {
            'title': name.rsplit('.', 1)[0],
            'file': SimpleUploadedFile(
                name,
                content if isinstance(content, bytes) else content.encode('utf-8'),
                content_type=content_type,
            ),
        }, format='multipart')

    def test_login_prototype_generates_review_only_ui_flow_draft(self):
        response = self.upload_file(
            'login_prototype.txt',
            '\n'.join([
                '登录页',
                '手机号输入框',
                '获取验证码按钮',
                '验证码输入框',
                '登录按钮',
                '登录成功后进入首页',
            ]),
        )

        self.assertEqual(response.status_code, 201)
        asset_id = response.data['id']

        analyze_response = self.client.post(
            f'/api/requirement-analysis/prototype-understanding/{asset_id}/analyze/'
        )

        self.assertEqual(analyze_response.status_code, 200)
        analysis = analyze_response.data['analysis_result']
        self.assertEqual(analysis['page']['type'], 'login')
        element_keys = [item['key'] for item in analysis['elements']]
        self.assertIn('phone_input', element_keys)
        self.assertIn('verify_code_input', element_keys)
        self.assertIn('login_button', element_keys)

        draft_response = self.client.post(
            f'/api/requirement-analysis/prototype-understanding/{asset_id}/generate-ui-flow/'
        )

        self.assertEqual(draft_response.status_code, 200)
        draft = draft_response.data['ui_flow_draft']
        self.assertFalse(draft['execution_ready'])
        self.assertTrue(draft['review_required'])
        self.assertEqual(draft['steps'][0]['action'], 'open_app')
        self.assertIn('${PHONE}', [step.get('value') for step in draft['steps']])
        self.assertIn('${VERIFY_CODE}', [step.get('value') for step in draft['steps']])

        asset = PrototypeUnderstandingAsset.objects.get(id=asset_id)
        self.assertEqual(asset.status, 'draft_generated')

    def test_swagger_json_extracts_api_contracts(self):
        swagger = {
            'openapi': '3.0.0',
            'info': {'title': 'Login API', 'version': '1.0.0'},
            'paths': {
                '/api/login': {
                    'post': {
                        'summary': '手机号验证码登录',
                        'responses': {'200': {'description': 'OK'}},
                    },
                },
            },
        }
        response = self.upload_file(
            'swagger_login.json',
            json.dumps(swagger, ensure_ascii=False),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['asset_type'], 'swagger')

        analyze_response = self.client.post(
            f'/api/requirement-analysis/prototype-understanding/{response.data["id"]}/analyze/'
        )

        self.assertEqual(analyze_response.status_code, 200)
        contracts = analyze_response.data['analysis_result']['api_contracts']
        self.assertEqual(contracts[0]['method'], 'POST')
        self.assertEqual(contracts[0]['path'], '/api/login')
