from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.core.crypto import decrypt_json, encrypt_json
from apps.core.outbound import validate_outbound_http_url


class CredentialCryptoTests(SimpleTestCase):
    def test_round_trip_preserves_nested_credentials(self):
        original = {
            'bearer_token': 'token-that-must-not-be-plain',
            'role_tokens': [{'name': 'viewer', 'token': 'viewer-secret'}],
        }
        encrypted = encrypt_json(original)
        self.assertNotIn(original['bearer_token'], encrypted)
        self.assertEqual(decrypt_json(encrypted), original)

    def test_invalid_ciphertext_fails_closed(self):
        self.assertEqual(decrypt_json('not-a-valid-ciphertext'), {})


class OutboundUrlSecurityTests(SimpleTestCase):
    def test_private_literal_and_localhost_are_blocked(self):
        for value in ('http://127.0.0.1:8000', 'http://localhost:8080', 'http://10.0.0.8'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_outbound_http_url(value, label='AI API 地址')

    @patch('apps.core.outbound.socket.getaddrinfo', return_value=[
        (2, 1, 6, '', ('10.20.30.40', 443)),
    ])
    def test_hostname_resolving_to_private_address_is_blocked(self, _dns):
        with self.assertRaises(ValueError):
            validate_outbound_http_url('https://ai.example.com/v1', label='AI API 地址')

    @override_settings(AI_OUTBOUND_ALLOWED_HOSTS=['ai.corp.example'])
    @patch('apps.core.outbound.socket.getaddrinfo', return_value=[
        (2, 1, 6, '', ('10.20.30.40', 443)),
    ])
    def test_explicit_hostname_allowlist_permits_private_service(self, _dns):
        value = validate_outbound_http_url('https://ai.corp.example/v1')
        self.assertEqual(value, 'https://ai.corp.example/v1')

    @patch('apps.core.outbound.socket.getaddrinfo', return_value=[
        (2, 1, 6, '', ('8.8.8.8', 443)),
    ])
    def test_public_service_is_allowed(self, _dns):
        value = validate_outbound_http_url('https://ai.example.com/v1')
        self.assertEqual(value, 'https://ai.example.com/v1')

    def test_credentials_and_fragments_are_rejected(self):
        for value in ('https://user:pass@ai.example.com', 'https://ai.example.com/v1#secret'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_outbound_http_url(value, resolve=False)
