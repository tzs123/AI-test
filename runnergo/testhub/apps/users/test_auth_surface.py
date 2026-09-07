from django.test import TestCase
from rest_framework.test import APIClient
from unittest.mock import MagicMock, patch


class AuthSurfaceTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_only_runnergo_login_endpoint_is_exposed(self):
        removed_endpoints = [
            '/api/auth/login/',
            '/api/auth/sms-login/',
            '/api/auth/register/',
            '/api/auth/test-register/',
            '/api/auth/direct-access/',
            '/api/auth/exchange-token/',
            '/api/auth/captcha/',
            '/api/auth/send-register-code/',
        ]

        for endpoint in removed_endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, {}, format='json')
                self.assertEqual(response.status_code, 404)

        response = self.client.post('/api/auth/runnergo-sso/', {}, format='json')
        self.assertEqual(response.status_code, 401)
        self.assertIn('RunnerGo', response.data['error'])

    @patch('apps.users.views.requests.get')
    def test_runnergo_token_is_exchanged_for_testhub_session(self, mock_get):
        permission_response = MagicMock()
        permission_response.json.return_value = {
            'code': 0,
            'data': {
                'user_info': {
                    'account': 'runnergo-user',
                    'nickname': 'RunnerGo User',
                    'email': 'runnergo@example.com',
                },
                'user_related': {
                    'setting_team_id': 'team-1',
                    'company_id': 'company-1',
                    'company_name': 'RunnerGo',
                },
            },
        }
        mock_get.return_value = permission_response

        response = self.client.post(
            '/api/auth/runnergo-sso/',
            {},
            format='json',
            HTTP_AUTHORIZATION='runnergo-main-token',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['username'], 'runnergo-user')
        self.assertTrue(response.data['access'])
        self.assertTrue(response.data['refresh'])
        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args.kwargs['headers']['authorization'],
            'runnergo-main-token',
        )

    @patch('apps.users.views.requests.get')
    def test_runnergo_admin_role_is_synced_to_testhub_permissions(self, mock_get):
        permission_response = MagicMock()
        permission_response.json.return_value = {
            'code': 0,
            'data': {
                'user_info': {
                    'account': 'runnergo-admin',
                    'nickname': 'RunnerGo Admin',
                    'role_name': '超管',
                },
                'user_related': {
                    'setting_team_id': 'team-admin',
                    'company_id': 'company-admin',
                },
                'team_list': [
                    {'team_id': 'team-admin', 'role_name': '团队管理员'},
                ],
            },
        }
        mock_get.return_value = permission_response

        response = self.client.post(
            '/api/auth/runnergo-sso/',
            {},
            format='json',
            HTTP_AUTHORIZATION='runnergo-admin-token',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['user']['is_staff'])
        self.assertTrue(response.data['user']['is_superuser'])
