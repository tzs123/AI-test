from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import TestCaseGenerationTask
from .tasks import (
    GENERATION_TASK_INTERRUPTED_MESSAGE,
    recover_orphaned_generation_tasks,
)


class GenerationTaskRecoveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='generation-recovery-user',
            password='secret',
        )

    def _create_task(self, *, status='generating'):
        return TestCaseGenerationTask.objects.create(
            task_id=f'RECOVERY_{status}',
            title='中断恢复任务',
            requirement_text='业务需求',
            created_by=self.user,
            status=status,
            progress=30,
        )

    def test_recover_stale_active_task(self):
        task = self._create_task()
        cutoff = timezone.now() - timezone.timedelta(minutes=10)
        TestCaseGenerationTask.objects.filter(pk=task.pk).update(updated_at=cutoff)

        recovered = recover_orphaned_generation_tasks(stale_after_seconds=60)

        self.assertEqual(recovered, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_message, GENERATION_TASK_INTERRUPTED_MESSAGE)
        self.assertIsNotNone(task.completed_at)
        self.assertTrue(task.generation_log)

    def test_keep_recent_active_task(self):
        task = self._create_task(status='reviewing')

        recovered = recover_orphaned_generation_tasks(stale_after_seconds=60)

        self.assertEqual(recovered, 0)
        task.refresh_from_db()
        self.assertEqual(task.status, 'reviewing')

    def test_process_restart_recovers_task_updated_before_startup(self):
        task = self._create_task(status='pending')
        process_started_at = timezone.now() + timezone.timedelta(seconds=1)

        recovered = recover_orphaned_generation_tasks(
            updated_before=process_started_at,
        )

        self.assertEqual(recovered, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, 'failed')
