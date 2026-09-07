from django.apps import AppConfig
from django.core.signals import request_started
from django.dispatch import receiver
from django.utils import timezone


_generation_recovery_checked = False
_generation_process_started_at = timezone.now()


class RequirementAnalysisConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.requirement_analysis'
    verbose_name = '需求分析'


@receiver(request_started, dispatch_uid='requirement_analysis_generation_recovery_receiver')
def recover_generation_tasks_on_request(sender, **kwargs):
    global _generation_recovery_checked
    if _generation_recovery_checked:
        return
    try:
        from .tasks import recover_orphaned_generation_tasks

        recovered = recover_orphaned_generation_tasks(
            updated_before=_generation_process_started_at,
        )
        _generation_recovery_checked = True
        if recovered:
            import logging

            logging.getLogger(__name__).warning(
                '首次请求收口中断的生成任务: %s 条',
                recovered,
            )
    except Exception:
        # Never make an otherwise healthy API request fail because cleanup is
        # temporarily unavailable; the Celery worker signal remains a backup.
        import logging

        logging.getLogger(__name__).exception('首次请求收口生成任务失败')
