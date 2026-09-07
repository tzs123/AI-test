import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.app_automation.utils.image_helpers import get_element_image_url


class SignedTemplateAccessTest(TestCase):
    def test_signed_template_url_serves_only_normalized_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            template_dir = root / 'apps' / 'app_automation' / 'Template' / 'common'
            template_dir.mkdir(parents=True)
            (template_dir / 'button.png').write_bytes(b'png-content')

            with override_settings(BASE_DIR=root, PUBLIC_TEMPLATE_MAX_AGE_SECONDS=60):
                url = get_element_image_url('common/button.png')
                response = self.client.get(url)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'png-content')
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_path_traversal_is_not_signed(self):
        self.assertIsNone(get_element_image_url('../settings.py'))

    def test_legacy_unauthenticated_static_report_route_is_removed(self):
        response = self.client.get('/app-automation-reports/1/index.html')
        self.assertEqual(response.status_code, 404)
