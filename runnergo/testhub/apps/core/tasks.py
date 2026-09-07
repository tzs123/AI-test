"""测试数据中心后台任务。"""

from celery import shared_task

from .bulk_data_generation import run_bulk_test_data_generation


@shared_task(bind=True)
def generate_bulk_test_data(self, job_id: int):
    return run_bulk_test_data_generation(job_id, self.request.id or '')
