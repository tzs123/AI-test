from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import DifyConfig
from .serializers import DifyConfigSerializer
from .views_config import DifyConfigViewSet


@override_settings(SECURITY_CREDENTIALS_KEY='dify-credential-test-key')
class DifyEnterpriseSecurityTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(username='dify-admin', is_staff=True)
        self.user = user_model.objects.create_user(username='dify-user')
        self.factory = APIRequestFactory()
        self.config = DifyConfig.objects.create(
            api_url='https://dify.example.test/v1',
            api_key='app-sensitive-key',
            is_active=True,
        )

    def test_api_key_is_encrypted_and_only_mask_is_serialized(self):
        self.config.refresh_from_db()
        self.assertTrue(self.config.api_key.startswith('fernet:v1:'))
        self.assertNotIn('app-sensitive-key', self.config.api_key)
        self.assertEqual(self.config.get_api_key(), 'app-sensitive-key')

        data = DifyConfigSerializer(self.config).data
        self.assertNotIn('api_key', data)
        self.assertEqual(data['api_key_masked'], 'app**********-key')

    def test_configuration_is_available_to_authenticated_users(self):
        request = self.factory.get('/api/assistant/config/dify/')
        force_authenticate(request, user=self.user)
        response = DifyConfigViewSet.as_view({'get': 'list'})(request)
        self.assertEqual(response.status_code, 200)

        admin_request = self.factory.get('/api/assistant/config/dify/')
        force_authenticate(admin_request, user=self.admin)
        admin_response = DifyConfigViewSet.as_view({'get': 'list'})(admin_request)
        self.assertEqual(admin_response.status_code, 200)
        self.assertNotIn('api_key', admin_response.data)

    def test_private_literal_is_rejected_before_connection(self):
        request = self.factory.post('/test_connection/', {
            'api_url': 'http://127.0.0.1:8080/v1',
            'api_key': 'app-test-key',
        }, format='json')
        force_authenticate(request, user=self.admin)
        response = DifyConfigViewSet.as_view({'post': 'test_connection'})(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn('受保护', response.data['error'])

    @patch('apps.assistant.views_config.requests.post')
    def test_connection_disables_redirects(self, post):
        post.return_value = Mock(status_code=200)
        request = self.factory.post('/test_connection/', {
            'api_url': 'https://dify.example.test/v1',
            'api_key': 'app-test-key',
        }, format='json')
        force_authenticate(request, user=self.admin)
        response = DifyConfigViewSet.as_view({'post': 'test_connection'})(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
