from io import BytesIO
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient
from PIL import Image

from .image_data_assets import infer_fields_from_text, recognize_image_fields


class ImageDataAssetRecognitionTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='image-data-user', password='pass')

    def test_infer_common_fields_and_safe_sample_payload(self):
        result = infer_fields_from_text('''
        用户注册
        姓名：李四
        手机号：13800138000
        邮箱：li@example.com
        密码：real-password-from-image
        ''')

        self.assertEqual([field['name'] for field in result['fields']], ['name', 'phone', 'email', 'password'])
        self.assertEqual(result['sample_payload']['name'], '李四')
        self.assertEqual(result['sample_payload']['email'], 'li@example.com')
        self.assertEqual(result['sample_payload']['password'], 'Test@123456')
        self.assertEqual(result['field_definitions'][1]['type'], 'phone')

    def test_ignores_browser_url_and_recognizes_the_uploaded_form_fields(self):
        result = infer_fields_from_text('''
        不安全 172.16.0.88:9527/home?channelId=JDYFWH&productId=JDYPRD01
        Dimensions: Responsive 333 x 766 No throttling
        输入信息获取评测额度
        手机    请输入手机号
        验证码  请输入验证码    点击发送
        车牌号
        ''')

        names = [field['name'] for field in result['fields']]
        self.assertEqual(names, ['phone', 'verification_code', 'license_plate'])
        self.assertNotIn('product_name', names)
        self.assertEqual(result['fields'][1]['type'], 'verification_code')

    def test_recognizes_spaced_phone_label_and_deduplicates_ocr_variants(self):
        result = infer_fields_from_text('''
        手 机 请输入手 机 号
        手机
        验证码
        验证码
        ''')

        self.assertEqual([field['name'] for field in result['fields']], ['phone', 'verification_code'])
        self.assertEqual(result['sample_payload']['phone'], '13800138000')

    def test_phone_number_can_be_used_when_ocr_misses_the_label(self):
        result = infer_fields_from_text('''
        请输入
        13800138000
        ''')

        self.assertEqual([field['name'] for field in result['fields']], ['phone'])
        self.assertEqual(result['sample_payload']['phone'], '13800138000')

    def test_image_recognition_merges_multiple_ocr_passes(self):
        calls = []

        def fake_image_to_string(image, lang, config):
            calls.append((image.size, lang, config))
            # 模拟整图首次识别漏掉“手机”，第二个 OCR 通道识别出完整表单。
            if len(calls) == 2:
                return '手机\n验证码\n车牌号'
            return ''

        fake_pytesseract = SimpleNamespace(
            TesseractError=RuntimeError,
            image_to_string=fake_image_to_string,
        )
        image_buffer = BytesIO()
        Image.new('RGB', (120, 180), 'white').save(image_buffer, format='PNG')
        image_buffer.seek(0)
        upload = SimpleUploadedFile('form.png', image_buffer.read(), content_type='image/png')

        with patch.dict(sys.modules, {'pytesseract': fake_pytesseract}):
            result = recognize_image_fields(upload)

        self.assertGreater(len(calls), 1)
        self.assertEqual([field['name'] for field in result['fields']], ['phone', 'verification_code', 'license_plate'])

    def test_recognize_image_endpoint_requires_authentication(self):
        response = APIClient().post('/api/core/test-data-assets/recognize-image/', {}, format='multipart')
        self.assertEqual(response.status_code, 401)

    @patch('apps.core.views.recognize_image_fields')
    def test_recognize_image_endpoint_returns_editable_fields(self, recognize):
        recognize.return_value = infer_fields_from_text('姓名：李四\n手机号：13800138000\n邮箱：li@example.com')
        image_buffer = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image_buffer, format='PNG')
        image_buffer.seek(0)
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post(
            '/api/core/test-data-assets/recognize-image/',
            {'file': SimpleUploadedFile('register.png', image_buffer.read(), content_type='image/png')},
            format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['sample_payload']['phone'], '13800138000')
        self.assertEqual(response.data['field_definitions'][0]['name'], 'name')
