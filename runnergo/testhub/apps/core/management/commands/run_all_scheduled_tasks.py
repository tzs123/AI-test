from django.core.management.base import BaseCommand
from django.utils import timezone
import logging
import time

from django.db import close_old_connections

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '运行 APP 自动化定时任务调度器'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=60,
            help='检查间隔（秒），默认60秒'
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='只执行一次检查，不循环'
        )

    def handle(self, *args, **options):
        interval = options['interval']
        run_once = options['once']

        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('启动 APP 自动化定时任务调度器'))
        self.stdout.write(self.style.SUCCESS(f'检查间隔: {interval}秒'))
        self.stdout.write(self.style.SUCCESS('=' * 60))

        while True:
            close_old_connections()
            try:
                now = timezone.localtime(timezone.now())
                self.stdout.write(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开始检查 APP 自动化任务...")

                app_count = self.schedule_app_tasks()
                if app_count > 0:
                    self.stdout.write(self.style.SUCCESS(f'✓ 本次调度执行了 {app_count} 个 APP 自动化任务'))
                else:
                    self.stdout.write('  没有需要执行的任务')

                if run_once:
                    self.stdout.write(self.style.WARNING('单次执行模式，调度器退出'))
                    break

                self.stdout.write(f'等待 {interval} 秒后进行下一次检查...')
                time.sleep(interval)

            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('\n\n调度器已停止'))
                break
            except Exception as e:
                logger.error(f'调度器运行出错: {e}', exc_info=True)
                self.stdout.write(self.style.ERROR(f'调度器运行出错: {e}'))
                if run_once:
                    break
                self.stdout.write(f'等待 {interval} 秒后重试...')
                time.sleep(interval)

    def schedule_app_tasks(self):
        """调度 APP 自动化模块的定时任务"""
        try:
            from apps.app_automation.models import AppScheduledTask, AppTestExecution

            active_tasks = AppScheduledTask.objects.filter(status='ACTIVE')
            executed_count = 0

            if active_tasks.exists():
                now = timezone.now()
                self.stdout.write(f'  [APP] 活跃任务数: {active_tasks.count()}')
                for task in active_tasks:
                    if task.next_run_time:
                        time_diff = (task.next_run_time - now).total_seconds()
                        if time_diff > 0:
                            self.stdout.write(f'        - {task.name}: 距下次执行还有 {int(time_diff)} 秒')
                        else:
                            self.stdout.write(f'        - {task.name}: 应该立即执行！')
                    else:
                        self.stdout.write(f'        - {task.name}: 未设置下次执行时间')

            for task in active_tasks:
                if not task.should_run_now():
                    continue

                self.stdout.write(f'  [APP] 执行任务: {task.name}')
                self.stdout.write(f'       类型: {task.get_task_type_display()}, 触发方式: {task.get_trigger_type_display()}')
                try:
                    task.last_run_time = timezone.now()
                    task.total_runs += 1
                    task.next_run_time = task.calculate_next_run()
                    task.save()

                    device = task.device
                    if not device:
                        self.stdout.write(self.style.ERROR(f'    ✗ 任务 {task.name} 未配置设备'))
                        continue

                    package_name = task.app_package.package_name if task.app_package else ''

                    if task.task_type == 'TEST_SUITE' and task.test_suite:
                        suite_cases = task.test_suite.suite_cases.select_related('test_case').all()
                        if not suite_cases.exists():
                            self.stdout.write(self.style.ERROR(f'    ✗ 套件 {task.test_suite.name} 无用例'))
                            continue

                        executions = []
                        for sc in suite_cases:
                            execution = AppTestExecution.objects.create(
                                test_case=sc.test_case,
                                test_suite=task.test_suite,
                                device=device,
                                user=task.created_by,
                                status='pending'
                            )
                            executions.append(execution)

                        task.test_suite.execution_status = 'running'
                        task.test_suite.save(update_fields=['execution_status'])

                        from apps.app_automation.tasks import execute_app_suite_task
                        execute_app_suite_task.delay(
                            suite_id=task.test_suite.id,
                            execution_ids=[execution.id for execution in executions],
                            package_name=package_name,
                            scheduled_task_id=task.id,
                        )

                    elif task.task_type == 'TEST_CASE' and task.test_case:
                        execution = AppTestExecution.objects.create(
                            test_case=task.test_case,
                            device=device,
                            user=task.created_by,
                            status='pending'
                        )
                        from apps.app_automation.tasks import execute_app_test_task
                        celery_task = execute_app_test_task.delay(
                            execution.id,
                            package_name=package_name,
                            scheduled_task_id=task.id,
                        )
                        execution.task_id = celery_task.id
                        execution.save(update_fields=['task_id'])

                    else:
                        self.stdout.write(self.style.ERROR(f'    ✗ 任务 {task.name} 配置不完整'))
                        continue

                    executed_count += 1
                    self.stdout.write(self.style.SUCCESS(f'    ✓ 任务 {task.name} 已启动'))

                except Exception as e:
                    logger.error(f'执行APP任务 {task.name} 时出错: {e}', exc_info=True)
                    self.stdout.write(self.style.ERROR(f'    ✗ 任务 {task.name} 执行失败: {e}'))

            return executed_count

        except Exception as e:
            logger.error(f'调度APP任务时出错: {e}', exc_info=True)
            self.stdout.write(self.style.ERROR(f'[APP] 调度失败: {e}'))
            return 0
