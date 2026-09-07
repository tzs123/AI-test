from types import SimpleNamespace

from django.test import SimpleTestCase
from django.conf import settings
from django.urls import Resolver404, resolve
from django.core import signing
from django.conf import settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from .permissions import UserManagementPermission


class UserManagementPermissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.permission = UserManagementPermission()

    def request(self, method, user):
        raw = getattr(self.factory, method.lower())('/api/users/1/')
        request = Request(raw)
        request.user = user
        return request

    def test_regular_user_can_read_self_only(self):
        user = SimpleNamespace(
            pk=7, is_authenticated=True, is_staff=False, is_superuser=False
        )
        request = self.request('GET', user)
        self.assertTrue(self.permission.has_permission(request, None))
        self.assertTrue(self.permission.has_object_permission(request, None, user))
        self.assertFalse(
            self.permission.has_object_permission(
                request, None, SimpleNamespace(pk=8)
            )
        )

    def test_regular_user_cannot_mutate_self(self):
        user = SimpleNamespace(
            pk=7, is_authenticated=True, is_staff=False, is_superuser=False
        )
        request = self.request('PATCH', user)
        self.assertFalse(self.permission.has_object_permission(request, None, user))

    def test_staff_can_manage_any_user(self):
        staff = SimpleNamespace(
            pk=7, is_authenticated=True, is_staff=True, is_superuser=False
        )
        request = self.request('DELETE', staff)
        self.assertTrue(
            self.permission.has_object_permission(
                request, None, SimpleNamespace(pk=8)
            )
        )


class ProductionSecuritySettingsTests(SimpleTestCase):
    def test_jwt_access_token_uses_long_term_policy(self):
        lifetime_seconds = settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds()
        self.assertGreaterEqual(lifetime_seconds, 3650 * 24 * 60 * 60)

    def test_all_platform_token_policies_are_long_term(self):
        long_term_seconds = 3650 * 24 * 60 * 60
        self.assertGreaterEqual(
            settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
            long_term_seconds,
        )
        self.assertGreaterEqual(
            settings.SIMPLE_JWT['SLIDING_TOKEN_LIFETIME'].total_seconds(),
            long_term_seconds,
        )
        self.assertGreaterEqual(
            settings.SIMPLE_JWT['SLIDING_TOKEN_REFRESH_LIFETIME'].total_seconds(),
            long_term_seconds,
        )
        self.assertGreaterEqual(settings.AI_SSE_TOKEN_MAX_AGE_SECONDS, long_term_seconds)
        self.assertGreaterEqual(settings.PUBLIC_REPORT_MAX_AGE_SECONDS, long_term_seconds)
        self.assertGreaterEqual(settings.PUBLIC_TEMPLATE_MAX_AGE_SECONDS, long_term_seconds)

    def test_api_docs_are_not_exposed_by_default(self):
        if settings.EXPOSE_API_DOCS:
            self.skipTest('API docs explicitly enabled by the test environment')
        with self.assertRaises(Resolver404):
            resolve('/api/docs/')

    def test_public_report_tokens_are_timestamped(self):
        token = signing.TimestampSigner(salt='app-automation-public-report').sign('42')
        value = signing.TimestampSigner(
            salt='app-automation-public-report'
        ).unsign(token, max_age=settings.PUBLIC_REPORT_MAX_AGE_SECONDS)
        self.assertEqual(value, '42')
