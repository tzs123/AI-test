from unittest.mock import patch
from urllib.parse import quote

from asgiref.sync import async_to_sync
from django.core import signing
from django.http import StreamingHttpResponse
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory, force_authenticate
from django.test import SimpleTestCase

from .views import TestCaseGenerationTaskViewSet


class _CompletedTask:
    task_id = 'TASK_TEST_SSE'
    status = 'completed'
    progress = 100
    output_mode = 'stream'
    stream_buffer = 'generated content'
    stream_position = len(stream_buffer)
    review_feedback = 'review content'
    final_test_cases = 'final content'
    created_by_id = 1
    project = None

    def refresh_from_db(self):
        return None


class StreamProgressSSETests(SimpleTestCase):
    def test_streaming_response_uses_async_iterator(self):
        stream_token = signing.dumps(
            {'task_id': 'TASK_TEST_SSE', 'user_id': 1},
            salt='runnergo.ai-generation.sse',
            compress=True,
        )
        request = APIRequestFactory().get(
            '/api/requirement-analysis/testcase-generation/TASK_TEST_SSE/stream_progress/'
            f'?stream_token={quote(stream_token)}',
            HTTP_ORIGIN='http://localhost:3000',
        )
        force_authenticate(request, user=type('AuthenticatedUser', (), {
            'pk': 1,
            'is_authenticated': True,
            '__str__': lambda self: 'test-user',
        })())
        with patch('apps.requirement_analysis.views.TestCaseGenerationTask.objects.select_related') as related_mock:
            related_mock.return_value.filter.return_value.first.return_value = _CompletedTask()
            response = TestCaseGenerationTaskViewSet.as_view(
                {'get': 'stream_progress_sse'},
                permission_classes=[AllowAny],
                authentication_classes=[],
            )(request, task_id='TASK_TEST_SSE')

        self.assertIsInstance(response, StreamingHttpResponse)
        self.assertTrue(response.is_async)

        async def consume_stream():
            chunks = []
            async for chunk in response.streaming_content:
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8')

        content = async_to_sync(consume_stream)()
        self.assertIn('"status": "completed"', content)
        self.assertIn('generated content', content)
        self.assertIn('review content', content)
        self.assertIn('final content', content)
        self.assertIn('"type": "done"', content)

    def test_streaming_response_rejects_missing_task_token(self):
        request = APIRequestFactory().get(
            '/api/requirement-analysis/testcase-generation/TASK_TEST_SSE/stream_progress/',
            HTTP_ORIGIN='http://localhost:3000',
        )
        response = TestCaseGenerationTaskViewSet.as_view(
            {'get': 'stream_progress_sse'},
            permission_classes=[AllowAny],
            authentication_classes=[],
        )(request, task_id='TASK_TEST_SSE')

        self.assertEqual(response.status_code, 403)
