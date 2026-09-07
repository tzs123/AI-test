from celery import shared_task
from django.utils import timezone

from .models import TestCaseImportRecord
from .services import TestCaseExcelImportService


@shared_task(bind=True)
def import_testcases_from_excel(self, record_id: int):
    record = TestCaseImportRecord.objects.get(id=record_id)
    record.status = 'importing'
    record.progress = 5
    record.celery_task_id = self.request.id or ''
    record.error_message = ''
    record.save(update_fields=['status', 'progress', 'celery_task_id', 'error_message', 'updated_at'])

    try:
        summary = TestCaseExcelImportService.import_record(record)
        record.refresh_from_db()
        record.total_rows = summary.total_rows
        record.success_count = summary.success_count
        record.failed_count = summary.failed_count
        record.skip_count = summary.skip_count
        record.failure_details = summary.failure_details
        record.progress = 100
        record.completed_at = timezone.now()

        if summary.failed_count > 0 and summary.success_count > 0:
            record.status = 'partial_success'
        elif summary.failed_count > 0:
            record.status = 'failed'
        else:
            record.status = 'completed'

        failure_report = TestCaseExcelImportService.build_failure_report(record)
        if failure_report:
            record.failure_report_file.save(failure_report.name, failure_report, save=False)

        record.save()
        return {
            'record_id': record.id,
            'status': record.status,
            'success_count': record.success_count,
            'failed_count': record.failed_count,
        }
    except Exception as exc:
        record.status = 'failed'
        record.progress = 100
        record.error_message = str(exc)
        record.completed_at = timezone.now()
        record.save(update_fields=['status', 'progress', 'error_message', 'completed_at', 'updated_at'])
        raise
