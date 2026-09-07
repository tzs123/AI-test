# -*- coding: utf-8 -*-
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.requirement_analysis.models import RequirementDocument


User = get_user_model()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class RequirementDocumentUploadHashTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='document-upload-user',
            password='test-password',
        )
        self.client.force_authenticate(self.user)

    def upload_file(self, name, content):
        return self.client.post(
            '/api/requirement-analysis/documents/',
            {
                'title': name.rsplit('.', 1)[0],
                'file': SimpleUploadedFile(name, content, content_type='text/plain'),
            },
            format='multipart',
        )

    def test_same_content_can_be_uploaded_again_in_a_new_generation(self):
        content = b'login requirement\nsubmit button\n'

        first_response = self.upload_file('login.txt', content)
        duplicate_response = self.upload_file('login-copy.txt', content)

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(duplicate_response.status_code, 201)
        self.assertNotEqual(first_response.data['id'], duplicate_response.data['id'])
        self.assertEqual(RequirementDocument.objects.count(), 2)
        self.assertEqual(
            RequirementDocument.objects.order_by('id').first().content_hash,
            'f46a0e81b9801110c2b23ebecb5f9d77fd357c0a1df2a4f8bb4d35bb9fcfaf5d',
        )
