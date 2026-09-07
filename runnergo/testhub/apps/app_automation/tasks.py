# -*- coding: utf-8 -*-
"""
APP自动化测试 Celery 任务
"""
from celery import current_app, shared_task
from celery.signals import worker_ready
from django.utils import timezone
import logging
import os
import random
import string
import uuid
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


APP_EXECUTION_INTERRUPTED_MESSAGE = (
    "APP自动化执行进程已中断：未在 Celery 活跃/排队任务中找到对应 task_id，"
    "已自动结束执行并释放设备"
)
APP_EXECUTION_STALE_SECONDS = int(os.environ.get('APP_EXECUTION_STALE_SECONDS', '300'))


def _agent_data_value(field_type):
    digits = lambda size: ''.join(random.choice(string.digits) for _ in range(size))
    generators = {
        'phone': lambda: '1' + random.choice('3456789') + digits(9),
        'verification_code': lambda: digits(6),
        'password': lambda: 'Rg@' + uuid.uuid4().hex[:10],
        'email': lambda: f"agent_{uuid.uuid4().hex[:8]}@runnergo.test",
        'username': lambda: f"agent_{uuid.uuid4().hex[:10]}",
        'name': lambda: '测试用户' + digits(4),
        'id_card': lambda: '1101011990' + digits(8),
        'license_plate': lambda: '京A' + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5)),
        'amount': lambda: str(random.randint(1, 9999)),
        'date': lambda: timezone.localdate().isoformat(),
        'url': lambda: f"https://runnergo.test/{uuid.uuid4().hex[:8]}",
        'address': lambda: f"北京市朝阳区测试路{random.randint(1, 999)}号",
        'number': lambda: random.randint(1, 99),
    }
    return generators.get(field_type, lambda: f"test_{uuid.uuid4().hex[:8]}")()


def _prepare_app_agent_data(task, test_case, row_count):
    """Create reusable data-center rows and one exact requirement for an APP case."""
    from apps.core.models import TestDataAsset, TestDataAssetRequirement
    from apps.core.smart_data_orchestration import analyze_data_requirements

    requirements = analyze_data_requirements(test_case.ui_flow)
    if not requirements:
        return None
    generated_bindings = task.plan.get('generated_data_bindings') or []
    generated_binding = next((
        item for item in generated_bindings
        if str(item.get('target_case_id')) == str(test_case.id)
    ), None)
    if generated_binding:
        data_requirement = TestDataAssetRequirement.objects.filter(
            id=generated_binding.get('requirement_id'),
            source_asset_id=generated_binding.get('asset_id'),
            target_type='app_automation',
            target_case_id=test_case.id,
            is_active=True,
        ).select_related('source_asset').first()
        if data_requirement and data_requirement.source_asset:
            available_fields = [str(item) for item in generated_binding.get('fields') or []]
            mapped_fields = []
            overrides = []
            for index, requirement in enumerate(requirements):
                field_key = requirement['fieldType']
                if available_fields and field_key not in available_fields:
                    field_key = available_fields[index % len(available_fields)]
                mapped_fields.append(field_key)
                overrides.append({
                    'stepId': requirement['field'].get('stepId'),
                    'stepPath': requirement['field'].get('stepPath'),
                    'runtimeValue': '${dataAssets.' + data_requirement.alias + '.' + field_key + '}',
                })
            payload = data_requirement.source_asset.payload
            payload_rows = payload if isinstance(payload, list) else [payload]
            return {
                'alias': data_requirement.alias,
                'binding_tag': generated_binding.get('binding_tag') or '',
                'requirement_id': data_requirement.id,
                'asset_ids': [data_requirement.source_asset_id] * max(1, len(payload_rows)),
                'runtime_override': overrides,
                'row_count': max(1, len(payload_rows)),
                'fields': mapped_fields,
                'reused_generated_asset': True,
            }
    alias = f"agent_app_{task.id}_{test_case.id}"[:100]
    binding_tag = f"agent-bind-app-{task.id}-{test_case.id}"
    stale_asset_ids = [
        asset.id for asset in TestDataAsset.objects.filter(created_by=task.user)
        if binding_tag in (asset.tags or [])
    ]
    if stale_asset_ids:
        TestDataAsset.objects.filter(id__in=stale_asset_ids).delete()
    TestDataAssetRequirement.objects.filter(
        target_type='app_automation',
        target_case_id=test_case.id,
        alias=alias,
    ).delete()
    assets = []
    for index in range(max(1, min(int(row_count or 1), 20))):
        row = {}
        for requirement in requirements:
            key = requirement['fieldType']
            if key in row:
                key = f"{key}_{len(row) + 1}"
            requirement['assetField'] = key
            row[key] = _agent_data_value(requirement['fieldType'])
        assets.append(TestDataAsset.objects.create(
            asset_type='CUSTOM',
            name=f"AI Test Agent - {test_case.name} - 参数 {index + 1}",
            status=TestDataAsset.STATUS_AVAILABLE,
            payload=row,
            tags=['ai-generated', 'app-test-agent', binding_tag],
            created_by=task.user,
        ))
    data_requirement = TestDataAssetRequirement.objects.create(
        target_type='app_automation',
        target_case_id=test_case.id,
        target_case_name=test_case.name,
        alias=alias,
        asset_type='CUSTOM',
        source_asset=assets[0],
        quantity=1,
        filters={},
        tags=[binding_tag],
        release_policy='mark_used',
        is_active=True,
        created_by=task.user,
    )
    overrides = [{
        'stepId': requirement['field'].get('stepId'),
        'stepPath': requirement['field'].get('stepPath'),
        'runtimeValue': '${dataAssets.' + alias + '.' + requirement['assetField'] + '}',
    } for requirement in requirements]
    return {
        'alias': alias,
        'binding_tag': binding_tag,
        'requirement_id': data_requirement.id,
        'asset_ids': [asset.id for asset in assets],
        'runtime_override': overrides,
        'row_count': len(assets),
        'fields': [item['assetField'] for item in requirements],
    }


def _collect_celery_task_ids(payload):
    """从 Celery inspect 返回的 active/reserved/scheduled 结构中提取任务 ID。"""
    task_ids = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {'id', 'task_id'} and value:
                task_ids.add(str(value))
            else:
                task_ids.update(_collect_celery_task_ids(value))
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            task_ids.update(_collect_celery_task_ids(item))
    return task_ids


def _inspect_known_celery_task_ids():
    """返回当前 Celery 集群已知的活跃/排队任务 ID；inspect 失败时返回 None。"""
    try:
        inspector = current_app.control.inspect(timeout=1.0)
        snapshots = []
        saw_response = False
        for getter in (inspector.active, inspector.reserved, inspector.scheduled):
            try:
                snapshot = getter()
            except Exception as exc:
                logger.debug(f"获取 Celery 任务快照失败: {exc}")
                continue
            if snapshot is None:
                continue
            saw_response = True
            snapshots.append(snapshot)
        if not saw_response:
            return None
        return _collect_celery_task_ids(snapshots)
    except Exception as exc:
        logger.warning(f"检查 Celery 活跃任务失败: {exc}")
        return None


def recover_interrupted_app_executions(
    *,
    active_task_ids=None,
    stale_after_seconds=APP_EXECUTION_STALE_SECONDS,
    reason=APP_EXECUTION_INTERRUPTED_MESSAGE,
):
    """收口 worker 异常退出遗留的 running 执行，并释放被占用设备。"""
    from .models import AppTestExecution

    known_task_ids = active_task_ids
    if known_task_ids is None:
        known_task_ids = _inspect_known_celery_task_ids()
    elif not isinstance(known_task_ids, set):
        known_task_ids = {str(task_id) for task_id in known_task_ids if task_id}

    now = timezone.now()
    queryset = AppTestExecution.objects.select_related('device', 'user').filter(status='running')
    if known_task_ids is None:
        queryset = queryset.filter(updated_at__lt=now - timezone.timedelta(seconds=stale_after_seconds))
    else:
        queryset = queryset.exclude(task_id__in=known_task_ids)

    recovered = 0
    for execution in queryset:
        finished_at = timezone.now()
        duration = execution.duration
        if execution.started_at:
            duration = (finished_at - execution.started_at).total_seconds()
        updated = AppTestExecution.objects.filter(
            id=execution.id,
            status='running',
        ).update(
            status='error',
            result=None,
            error_message=reason,
            finished_at=finished_at,
            duration=duration,
        )
        if not updated:
            continue
        recovered += 1
        logger.warning(
            "已清理中断的 APP 自动化执行: execution_id=%s, task_id=%s",
            execution.id,
            execution.task_id,
        )
        try:
            from apps.core.data_assets import release_assets_for_execution
            release_assets_for_execution('app_automation', execution.id)
        except Exception as exc:
            logger.error(f"释放中断执行的数据资产失败: execution_id={execution.id}, error={exc}")
        try:
            device = execution.device
            if device and execution.user_id and device.locked_by_id == execution.user_id:
                device.unlock()
                logger.info(f"中断执行设备已释放: {device.device_id}")
        except Exception as exc:
            logger.error(f"释放中断执行设备失败: execution_id={execution.id}, error={exc}")
        send_execution_update(
            execution.id,
            status='error',
            progress=execution.progress or 0,
            message=reason,
            report_path=execution.report_path,
            finished_at=finished_at,
            result=None,
        )
    return recovered


def _build_public_report_url(execution_id):
    """生成飞书等外部客户端无需登录即可访问的签名 Allure 地址。"""
    from .report_urls import build_public_report_url

    return build_public_report_url(execution_id)


def _get_report_links(last_result):
    from .models import AppTestExecution

    execution_ids = last_result.get('execution_ids') or []
    if last_result.get('execution_id'):
        execution_ids = [last_result['execution_id']]
    executions = AppTestExecution.objects.filter(
        id__in=execution_ids
    ).select_related('test_case')
    execution_map = {execution.id: execution for execution in executions}
    links = []
    for execution_id in execution_ids:
        execution = execution_map.get(int(execution_id))
        if execution and execution.report_path:
            links.append({
                'name': execution.test_case.name if execution.test_case else f'执行 #{execution.id}',
                'url': _build_public_report_url(execution.id),
            })
    return links


def _get_report_paths(execution_id, report_path):
    if not report_path:
        return {}
    from .report_urls import build_internal_report_path, build_public_report_path

    return {
        'report_url': build_internal_report_path(execution_id),
        'public_report_url': build_public_report_path(execution_id),
    }


def _get_enabled_app_webhook_bots():
    import requests
    from django.conf import settings

    url = (
        settings.RUNNERGO_MANAGEMENT_API_URL
        + '/notice/internal/enabled_webhook_bots'
    )
    try:
        response = requests.get(
            url,
            headers={'X-Agent-Token': settings.TEST_DATA_CENTER_AGENT_TOKEN},
            timeout=5,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception as e:
        logger.error('从 RunnerGo 第三方集成获取通知配置失败: %s，回退直连读取 runnergo 库', type(e).__name__)
        return _read_webhook_bots_from_runnergo_db()

    return [
        bot for bot in (payload.get('bots') or [])
        if isinstance(bot, dict) and bot.get('enabled', True)
    ]


# third_notice_channel.type 与 Webhook 发送器支持类型的映射：
# 1=飞书 2=企业微信 3=邮箱（发送器不支持，跳过） 4=钉钉
_RUNNERGO_NOTICE_CHANNEL_TYPES = {1: 'feishu', 2: 'wechat', 4: 'dingtalk'}


def _read_webhook_bots_from_runnergo_db():
    """manage 未提供 internal 通知接口时，直接从 runnergo 库读取启用的 Webhook 机器人。"""
    import json

    import pymysql
    from django.conf import settings

    if not settings.RUNNERGO_MYSQL_HOST or not settings.RUNNERGO_MYSQL_PASSWORD:
        logger.warning('未配置 RUNNERGO_MYSQL_*，无法回退读取通知配置')
        return []
    try:
        connection = pymysql.connect(
            host=settings.RUNNERGO_MYSQL_HOST,
            port=int(settings.RUNNERGO_MYSQL_PORT),
            user=settings.RUNNERGO_MYSQL_USER,
            password=settings.RUNNERGO_MYSQL_PASSWORD,
            database=settings.RUNNERGO_MYSQL_DATABASE,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
        )
    except Exception as e:
        logger.error('连接 runnergo 库读取通知配置失败: %s', type(e).__name__)
        return []

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT n.notice_id, n.name, n.params, n.created_at, "
                "c.type AS channel_type "
                "FROM third_notice n "
                "LEFT JOIN third_notice_channel c ON c.id = n.channel_id "
                "WHERE n.status = 1 AND n.deleted_at IS NULL "
                "AND (c.deleted_at IS NULL OR c.id IS NULL)"
            )
            rows = cursor.fetchall() or []
    except Exception as e:
        logger.error('读取 runnergo 通知配置失败: %s', type(e).__name__)
        return []
    finally:
        try:
            connection.close()
        except Exception:
            pass

    bots = []
    for row in rows:
        params = row.get('params') or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (TypeError, ValueError):
                params = {}
        if not isinstance(params, dict) or not params.get('webhook_url'):
            continue
        bot_type = _RUNNERGO_NOTICE_CHANNEL_TYPES.get(row.get('channel_type'))
        if not bot_type:
            continue
        bots.append({
            'id': str(row.get('notice_id') or ''),
            'type': bot_type,
            'name': str(row.get('name') or '未命名通知'),
            'webhook_url': str(params.get('webhook_url') or ''),
            'secret': str(params.get('secret') or ''),
            'enabled': True,
            'created_at': str(row.get('created_at') or ''),
        })
    return bots


def get_app_notification_targets(bots=None):
    """Return browser-safe notification choices without webhook credentials."""
    source = _get_enabled_app_webhook_bots() if bots is None else bots
    targets = []
    for bot in source:
        notification_id = str(bot.get('id') or '').strip()
        if not notification_id:
            continue
        targets.append({
            'id': notification_id,
            'type': str(bot.get('type') or 'webhook').strip().lower(),
            'name': str(bot.get('name') or '未命名通知').strip(),
            'created_at': str(bot.get('created_at') or '').strip(),
        })
    return targets


def select_app_webhook_bots(notification_ids, bots=None):
    """Resolve selected IDs against the latest enabled integrations."""
    wanted = {str(item or '').strip() for item in notification_ids or []}
    wanted.discard('')
    source = _get_enabled_app_webhook_bots() if bots is None else bots
    return [bot for bot in source if str(bot.get('id') or '').strip() in wanted]


def _remove_allure_link_section(content):
    """飞书卡片已有按钮时，正文里不再重复展示 Allure markdown 链接。"""
    if not content:
        return content
    marker = "\n\nAllure 报告:"
    if marker not in content:
        return content
    return content.split(marker, 1)[0].rstrip()


def _send_app_webhook_message(
    *,
    task=None,
    task_name,
    task_type='',
    notification_type='manual',
    title,
    detail_content,
    status_text,
    report_links=None,
    bots=None,
):
    """发送 APP 自动化 Webhook 通知，并写入通知日志。"""
    import requests
    import json
    from .models import AppNotificationLog

    all_bots = _get_enabled_app_webhook_bots() if bots is None else bots
    if not all_bots:
        logger.info("未找到启用的 APP 自动化 Webhook 机器人，跳过通知")
        return []

    report_links = report_links or []
    success = status_text == '成功'
    results = []

    for bot in all_bots:
        webhook_url = bot.get('webhook_url')
        if not webhook_url:
            continue

        bot_type = bot.get('type', 'unknown')
        safe_bot_info = {
            'type': bot_type,
            'name': bot.get('name', 'Unknown'),
            'enabled': bool(bot.get('enabled', True)),
        }
        try:
            from apps.core.outbound import validate_outbound_http_url

            # DNS 必须在真正发送前重新解析，防止保存后发生 DNS rebinding。
            webhook_url = validate_outbound_http_url(
                webhook_url,
                label='通知 Webhook 地址',
            )
        except ValueError as exc:
            logger.warning('拒绝不安全的 %s Webhook 地址: %s', bot_type, exc)
            AppNotificationLog.objects.create(
                task=task,
                task_name=task_name[:200],
                task_type=task_type or '',
                notification_type=notification_type,
                sender_name='系统Webhook通知',
                sender_email='system@notification.com',
                recipient_info=[{'name': safe_bot_info['name']}],
                webhook_bot_info=safe_bot_info,
                notification_content='{}',
                status='failed',
                error_message='Webhook 地址未通过出站安全校验',
            )
            results.append({
                'id': str(bot.get('id') or ''),
                'type': bot_type,
                'name': safe_bot_info['name'],
                'success': False,
                'reason': 'Webhook 地址未通过出站安全校验',
            })
            continue

        if bot_type == 'wechat':
            message_data = {
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n\n{detail_content}"}
            }
        elif bot_type == 'feishu':
            feishu_content = _remove_allure_link_section(detail_content) if report_links else detail_content
            elements = [{
                "tag": "div",
                "text": {"content": feishu_content, "tag": "lark_md"}
            }]
            if report_links:
                actions = []
                for index, report in enumerate(report_links[:5]):
                    actions.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看allure报告"},
                        "url": report['url'],
                        "type": "primary" if index == 0 else "default",
                    })
                elements.append({"tag": "action", "actions": actions})
            message_data = {
                "msg_type": "interactive",
                "card": {
                    "elements": elements,
                    "header": {
                        "title": {"content": title, "tag": "plain_text"},
                        "template": "green" if success else "red",
                    },
                },
            }
        elif bot_type == 'dingtalk':
            message_data = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": f"**{title}**\n\n{detail_content}"}
            }
            secret = bot.get('secret')
            if secret:
                import time as _time, hmac, hashlib, base64, urllib.parse
                timestamp = str(round(_time.time() * 1000))
                sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(
                    secret.encode('utf-8'),
                    f'{timestamp}\n{secret}'.encode('utf-8'),
                    digestmod=hashlib.sha256
                ).digest()))
                webhook_url += f'{"&" if "?" in webhook_url else "?"}timestamp={timestamp}&sign={sign}'
        else:
            continue

        try:
            resp = requests.post(
                webhook_url,
                json=message_data,
                headers={'Content-Type': 'application/json'},
                timeout=10,
                allow_redirects=False,
            )
            log_status = 'success' if resp.status_code == 200 else 'failed'
            AppNotificationLog.objects.create(
                task=task,
                task_name=task_name[:200],
                task_type=task_type or '',
                notification_type=notification_type,
                sender_name='系统Webhook通知',
                sender_email='system@notification.com',
                recipient_info=[{'name': safe_bot_info['name']}],
                webhook_bot_info=safe_bot_info,
                notification_content=json.dumps(message_data, ensure_ascii=False),
                status=log_status,
                error_message='' if log_status == 'success' else f'Webhook HTTP {resp.status_code}',
                response_info={'status_code': resp.status_code},
                sent_at=timezone.now()
            )
            results.append({
                'id': str(bot.get('id') or ''),
                'type': bot_type,
                'name': safe_bot_info['name'],
                'success': log_status == 'success',
            })
        except Exception as e:
            logger.error('发送 %s Webhook 失败: %s', bot_type, type(e).__name__)
            AppNotificationLog.objects.create(
                task=task,
                task_name=task_name[:200],
                task_type=task_type or '',
                notification_type=notification_type,
                sender_name='系统Webhook通知',
                sender_email='system@notification.com',
                recipient_info=[{'name': safe_bot_info['name']}],
                webhook_bot_info=safe_bot_info,
                notification_content=json.dumps(message_data, ensure_ascii=False),
                status='failed',
                error_message=f'Webhook 请求失败: {type(e).__name__}'
            )
            results.append({
                'id': str(bot.get('id') or ''),
                'type': bot_type,
                'name': safe_bot_info['name'],
                'success': False,
                'reason': 'Webhook 请求失败',
            })

    return results


def send_manual_execution_failure_notification(execution, reason=''):
    """手动执行失败/异常时发送 APP 自动化报告通知。"""
    send_manual_execution_completion_notification(execution, reason=reason)


def send_manual_execution_completion_notification(
    execution, reason='', bots=None, allow_stopped=False,
):
    """手动执行完成后发送 APP 自动化报告通知；停止中的执行不发送。"""
    try:
        if execution.status == 'stopped' and not allow_stopped:
            return []
        test_case_name = execution.test_case.name if execution.test_case else f'执行 #{execution.id}'
        if execution.status == 'completed':
            status_text = {
                'passed': '成功',
                'failed': '失败',
                'skipped': '跳过',
            }.get(execution.result or '', execution.result or '完成')
        elif execution.status == 'stopped':
            status_text = '已停止'
        else:
            status_text = '异常'
        device_name = (execution.device.name or execution.device.device_id) if execution.device else '未指定'
        finished_at = timezone.localtime(execution.finished_at).strftime('%Y-%m-%d %H:%M:%S') if execution.finished_at else '未知'
        report_links = _get_report_links({'execution_id': execution.id})
        detail_content = (
            f"用例名称: {test_case_name}\n\n"
            f"执行状态: {status_text}\n\n"
            f"执行设备: {device_name}\n\n"
            f"通过步骤: {execution.passed_steps}\n\n"
            f"失败步骤: {execution.failed_steps}\n\n"
            f"跳过步骤: {execution.skipped_steps}\n\n"
            f"结束时间: {finished_at}"
        )
        if reason:
            detail_content += f"\n\n失败原因: {reason}"
        if report_links:
            detail_content += "\n\nAllure 报告:"
            for report in report_links:
                detail_content += f"\n[{report['name']}]({report['url']})"

        return _send_app_webhook_message(
            task_name=test_case_name,
            task_type='TEST_CASE',
            notification_type='manual',
            title=f'APP自动化执行{status_text}',
            detail_content=detail_content,
            status_text=status_text,
            report_links=report_links,
            bots=bots,
        )
    except Exception as e:
        logger.error(f"发送APP手动执行完成通知失败: {e}", exc_info=True)
        return []


def send_manual_suite_failure_notification(suite, executions, passed, failed, reason=''):
    """手动套件执行失败/异常时发送 APP 自动化报告通知。"""
    send_manual_suite_completion_notification(
        suite, executions, passed, failed, reason=reason
    )


def send_manual_suite_completion_notification(
    suite, executions, passed, failed, reason='', bots=None, allow_stopped=False,
):
    """手动套件全部结束后发送 APP 自动化报告通知；手动停止不发送。"""
    try:
        if suite.execution_status == 'stopped' and not allow_stopped:
            return []
        execution_ids = [execution.id for execution in executions]
        report_links = _get_report_links({'execution_ids': execution_ids})
        if suite.execution_status == 'completed':
            status_text = '成功' if failed == 0 else '失败'
        elif suite.execution_status == 'stopped':
            status_text = '已停止'
        else:
            status_text = '异常'
        detail_content = (
            f"套件名称: {suite.name}\n\n"
            f"执行状态: {status_text}\n\n"
            f"通过用例: {passed}\n\n"
            f"失败用例: {failed}"
        )
        if reason:
            detail_content += f"\n\n失败原因: {reason}"
        if report_links:
            detail_content += "\n\nAllure 报告:"
            for report in report_links:
                detail_content += f"\n[{report['name']}]({report['url']})"

        return _send_app_webhook_message(
            task_name=suite.name,
            task_type='TEST_SUITE',
            notification_type='test_suite_execution',
            title=f'APP自动化测试套件执行{status_text}',
            detail_content=detail_content,
            status_text=status_text,
            report_links=report_links,
            bots=bots,
        )
    except Exception as e:
        logger.error(f"发送APP套件完成通知失败: {e}", exc_info=True)
        return []


def send_app_agent_completion_notification(agent_task, executions, summary):
    """APP AI Agent 全部执行结束后统一发送报告通知。"""
    try:
        if agent_task.status == 'stopped':
            return
        task_name = str(agent_task.goal or '').strip()[:80] or f'APP AI Agent #{agent_task.id}'
        report_links = _get_report_links({
            'execution_ids': [execution.id for execution in executions],
        })
        status_text = '失败' if summary.get('has_failures') else '成功'
        detail_content = (
            f"Agent 任务: {task_name}\n\n"
            f"执行状态: {status_text}\n\n"
            f"总用例: {summary.get('total', 0)}\n\n"
            f"通过: {summary.get('passed', 0)}\n\n"
            f"失败: {summary.get('failed', 0)}\n\n"
            f"跳过: {summary.get('skipped', 0)}\n\n"
            f"通过率: {summary.get('pass_rate', 0)}%"
        )
        if report_links:
            detail_content += "\n\nAllure 报告:"
            for report in report_links:
                detail_content += f"\n[{report['name']}]({report['url']})"

        _send_app_webhook_message(
            task_name=task_name,
            task_type='AI_AGENT',
            notification_type='manual',
            title=f'APP AI Test Agent 执行{status_text}',
            detail_content=detail_content,
            status_text=status_text,
            report_links=report_links,
        )
    except Exception as e:
        logger.error(f"发送APP Agent完成通知失败: {e}", exc_info=True)


def send_scheduled_task_notification(task_id, success):
    """定时执行结束后，通过统一第三方集成自动发送报告通知。"""
    try:
        from .models import AppScheduledTask

        task = AppScheduledTask.objects.get(id=task_id)

        status_text = '成功' if success else '失败'
        last_result = task.last_result or {}
        report_links = _get_report_links(last_result)
        result_message = last_result.get('message', '')
        local_run_time = timezone.localtime(task.last_run_time).strftime('%Y-%m-%d %H:%M:%S') if task.last_run_time else '未知'
        device_name = (task.device.name or task.device.device_id) if task.device else '未指定'

        detail_content = (
            f"任务名称: {task.name}\n\n"
            f"执行状态: {status_text}\n\n"
            f"执行时间: {local_run_time}\n\n"
            f"任务类型: {task.get_task_type_display()}\n\n"
            f"执行设备: {device_name}"
        )
        if result_message:
            detail_content += f"\n\n执行结果: {result_message}"
        if report_links:
            detail_content += "\n\nAllure 报告:"
            for report in report_links:
                detail_content += f"\n[{report['name']}]({report['url']})"

        return _send_app_webhook_notification(
            task,
            detail_content,
            status_text,
            report_links,
        )

    except Exception as e:
        logger.error(f"发送APP定时任务通知失败: {e}", exc_info=True)
        return []


def _send_app_webhook_notification(task, detail_content, status_text, report_links=None):
    """使用“设置 → 第三方集成”中的全部启用通知发送定时报告。"""
    return _send_app_webhook_message(
        task=task,
        task_name=task.name,
        task_type=task.task_type,
        notification_type='task_execution',
        title=f"APP自动化定时任务执行{status_text}",
        detail_content=detail_content,
        status_text=status_text,
        report_links=report_links,
    )


def send_execution_update(execution_id, status=None, progress=None, message=None, report_path=None, finished_at=None, result=None):
    """通过 WebSocket 发送执行状态更新"""
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        payload = {
            "type": "execution_update",
            "execution_id": int(execution_id),
            "status": status,
            "result": result,
            "progress": progress,
            "message": message,
            "report_path": report_path,
            "finished_at": finished_at.isoformat() if finished_at else None,
        }
        payload.update(_get_report_paths(execution_id, report_path))
        async_to_sync(channel_layer.group_send)(
            f"app_execution_{execution_id}",
            payload
        )
    except Exception as e:
        logger.debug(f"发送执行状态更新失败: {e}")


@shared_task
def execute_app_test_task(
    execution_id,
    package_name: str = None,
    scheduled_task_id: int = None,
    send_completion_notification: bool = True,
):
    """
    异步执行APP测试任务
    
    Args:
        execution_id: AppTestExecution 的 ID
        package_name: 可选的应用包名
        scheduled_task_id: 可选的定时任务 ID（来自定时调度）
    """
    from django.conf import settings
    from .models import AppTestExecution, AppDevice
    from .executors.test_executor import AppTestExecutor
    
    execution = None
    device = None
    
    try:
        # 获取执行记录
        execution = AppTestExecution.objects.get(id=execution_id)
        test_case = execution.test_case
        
        device = execution.device
        
        # 只允许 pending -> running，避免停止请求与任务启动并发时被覆盖回运行中。
        started_at = timezone.now()
        started = AppTestExecution.objects.filter(
            id=execution_id,
            status='pending',
        ).update(status='running', started_at=started_at, progress=0)
        if not started:
            execution.refresh_from_db()
            if execution.status == 'stopped':
                logger.info(f"执行在任务启动前已停止: execution_id={execution_id}")
                return
            raise RuntimeError(f"执行状态不允许启动: {execution.status}")
        execution.refresh_from_db()
        send_execution_update(execution_id, status='running', progress=0, message='任务开始执行')
        
        logger.info(f"开始执行APP测试: {test_case.name}")
        
        # 1. 检查并锁定设备
        if device.status == 'locked' and device.locked_by != execution.user:
            raise RuntimeError(f"设备 {device.device_id} 已被其他用户锁定")
        
        if device.status != 'locked':
            device.lock(execution.user)
        
        logger.info(f"设备已锁定: {device.device_id}")
        
        # 2. 由 pytest + allure 插件执行测试
        # 进度分配：0~10% 环境准备，10~90% 步骤执行（由子进程内回调动态更新），90~100% 报告生成
        progressed = AppTestExecution.objects.filter(
            id=execution_id,
            status='running',
        ).update(progress=10)
        if not progressed:
            execution.refresh_from_db()
            if execution.status == 'stopped':
                logger.info(f"执行在环境准备前已停止: execution_id={execution_id}")
                return
            raise RuntimeError(f"执行状态异常: {execution.status}")
        execution.progress = 10
        send_execution_update(execution_id, status='running', progress=10, message='正在准备测试环境')
        
        if package_name:
            final_package_name = package_name
        else:
            final_package_name = (
                test_case.app_package.package_name
                if test_case.app_package
                else (device.default_bundle_id or "")
            )

        executor = AppTestExecutor()
        report_result = executor.run_tests(
            test_case_id=test_case.id,
            device_id=device.device_id,
            package_name=final_package_name,
            execution_id=execution_id,
            username=execution.user.username if execution.user else 'unknown',
            platform=device.platform,
            wda_url=device.wda_url,
            wda_bundle_id=device.wda_bundle_id,
        )
        
        # 从数据库重新读取最新进度（子进程中的回调可能已经更新过）
        execution.refresh_from_db()

        # 无论停止与否，先提取执行器返回的报告路径和步骤结果。
        # 停止后的报告与统计同样重要：用户需要查看已执行步骤的报告。
        if report_result.get('report_path'):
            execution.report_path = report_result['report_path']
            logger.info(f"报告已生成: {report_result['report_path']}")

        test_results = report_result.get('test_results', {}) or {}
        execution.total_steps = test_results.get('total', 0)
        execution.passed_steps = test_results.get('passed', 0)
        # parser 已将 broken 算在 failed 里，直接使用
        execution.failed_steps = test_results.get('failed', 0)
        execution.skipped_steps = test_results.get('skipped', 0)
        if test_results.get('broken', 0):
            logger.info(f"检测到 broken 用例 {test_results.get('broken')} 个（已计入失败统计）。")

        # ===== 用户停止分支：保存结果并发送带报告路径的最终通知 =====
        if execution.status == 'stopped' or report_result.get('stopped'):
            logger.info(
                f"APP测试已按用户请求停止，保存结果报告并通知前端: "
                f"execution_id={execution_id}, report_path={bool(execution.report_path)}, "
                f"total={execution.total_steps}, passed={execution.passed_steps}, "
                f"failed={execution.failed_steps}"
            )
            # 计算 stopped 场景的部分测试结果（已执行步骤的统计）
            if execution.total_steps == 0:
                stopped_result = 'skipped'
            elif execution.failed_steps == 0:
                stopped_result = 'passed'
            else:
                stopped_result = 'failed'

            # 确保状态已落库为 stopped（停止 API 与 Celery 存在竞态，API 未必先写库）
            now_ts = timezone.now()
            if execution.started_at and not execution.finished_at:
                stopped_duration = (now_ts - execution.started_at).total_seconds()
            else:
                stopped_duration = execution.duration or (
                    (now_ts - execution.started_at).total_seconds()
                    if execution.started_at else 0
                )
            # 注意：stop() API 已先把 status 写成 stopped 并保存了 finished_at / duration，
            # 这里仅在缺失时补齐，并写入报告路径与统计。
            AppTestExecution.objects.filter(id=execution_id).update(
                status='stopped',
                report_path=execution.report_path,
                total_steps=execution.total_steps,
                passed_steps=execution.passed_steps,
                failed_steps=execution.failed_steps,
                skipped_steps=execution.skipped_steps,
                result=stopped_result,
                finished_at=execution.finished_at or now_ts,
                duration=stopped_duration,
                progress=100,
            )
            execution.refresh_from_db()

            # 通知前端：停止后同样携带 report_path、result、finished_at，让"查看报告"可用
            send_execution_update(
                execution_id,
                status='stopped',
                progress=100,
                message='执行已停止',
                report_path=execution.report_path,
                finished_at=execution.finished_at,
                result=execution.result,
            )
            # 定时任务/手动通知：停止也算一次完成（用部分结果）
            if scheduled_task_id:
                try:
                    from .models import AppScheduledTask
                    st = AppScheduledTask.objects.get(id=scheduled_task_id)
                    is_success = execution.result == 'passed'
                    if is_success:
                        st.successful_runs += 1
                    else:
                        st.failed_runs += 1
                    st.last_result = {
                        'status': 'stopped',
                        'result': execution.result,
                        'message': f'{test_case.name} - 已停止（{execution.result or "无结果"}）',
                        'execution_id': execution.id,
                    }
                    st.save(update_fields=['successful_runs', 'failed_runs', 'last_result'])
                    send_scheduled_task_notification(scheduled_task_id, success=is_success)
                except Exception as ne:
                    logger.error(f"停止场景更新定时任务状态失败: {ne}")
            elif send_completion_notification:
                try:
                    send_manual_execution_completion_notification(execution)
                except Exception as ne:
                    logger.warning(f"停止场景发送手动执行通知失败: {ne}")
            return
        
        execution.progress = 95
        result_saved = AppTestExecution.objects.filter(
            id=execution_id,
            status='running',
        ).update(
            report_path=execution.report_path,
            total_steps=execution.total_steps,
            passed_steps=execution.passed_steps,
            failed_steps=execution.failed_steps,
            skipped_steps=execution.skipped_steps,
            progress=95,
        )
        if not result_saved:
            execution.refresh_from_db()
            if execution.status == 'stopped':
                logger.info(f"执行在结果保存阶段已停止: execution_id={execution_id}")
                return
            raise RuntimeError(f"执行状态异常: {execution.status}")
        send_execution_update(
            execution_id,
            status='running',
            progress=95,
            message='正在生成测试报告',
            report_path=execution.report_path
        )
        
        # 3. 完成测试 — 分离任务状态和测试结果
        if execution.total_steps == 0:
            final_result = 'skipped'
        elif execution.failed_steps == 0:
            final_result = 'passed'
        else:
            final_result = 'failed'
        finished_at = timezone.now()
        duration = (finished_at - execution.started_at).total_seconds()
        completed = AppTestExecution.objects.filter(
            id=execution_id,
            status='running',
        ).update(
            status='completed',
            result=final_result,
            finished_at=finished_at,
            duration=duration,
            progress=100,
        )
        if not completed:
            execution.refresh_from_db()
            if execution.status == 'stopped':
                logger.info(f"执行在完成落库前已停止: execution_id={execution_id}")
                return
            raise RuntimeError(f"执行状态异常: {execution.status}")
        execution.refresh_from_db()
        if execution.result == 'failed':
            try:
                from apps.core.smart_data_orchestration import repair_failed_execution_data
                repair_failed_execution_data(
                    execution_type='app_automation',
                    execution_id=execution.id,
                    failure_info={
                        'status': execution.status,
                        'result': execution.result,
                        'failedSteps': execution.failed_steps,
                        'reportPath': execution.report_path,
                    },
                )
            except Exception as repair_error:
                logger.warning(f"V6测试数据自动修复失败: {repair_error}")
        send_execution_update(
            execution_id,
            status=execution.status,
            progress=100,
            message='执行完成',
            report_path=execution.report_path,
            finished_at=execution.finished_at,
            result=execution.result,
        )
        
        logger.info(f"APP测试执行完成: {test_case.name}, 状态: {execution.status}, 结果: {execution.result}")

        # 定时任务通知
        if scheduled_task_id:
            try:
                from .models import AppScheduledTask
                st = AppScheduledTask.objects.get(id=scheduled_task_id)
                is_success = execution.result == 'passed'
                if is_success:
                    st.successful_runs += 1
                else:
                    st.failed_runs += 1
                st.last_result = {
                    'status': execution.status,
                    'result': execution.result,
                    'message': f'{test_case.name} - {execution.result or execution.status}',
                    'execution_id': execution.id,
                }
                st.save(update_fields=['successful_runs', 'failed_runs', 'last_result'])
                send_scheduled_task_notification(scheduled_task_id, success=is_success)
            except Exception as ne:
                logger.error(f"更新定时任务状态失败: {ne}")
        elif send_completion_notification:
            send_manual_execution_completion_notification(execution)

    except AppTestExecution.DoesNotExist:
        logger.error(f"执行记录不存在: {execution_id}")
    except Exception as e:
        logger.error(f"执行APP测试失败: {str(e)}", exc_info=True)
        
        if execution:
            execution.refresh_from_db()
            if execution.status == 'stopped':
                # 停止场景下异常不会被记录为 error，但仍要确保报告已生成并通知前端，
                # 以防正常停止处理链路（上方）未走完。
                logger.info(
                    f"执行停止后捕获到异常（非致命），仍尝试补齐报告与最终通知: "
                    f"execution_id={execution_id}, error={e}"
                )
                try:
                    # 尝试生成报告（如果执行器正常路径还没来得及生成）
                    if not execution.report_path:
                        executor_ = AppTestExecutor()
                        rp = executor_._generate_allure_report(execution_id=execution_id)
                        if rp:
                            execution.report_path = rp
                    # 确保 stopped 记录有 result / finished_at / duration / progress
                    finalize_updates = {
                        'report_path': execution.report_path,
                        'progress': 100,
                    }
                    if execution.result is None:
                        if (execution.total_steps or 0) == 0:
                            finalize_updates['result'] = 'skipped'
                        elif (execution.failed_steps or 0) == 0:
                            finalize_updates['result'] = 'passed'
                        else:
                            finalize_updates['result'] = 'failed'
                    if not execution.finished_at:
                        now_ts = timezone.now()
                        finalize_updates['finished_at'] = now_ts
                        if execution.started_at and not execution.duration:
                            finalize_updates['duration'] = (
                                now_ts - execution.started_at
                            ).total_seconds()
                    AppTestExecution.objects.filter(id=execution_id).update(**finalize_updates)
                    execution.refresh_from_db()
                    send_execution_update(
                        execution_id,
                        status='stopped',
                        progress=100,
                        message='执行已停止',
                        report_path=execution.report_path,
                        finished_at=execution.finished_at,
                        result=execution.result,
                    )
                except Exception as finalize_error:
                    logger.warning(
                        f"停止+异常场景补齐报告失败（忽略）: "
                        f"execution_id={execution_id}, error={finalize_error}"
                    )
                return
            execution.status = 'error'       # 任务异常（非用例失败）
            execution.result = None           # 没有测试结果
            execution.error_message = str(e)
            execution.finished_at = timezone.now()
            if execution.started_at:
                execution.duration = (execution.finished_at - execution.started_at).total_seconds()
            execution.save()
            try:
                from apps.core.smart_data_orchestration import repair_failed_execution_data
                repair_failed_execution_data(
                    execution_type='app_automation',
                    execution_id=execution.id,
                    failure_info={
                        'status': execution.status,
                        'errorMessage': str(e),
                        'reportPath': execution.report_path,
                    },
                )
            except Exception as repair_error:
                logger.warning(f"V6测试数据自动修复失败: {repair_error}")
            send_execution_update(
                execution_id,
                status='error',
                progress=execution.progress or 0,
                message=str(e),
                report_path=execution.report_path,
                finished_at=execution.finished_at,
                result=None,
            )
            
            # 尝试生成报告
            try:
                executor = AppTestExecutor()
                report_path = executor._generate_allure_report(execution_id=execution_id)
                if report_path:
                    execution.report_path = report_path
                    execution.save(update_fields=['report_path', 'updated_at'])
            except Exception as report_error:
                logger.warning(f"异常场景生成APP报告失败: {report_error}")

            if scheduled_task_id:
                try:
                    from .models import AppScheduledTask
                    st = AppScheduledTask.objects.get(id=scheduled_task_id)
                    st.failed_runs += 1
                    st.last_result = {
                        'status': execution.status,
                        'result': execution.result,
                        'message': str(e),
                        'execution_id': execution.id,
                    }
                    st.save(update_fields=['failed_runs', 'last_result'])
                    send_scheduled_task_notification(scheduled_task_id, success=False)
                except Exception as ne:
                    logger.error(f"更新定时任务异常状态失败: {ne}")
            else:
                send_manual_execution_failure_notification(execution, reason=str(e))
    finally:
        # 6. 清理：释放或清理本次执行申请的数据资产（幂等，pytest 内已释放时会跳过）
        try:
            if execution:
                from apps.core.data_assets import release_assets_for_execution
                release_assets_for_execution('app_automation', execution.id)
        except Exception as e:
            logger.error(f"释放测试数据资产失败: {str(e)}")

        # 7. 清理：释放设备
        try:
            if device and device.locked_by == execution.user:
                device.unlock()
                logger.info(f"设备已释放: {device.device_id}")
        except Exception as e:
            logger.error(f"释放设备失败: {str(e)}")


def finalize_app_agent_task(agent_task_id: int) -> None:
    """Finalize recorded-case and planned-exploration results as one task."""
    from .agent_service import analyze_app_agent_results, record_agent_event
    from .models import AppAgentTask, AppTestExecution

    task = AppAgentTask.objects.select_related(
        'user', 'project', 'device', 'app_package'
    ).get(id=agent_task_id)
    execution_map = {
        execution.id: execution
        for execution in AppTestExecution.objects.filter(
            id__in=task.execution_ids
        ).select_related('test_case', 'test_case__app_package', 'device')
    }
    executions = [
        execution_map[item] for item in task.execution_ids if item in execution_map
    ]
    summary, analysis, defect_drafts = analyze_app_agent_results(executions)
    exploration_targets = list((task.plan or {}).get('exploration_targets') or [])
    exploration_run = dict((task.plan or {}).get('exploration_run') or {})
    if exploration_targets:
        exploration_results = list(exploration_run.get('results') or [])
        completed_explorations = sum(
            1 for item in exploration_results if item.get('status') == 'completed'
        )
        failed_explorations = sum(
            1 for item in exploration_results if item.get('status') != 'completed'
        )
        summary = {
            **summary,
            'has_failures': bool(summary.get('has_failures') or failed_explorations),
            'execution_completed': True,
            'exploration': {
                'total': len(exploration_targets),
                'completed': completed_explorations,
                'failed': failed_explorations,
                'session_ids': exploration_run.get('session_ids') or [],
                'results': exploration_results,
            },
        }
        analysis['summary'] = (
            f'{analysis["summary"]} '
            f'另执行 {len(exploration_targets)} 个页面探索目标，'
            f'{completed_explorations} 个完成，{failed_explorations} 个失败。'
        )
        record_agent_event(
            task,
            'exploration',
            f'已完成 {completed_explorations}/{len(exploration_targets)} 个页面探索目标',
            level='success' if not failed_explorations else 'error',
            payload={
                'status': 'success' if not failed_explorations else 'failed',
                **summary['exploration'],
            },
        )
        record_agent_event(
            task,
            'execution',
            '已有正式用例与用户输入的页面探索目标均已执行完成',
            level='success' if not summary['has_failures'] and not failed_explorations else 'error',
            payload={
                'status': (
                    'success'
                    if not summary['has_failures'] and not failed_explorations
                    else 'failed'
                ),
                'case_execution_count': summary['total'],
                'exploration_count': len(exploration_targets),
            },
        )
    else:
        runtime_sessions = task.runtime_sessions.filter(
            status__in=['completed', 'failed', 'budget_exhausted', 'stopped'],
        )
        # A direct/controlled runtime run has one session. Keep historical
        # attempts out of a rerun summary by selecting the latest terminal one.
        runtime_sessions = list(runtime_sessions.order_by('-created_at', '-id')[:1])
        if runtime_sessions:
            runtime_passed = sum(item.status == 'completed' for item in runtime_sessions)
            runtime_failed = sum(
                item.status in {'failed', 'budget_exhausted'} for item in runtime_sessions
            )
            runtime_stopped = sum(item.status == 'stopped' for item in runtime_sessions)
            runtime_results = [
                {
                    'session_id': item.id,
                    'status': item.status,
                    'message': (item.result_summary or {}).get('message') or item.error_message,
                    'used_steps': item.used_steps,
                    'page_count': (item.result_summary or {}).get('page_count', 0),
                    'edge_count': (item.result_summary or {}).get('edge_count', 0),
                }
                for item in runtime_sessions
            ]
            summary = {
                **summary,
                'total': summary['total'] + len(runtime_sessions),
                'passed': summary['passed'] + runtime_passed,
                'failed': summary['failed'] + runtime_failed,
                'stopped': summary['stopped'] + runtime_stopped,
                'has_failures': bool(summary['has_failures'] or runtime_failed),
                'execution_completed': True,
                'runtime': {
                    'total': len(runtime_sessions),
                    'completed': runtime_passed,
                    'failed': runtime_failed,
                    'stopped': runtime_stopped,
                    'session_ids': [item.id for item in runtime_sessions],
                    'results': runtime_results,
                },
            }
            summary['pass_rate'] = round(
                summary['passed'] / summary['total'] * 100, 2
            ) if summary['total'] else 0
            if runtime_failed:
                runtime_analysis = task.failure_analysis or {}
                if runtime_analysis:
                    analysis = runtime_analysis
                runtime_defects = task.defect_draft or []
                if runtime_defects:
                    defect_drafts = runtime_defects
            analysis['summary'] = (
                f'{analysis.get("summary") or "执行结果已聚合"} '
                f'另完成 {len(runtime_sessions)} 个自治运行会话，'
                f'{runtime_passed} 个通过，{runtime_failed} 个失败。'
            )
            record_agent_event(
                task,
                'execution',
                f'自治运行会话已完成：{runtime_passed} 个通过，{runtime_failed} 个失败',
                level='error' if runtime_failed else 'success',
                payload={
                    'status': 'failed' if runtime_failed else 'success',
                    **summary['runtime'],
                },
            )

    task.status = 'analyzing'
    task.progress = 92
    task.current_execution = None
    task.save(update_fields=['status', 'progress', 'current_execution', 'updated_at'])
    record_agent_event(
        task,
        'result_analysis',
        '已聚合真实执行结果并生成质量汇总',
        level='success',
        payload={'status': 'success', **summary},
    )
    if summary['has_failures']:
        repair_suggestions = analysis.get('repair_suggestions') or []
        record_agent_event(
            task,
            'failure_analysis',
            f'已完成 {summary["failed"]} 个失败执行的原因定位',
            level='success',
            payload={
                'status': 'success',
                'failed': summary['failed'],
                'repair_suggestion_count': len(repair_suggestions),
            },
        )
        record_agent_event(
            task,
            'self_heal',
            (
                f'已生成 {len(repair_suggestions)} 条修复建议，需评审后应用，本次未修改测试用例'
                if repair_suggestions else
                '未生成可应用的修复建议，本次未修改测试用例'
            ),
            payload={
                'status': 'not_applicable',
                'reason': 'review_required' if repair_suggestions else 'no_repair_candidate',
                'repair_suggestion_count': len(repair_suggestions),
                'repair_applied': False,
            },
        )
    else:
        record_agent_event(
            task,
            'failure_analysis',
            '全部执行通过，无需进行失败原因定位',
            payload={'status': 'not_applicable', 'reason': 'no_failures'},
        )
        record_agent_event(
            task,
            'self_heal',
            '没有失败用例，无需应用测试修复',
            payload={
                'status': 'not_applicable',
                'reason': 'no_failures',
                'repair_applied': False,
            },
        )
    record_agent_event(
        task,
        'rerun',
        '本次未应用自动修复，因此没有触发修复后复跑',
        payload={
            'status': 'not_applicable',
            'reason': 'repair_not_applied',
            'rerun_performed': False,
        },
    )
    record_agent_event(
        task,
        'security',
        '未提供并授权独立 API 安全测试目标，本次不执行安全扫描',
        payload={
            'status': 'not_applicable',
            'reason': 'authorized_api_target_missing',
            'scan_performed': False,
        },
    )
    report_execution_ids = [execution.id for execution in executions if execution.report_path]
    record_agent_event(
        task,
        'report',
        (
            f'已生成 {len(report_execution_ids)} 份可查看的 APP 执行报告'
            if report_execution_ids else
            '本轮没有生成可查看的 APP 执行报告'
        ),
        level='success' if report_execution_ids else 'info',
        payload={
            'status': 'success' if report_execution_ids else 'not_applicable',
            'report_count': len(report_execution_ids),
            'execution_ids': report_execution_ids,
        },
    )

    task.status = 'completed'
    task.progress = 100
    task.result_summary = summary
    task.failure_analysis = analysis
    task.defect_draft = defect_drafts
    task.finished_at = timezone.now()
    task.save(update_fields=[
        'status', 'progress', 'result_summary', 'failure_analysis',
        'defect_draft', 'finished_at', 'updated_at',
    ])
    record_agent_event(
        task,
        'completed',
        analysis['summary'],
        level='warning' if summary['has_failures'] else 'success',
        payload={'status': 'success', **summary},
    )
    send_app_agent_completion_notification(task, executions, summary)


@shared_task
def execute_app_agent_task(agent_task_id: int, execute_only: bool = False):
    """规划并顺序执行 APP Agent 任务。"""
    from .agent_service import (
        analyze_agent_structured_input,
        analyze_app_agent_results,
        build_app_agent_plan,
        record_agent_event,
        validate_agent_execution,
    )
    from .models import AppAgentTask, AppTestCase, AppTestExecution
    from .runtime_service import (
        create_exploratory_runtime_session,
        queue_next_planned_exploration,
        validate_exploratory_runtime_start,
    )
    from apps.core.data_assets import APP_AGENT_DATA_ALIAS_PREFIXES, request_assets_for_case
    from apps.core.models import TestDataAssetRequirement
    from apps.core.runtime_case import RuntimeCase
    from apps.core.runtime_orchestration import RuntimeContext

    task = None
    executions = []
    try:
        task = AppAgentTask.objects.select_related(
            'user', 'project', 'device', 'app_package'
        ).get(id=agent_task_id)

        started = AppAgentTask.objects.filter(
            id=agent_task_id,
            status='pending',
        ).update(
            status='executing' if execute_only else 'planning',
            progress=20 if execute_only else 5,
            started_at=task.started_at or timezone.now(),
            finished_at=None,
            error_message='',
        )
        if not started:
            task.refresh_from_db()
            logger.info(
                'APP Agent 任务不允许启动: task_id=%s, status=%s',
                agent_task_id,
                task.status,
            )
            return
        task.refresh_from_db()

        locked_plan = (
            execute_only
            and isinstance(task.plan, dict)
            and task.plan.get('source') == 'unified_ai_test_agent'
            and task.plan.get('selected_case_ids')
        )
        if locked_plan:
            record_agent_event(
                task,
                'planning',
                '统一 AI Test Agent 已指定 APP 编排用例，执行前仅校验最新用例内容',
                payload={'case_ids': task.plan.get('selected_case_ids')},
            )
            plan = task.plan
        elif execute_only:
            # ready/failed 任务可能在计划生成后又修改过用例。真正执行前必须重新
            # 查询数据库并重新规划，避免沿用任务 JSON 中保存的旧用例选择。
            record_agent_event(
                task,
                'planning',
                '执行前正在重新读取最新 APP 用例与步骤并刷新测试计划',
            )
        else:
            record_agent_event(task, 'planning', '正在分析测试目标与项目 APP 用例资产')

        if not locked_plan:
            plan = build_app_agent_plan(task)
            task.refresh_from_db()
            if task.status == 'stopped':
                return
            task.plan = plan
        task.progress = 20
        task.save(update_fields=['plan', 'progress', 'updated_at'])

        selected_case_ids = plan.get('selected_case_ids') or []
        record_agent_event(
            task,
            'requirement_analysis',
            'AI 已分析本轮测试目标与执行范围',
            level='success',
            payload={'status': 'success'},
        )
        record_agent_event(
            task,
            'understanding',
            f'AI 已理解测试目标并识别 {len(selected_case_ids)} 个可执行 APP 场景',
            level='success',
            payload={'status': 'success', 'case_ids': selected_case_ids},
        )
        if plan.get('input_parse_required'):
            structured_input = plan.get('structured_input') or {}
            record_agent_event(
                task,
                'planning',
                f"正在解析{('前端 URL' if structured_input.get('type') == 'frontend_url' else '接口 URL')}：{structured_input.get('url')}",
                payload={'structured_input': structured_input},
            )
            analysis = analyze_agent_structured_input(task, structured_input)
            plan['structured_input'] = {**structured_input, 'analysis': analysis}
            plan['input_parse_required'] = False
            task.plan = plan
            success = analysis.get('status') == 'success'
            auto_explore = success and task.auto_execute and structured_input.get('type') == 'frontend_url'
            task.status = 'planning' if auto_explore else ('ready' if success else 'failed')
            task.progress = 35 if success else 100
            next_action = '页面解析已完成，可直接在当前模拟器上启动自动探索测试。'
            task.result_summary = {
                'total': 0,
                'passed': 0,
                'failed': 0 if success else 1,
                'skipped': 0,
                'stopped': 0,
                'pass_rate': 0,
                'execution_ids': [],
                'structured_input_analysis': True,
                'execution_completed': False,
                'needs_input': False,
                'next_action': next_action if success else '',
            }
            task.error_message = '' if success else str(analysis.get('message') or '结构化输入解析失败')
            task.finished_at = None if success else timezone.now()
            task.save(update_fields=[
                'plan', 'status', 'progress', 'result_summary', 'error_message',
                'finished_at', 'updated_at',
            ])
            record_agent_event(
                task,
                'analysis' if success else 'error',
                analysis.get('message') or ('目标解析完成' if success else '目标解析失败'),
                level='success' if success else 'error',
                payload={
                    'structured_input': structured_input,
                    'opened': analysis.get('opened') or {},
                    'visual_summary': analysis.get('visual_summary') or {},
                    'screenshot_url': analysis.get('screenshot_url') or '',
                },
            )
            if success:
                if auto_explore:
                    session = create_exploratory_runtime_session(task, {
                        'target_url': structured_input.get('url'),
                    })
                    try:
                        celery_task = execute_app_agent_runtime_task.delay(session.id)
                    except Exception as exc:
                        session.status = 'failed'
                        session.error_message = f'自动探索会话提交失败: {exc}'
                        session.finished_at = timezone.now()
                        session.save(update_fields=[
                            'status', 'error_message', 'finished_at', 'updated_at'
                        ])
                        raise
                    session.celery_task_id = celery_task.id
                    session.save(update_fields=['celery_task_id', 'updated_at'])
                    task.task_id = celery_task.id
                    task.save(update_fields=['task_id', 'updated_at'])
                else:
                    record_agent_event(
                        task,
                        'review',
                        next_action,
                        payload={
                            'reason': 'exploration_ready',
                            'execution_count': 0,
                        },
                    )
            return

        if plan.get('review_required'):
            draft = plan.get('generated_draft') or {}
            task.status = 'review_required'
            task.progress = 30
            task.save(update_fields=['status', 'progress', 'updated_at'])
            record_agent_event(
                task,
                'review',
                f"已生成 {len(draft.get('flow') or [])} 个 APP Flow 草稿步骤，等待人工评审",
                level='warning',
                payload={
                    'source': draft.get('source'),
                    'overall_confidence': draft.get('overall_confidence'),
                    'required_variables': draft.get('required_variables') or [],
                },
            )
            return

        if plan.get('coverage_gap'):
            record_agent_event(
                task,
                'test_points',
                '测试点覆盖校验未通过，存在正式用例未覆盖的目标',
                level='error',
                payload={
                    'status': 'failed',
                    'uncovered_test_points': plan.get('uncovered_test_points') or [],
                },
            )
            task.status = 'needs_input'
            task.progress = 30
            task.result_summary = {
                'total': 0,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
                'stopped': 0,
                'pass_rate': 0,
                'execution_ids': [],
                'coverage_gap': True,
                'execution_completed': False,
                'needs_input': True,
                'next_action': plan.get('coverage_message') or '请先补充可执行测试用例',
            }
            task.finished_at = timezone.now()
            task.save(update_fields=[
                'status', 'progress', 'result_summary', 'finished_at', 'updated_at'
            ])
            record_agent_event(
                task,
                'input_required',
                plan.get('coverage_message') or '没有找到可执行用例',
                level='warning',
                payload={'asset_count': plan.get('asset_count', 0)},
            )
            return

        exploration_targets = list(plan.get('exploration_targets') or [])
        if exploration_targets:
            record_agent_event(
                task,
                'exploration',
                f'已识别 {len(exploration_targets)} 个用户输入的页面目标，等待设备逐个探索',
                payload={
                    'status': 'pending',
                    'target_count': len(exploration_targets),
                    'targets': exploration_targets,
                },
            )
        else:
            record_agent_event(
                task,
                'exploration',
                '本次复用已录制 APP 用例，无需执行系统探索',
                payload={
                    'status': 'not_applicable',
                    'reason': 'recorded_cases_selected',
                },
            )
        record_agent_event(
            task,
            'planning',
            (
                f"执行前已刷新最新测试计划，共选择 {len(plan.get('selected_case_ids') or [])} 个用例"
                if execute_only else
                f"测试计划已生成，共选择 {len(plan.get('selected_case_ids') or [])} 个用例"
            ),
            level='success',
            payload={
                'status': 'success',
                'source': plan.get('source'),
                'case_ids': plan.get('selected_case_ids'),
                'fresh_content': True,
            },
        )
        record_agent_event(
            task,
            'test_points',
            (
                f'已确认 {len(plan.get("scenarios") or selected_case_ids)} 个正式用例场景'
                f'和 {len(exploration_targets)} 个页面探索目标'
                if exploration_targets else
                f'已从测试计划确认 {len(plan.get("scenarios") or selected_case_ids)} 个测试点'
            ),
            level='success',
            payload={
                'status': 'success',
                'case_ids': selected_case_ids,
                'scenario_count': len(plan.get('scenarios') or selected_case_ids),
                'exploration_target_count': len(exploration_targets),
            },
        )
        if not execute_only and not task.auto_execute:
            task.status = 'ready'
            task.save(update_fields=['status', 'updated_at'])
            record_agent_event(task, 'review', '计划等待人工确认后执行')
            return

        selected_ids = [int(item) for item in task.plan.get('selected_case_ids', [])]
        if selected_ids:
            validate_agent_execution(task)
        elif exploration_targets:
            validate_exploratory_runtime_start(task)
        else:
            raise RuntimeError('测试计划中没有可执行用例或页面探索目标')

        task.status = 'executing'
        task.progress = 25
        task.save(update_fields=['status', 'progress', 'updated_at'])
        record_agent_event(
            task,
            'execution',
            (
                f'开始在设备 {task.device.name or task.device.device_id} 上执行 '
                f'{len(selected_ids)} 个正式用例和 {len(exploration_targets)} 个页面探索目标'
            ),
            payload={
                'status': 'pending',
                'case_count': len(selected_ids),
                'exploration_target_count': len(exploration_targets),
                'device_id': task.device.device_id,
            },
        )

        if not selected_ids:
            record_agent_event(
                task,
                'data',
                '页面探索使用运行时安全默认值和需求中明确提供的输入，无需生成用例数据',
                payload={
                    'status': 'not_applicable',
                    'reason': 'exploration_only',
                },
            )

        package_name = task.app_package.package_name if task.app_package else None
        parameter_row_count = max(1, min(int(task.plan.get('test_data_count') or 1), 20))
        total_executions = max(1, len(selected_ids) * parameter_row_count)
        completed_executions = 0
        for index, case_id in enumerate(selected_ids, 1):
            task.refresh_from_db()
            if task.status == 'stopped':
                break

            # 每个用例启动前再次查询，确保多用例串行执行期间发生的保存也能生效。
            test_case = AppTestCase.objects.select_related('app_package').get(id=case_id)
            data_binding = _prepare_app_agent_data(task, test_case, parameter_row_count)
            record_agent_event(
                task,
                'data',
                (
                    f'已为 {test_case.name} 自动生成并绑定 {data_binding["row_count"]} 组测试数据'
                    if data_binding else
                    f'{test_case.name} 没有输入字段，无需生成测试数据'
                ),
                level='success' if data_binding else 'info',
                payload={
                    'status': 'success' if data_binding else 'not_applicable',
                    'case_id': test_case.id,
                    'data_binding': data_binding or {},
                },
            )
            case_rounds = data_binding['row_count'] if data_binding else 1
            for row_index in range(case_rounds):
                task.refresh_from_db()
                if task.status == 'stopped':
                    break
                execution = AppTestExecution.objects.create(
                    test_case=test_case,
                    device=task.device,
                    user=task.user,
                    status='pending',
                    runtime_override=(data_binding or {}).get('runtime_override') or [],
                )
                if data_binding:
                    TestDataAssetRequirement.objects.filter(
                        id=data_binding['requirement_id'],
                    ).update(source_asset_id=data_binding['asset_ids'][row_index])
                runtime_context, leases = request_assets_for_case(
                    target_type='app_automation',
                    case_id=test_case.id,
                    execution_type='app_automation',
                    execution_id=execution.id,
                    user=task.user,
                    runtime_context={
                        'app_agent_task_id': str(task.pk),
                        'agent_task_id': str(task.pk),
                    },
                    case_name=test_case.name,
                    skip_alias_prefixes=APP_AGENT_DATA_ALIAS_PREFIXES,
                    include_aliases=[data_binding['alias']] if data_binding else [],
                )
                RuntimeCase(test_case.ui_flow, execution.runtime_override).build(
                    context=RuntimeContext(runtime_context)
                )
                execution.runtime_context = runtime_context
                execution.save(update_fields=['runtime_context', 'runtime_override', 'updated_at'])
                executions.append(execution)
                task.execution_ids = [*task.execution_ids, execution.id]
                task.current_execution = execution
                task.progress = 25 + int(completed_executions / total_executions * 60)
                task.save(update_fields=[
                    'execution_ids', 'current_execution', 'progress', 'updated_at'
                ])
                record_agent_event(
                    task,
                    'execution',
                    f'正在执行 {test_case.name} 参数 {row_index + 1}/{case_rounds}',
                    payload={
                        'execution_id': execution.id,
                        'case_id': test_case.id,
                        'data_asset_ids': [lease.asset_id for lease in leases],
                        'data_alias': (data_binding or {}).get('alias'),
                        'fresh_content': True,
                    },
                )
                execute_app_test_task.run(
                    execution.id,
                    package_name=package_name,
                    send_completion_notification=False,
                )
                execution.refresh_from_db()
                completed_executions += 1
                if execution.status == 'completed' and execution.result == 'passed':
                    level, message = 'success', f'{test_case.name} 参数 {row_index + 1} 执行通过'
                elif execution.status == 'stopped':
                    level, message = 'warning', f'{test_case.name} 参数 {row_index + 1} 已停止'
                else:
                    level, message = 'error', f'{test_case.name} 参数 {row_index + 1} 执行失败'
                record_agent_event(task, 'execution', message, level=level, payload={
                    'execution_id': execution.id,
                    'status': execution.status,
                    'result': execution.result,
                    'report_path': execution.report_path,
                })
                task.progress = 25 + int(completed_executions / total_executions * 60)
                task.save(update_fields=['progress', 'updated_at'])

        task.refresh_from_db()
        execution_map = {
            execution.id: execution
            for execution in AppTestExecution.objects.filter(
                id__in=task.execution_ids
            ).select_related('test_case', 'test_case__app_package', 'device')
        }
        executions = [execution_map[item] for item in task.execution_ids if item in execution_map]
        summary, analysis, defect_drafts = analyze_app_agent_results(executions)

        if task.status == 'stopped':
            task.result_summary = summary
            task.failure_analysis = analysis
            task.defect_draft = defect_drafts
            task.current_execution = None
            task.finished_at = task.finished_at or timezone.now()
            task.save(update_fields=[
                'result_summary', 'failure_analysis', 'defect_draft',
                'current_execution', 'finished_at', 'updated_at',
            ])
            record_agent_event(task, 'execution', 'Agent 任务已停止', level='warning')
            return

        if exploration_targets:
            task.result_summary = {
                **summary,
                'execution_completed': False,
                'exploration': {
                    'total': len(exploration_targets),
                    'completed': 0,
                    'failed': 0,
                    'session_ids': [],
                    'results': [],
                },
            }
            task.failure_analysis = analysis
            task.defect_draft = defect_drafts
            task.current_execution = None
            task.save(update_fields=[
                'result_summary', 'failure_analysis', 'defect_draft',
                'current_execution', 'updated_at',
            ])
            queue_next_planned_exploration(task, reset=True)
            return

        finalize_app_agent_task(task.id)
    except AppAgentTask.DoesNotExist:
        logger.error('APP Agent 任务不存在: %s', agent_task_id)
    except Exception as exc:
        logger.error('APP Agent 任务执行失败: %s', exc, exc_info=True)
        if task:
            task.refresh_from_db()
            if task.status == 'stopped':
                return
            task.status = 'failed'
            task.error_message = str(exc)
            task.current_execution = None
            task.finished_at = timezone.now()
            task.save(update_fields=[
                'status', 'error_message', 'current_execution', 'finished_at', 'updated_at'
            ])
            record_agent_event(task, 'error', str(exc), level='error')


@shared_task
def execute_app_agent_runtime_task(runtime_session_id: int):
    """执行 APP Agent 三期的单步观察-决策-动作闭环。"""
    from .runtime_service import run_runtime_session

    run_runtime_session(runtime_session_id)


@shared_task
def execute_app_suite_task(suite_id, execution_ids, package_name=None, scheduled_task_id=None):
    """
    异步执行APP测试套件（顺序执行多个用例）

    Args:
        suite_id: AppTestSuite 的 ID
        execution_ids: AppTestExecution ID 列表（按执行顺序）
        package_name: 可选的应用包名覆盖
        scheduled_task_id: 可选的定时任务 ID
    """
    from .models import AppTestSuite, AppTestExecution, AppDevice
    from .executors.test_executor import AppTestExecutor

    suite = None
    device = None
    executions = []
    passed = 0
    failed = 0
    suite_stopped = False

    try:
        suite = AppTestSuite.objects.get(id=suite_id)
        executions = list(
            AppTestExecution.objects.filter(id__in=execution_ids)
            .select_related('test_case', 'test_case__app_package', 'device', 'user')
            .order_by('id')
        )
        # 按 execution_ids 排序
        exec_map = {e.id: e for e in executions}
        executions = [exec_map[eid] for eid in execution_ids if eid in exec_map]

        if not executions:
            logger.error(f"套件 {suite_id} 未找到执行记录")
            return

        device = executions[0].device
        user = executions[0].user

        # 锁定设备
        if device.status != 'locked':
            device.lock(user)
        logger.info(f"套件执行开始: {suite.name}, 设备: {device.device_id}, 共 {len(executions)} 个用例")

        for idx, execution in enumerate(executions):
            execution.refresh_from_db()
            if execution.status == 'stopped':
                suite_stopped = True
                logger.info(f"套件执行已停止: suite_id={suite_id}, execution_id={execution.id}")
                break

            test_case = execution.test_case
            if not test_case:
                execution.status = 'error'
                execution.result = None
                execution.error_message = '用例不存在'
                execution.finished_at = timezone.now()
                execution.save()
                failed += 1
                continue

            try:
                # 只允许 pending -> running，避免并发停止被覆盖。
                started_at = timezone.now()
                started = AppTestExecution.objects.filter(
                    id=execution.id,
                    status='pending',
                ).update(status='running', started_at=started_at, progress=0)
                if not started:
                    execution.refresh_from_db()
                    if execution.status == 'stopped':
                        suite_stopped = True
                        break
                    raise RuntimeError(f"执行状态不允许启动: {execution.status}")
                execution.refresh_from_db()
                send_execution_update(
                    execution.id, status='running', progress=0,
                    message=f'开始执行 ({idx + 1}/{len(executions)})'
                )

                # 确定包名
                if package_name:
                    final_pkg = package_name
                else:
                    final_pkg = (
                        test_case.app_package.package_name
                        if test_case.app_package
                        else (device.default_bundle_id or "")
                    )

                progressed = AppTestExecution.objects.filter(
                    id=execution.id,
                    status='running',
                ).update(progress=10)
                if not progressed:
                    execution.refresh_from_db()
                    if execution.status == 'stopped':
                        suite_stopped = True
                        break
                    raise RuntimeError(f"执行状态异常: {execution.status}")
                execution.progress = 10
                send_execution_update(
                    execution.id, status='running', progress=10,
                    message='正在准备测试环境'
                )

                executor = AppTestExecutor()
                report_result = executor.run_tests(
                    test_case_id=test_case.id,
                    device_id=device.device_id,
                    package_name=final_pkg,
                    execution_id=execution.id,
                    username=execution.user.username if execution.user else 'unknown',
                    platform=device.platform,
                    wda_url=device.wda_url,
                    wda_bundle_id=device.wda_bundle_id,
                )

                execution.refresh_from_db()

                # 先提取执行器返回的报告和结果，停止后也必须保存这些数据
                if report_result.get('report_path'):
                    execution.report_path = report_result['report_path']
                tr = report_result.get('test_results', {}) or {}
                execution.total_steps = tr.get('total', 0)
                execution.passed_steps = tr.get('passed', 0)
                execution.failed_steps = tr.get('failed', 0)
                execution.skipped_steps = tr.get('skipped', 0)

                if execution.status == 'stopped' or report_result.get('stopped'):
                    suite_stopped = True
                    # 套件中途停止场景：为当前被停止的用例补齐报告路径、统计、结果与通知
                    _partial = 'skipped' if execution.total_steps == 0 else (
                        'passed' if execution.failed_steps == 0 else 'failed'
                    )
                    _now = timezone.now()
                    _duration = execution.duration or (
                        (_now - execution.started_at).total_seconds()
                        if execution.started_at else 0
                    )
                    AppTestExecution.objects.filter(id=execution.id).update(
                        status='stopped',
                        report_path=execution.report_path,
                        total_steps=execution.total_steps,
                        passed_steps=execution.passed_steps,
                        failed_steps=execution.failed_steps,
                        skipped_steps=execution.skipped_steps,
                        result=_partial,
                        finished_at=execution.finished_at or _now,
                        duration=_duration,
                        progress=100,
                    )
                    execution.refresh_from_db()
                    # 已执行部分也算结果：passed/failed 按已执行步骤统计（用于通知/前端）
                    if _partial == 'passed':
                        passed += 1
                    else:
                        failed += 1
                    send_execution_update(
                        execution.id, status='stopped', progress=100,
                        message='执行已停止',
                        report_path=execution.report_path,
                        finished_at=execution.finished_at,
                        result=execution.result,
                    )
                    logger.info(
                        f"套件当前用例已按用户请求停止，已保存执行结果与报告: "
                        f"suite_id={suite_id}, execution_id={execution.id}, "
                        f"result={_partial}, has_report={bool(execution.report_path)}"
                    )
                    break

                if report_result.get('report_path'):
                    execution.report_path = report_result['report_path']

                test_results = report_result.get('test_results', {})
                execution.total_steps = test_results.get('total', 0)
                execution.passed_steps = test_results.get('passed', 0)
                execution.failed_steps = test_results.get('failed', 0)
                execution.skipped_steps = test_results.get('skipped', 0)
                if test_results.get('broken', 0):
                    logger.info(f"检测到 broken 用例 {test_results.get('broken')} 个（已计入失败统计）。")

                if execution.total_steps == 0:
                    final_result = 'skipped'
                elif execution.failed_steps == 0:
                    final_result = 'passed'
                else:
                    final_result = 'failed'
                finished_at = timezone.now()
                duration = (finished_at - execution.started_at).total_seconds()
                completed = AppTestExecution.objects.filter(
                    id=execution.id,
                    status='running',
                ).update(
                    status='completed',
                    result=final_result,
                    report_path=execution.report_path,
                    total_steps=execution.total_steps,
                    passed_steps=execution.passed_steps,
                    failed_steps=execution.failed_steps,
                    skipped_steps=execution.skipped_steps,
                    finished_at=finished_at,
                    duration=duration,
                    progress=100,
                )
                if not completed:
                    execution.refresh_from_db()
                    if execution.status == 'stopped':
                        suite_stopped = True
                        # 状态竞态：API 将状态写成 stopped 发生在 completed 落库前，
                        # 但当前用例的报告路径与步骤统计已经算出来，需要落库。
                        _partial = 'skipped' if (execution.total_steps or 0) == 0 else (
                            'passed' if (execution.failed_steps or 0) == 0 else 'failed'
                        )
                        _now_c = timezone.now()
                        _duration_c = execution.duration or (
                            (_now_c - execution.started_at).total_seconds()
                            if execution.started_at else 0
                        )
                        AppTestExecution.objects.filter(id=execution.id).update(
                            status='stopped',
                            report_path=execution.report_path,
                            total_steps=execution.total_steps,
                            passed_steps=execution.passed_steps,
                            failed_steps=execution.failed_steps,
                            skipped_steps=execution.skipped_steps,
                            result=_partial,
                            finished_at=execution.finished_at or _now_c,
                            duration=_duration_c,
                            progress=100,
                        )
                        execution.refresh_from_db()
                        if _partial == 'passed':
                            passed += 1
                        else:
                            failed += 1
                        send_execution_update(
                            execution.id, status='stopped', progress=100,
                            message='执行已停止',
                            report_path=execution.report_path,
                            finished_at=execution.finished_at,
                            result=execution.result,
                        )
                        logger.info(
                            f"套件用例状态竞态停止: 已补齐结果与报告 "
                            f"suite_id={suite_id}, execution_id={execution.id}"
                        )
                        break
                    raise RuntimeError(f"执行状态异常: {execution.status}")
                execution.refresh_from_db()

                if execution.result == 'passed':
                    passed += 1
                else:
                    failed += 1

                send_execution_update(
                    execution.id, status=execution.status, progress=100,
                    message='执行完成',
                    report_path=execution.report_path,
                    finished_at=execution.finished_at,
                    result=execution.result,
                )

                logger.info(f"用例 {test_case.name} 执行完成: status={execution.status}, result={execution.result}")

            except Exception as e:
                logger.error(f"用例 {test_case.name} 执行失败: {str(e)}", exc_info=True)
                execution.refresh_from_db()
                if execution.status == 'stopped':
                    suite_stopped = True
                    # 停止场景下捕获异常：不写 error，但仍尝试补齐报告、发送最终通知。
                    logger.info(
                        f"套件用例停止+异常（非致命），仍尝试补齐结果与报告: "
                        f"execution_id={execution.id}, error={e}"
                    )
                    try:
                        if not execution.report_path:
                            _ex = AppTestExecutor()
                            _rp = _ex._generate_allure_report(execution_id=execution.id)
                            if _rp:
                                execution.report_path = _rp
                        _p_result = (
                            'skipped' if (execution.total_steps or 0) == 0
                            else ('passed' if (execution.failed_steps or 0) == 0 else 'failed')
                        )
                        _f_updates = {
                            'report_path': execution.report_path,
                            'progress': 100,
                        }
                        if execution.result is None:
                            _f_updates['result'] = _p_result
                        if not execution.finished_at:
                            _fn = timezone.now()
                            _f_updates['finished_at'] = _fn
                            if execution.started_at and not execution.duration:
                                _f_updates['duration'] = (
                                    _fn - execution.started_at
                                ).total_seconds()
                        AppTestExecution.objects.filter(id=execution.id).update(**_f_updates)
                        execution.refresh_from_db()
                        # 统计计入：停止异常场景的已执行步骤仍算结果
                        if _p_result == 'passed':
                            passed += 1
                        else:
                            failed += 1
                        send_execution_update(
                            execution.id, status='stopped', progress=100,
                            message='执行已停止',
                            report_path=execution.report_path,
                            finished_at=execution.finished_at,
                            result=execution.result,
                        )
                    except Exception as finalize_err:
                        logger.warning(
                            f"套件用例停止+异常场景补齐失败（忽略）: "
                            f"execution_id={execution.id}, error={finalize_err}"
                        )
                    break
                execution.status = 'error'
                execution.result = None
                execution.error_message = str(e)
                execution.finished_at = timezone.now()
                if execution.started_at:
                    execution.duration = (execution.finished_at - execution.started_at).total_seconds()
                execution.save()
                failed += 1
                send_execution_update(
                    execution.id, status='error',
                    progress=execution.progress or 0,
                    message=str(e),
                    finished_at=execution.finished_at,
                    result=None,
                )

        if suite_stopped:
            stopped_at = timezone.now()
            AppTestExecution.objects.filter(
                id__in=execution_ids,
                status__in=['pending', 'running'],
            ).update(status='stopped', finished_at=stopped_at)
            suite.execution_status = 'stopped'
            suite.execution_result = None
            suite.passed_count = passed
            suite.failed_count = failed
            suite.last_run_at = stopped_at
            suite.save(update_fields=[
                'execution_status', 'execution_result', 'passed_count',
                'failed_count', 'last_run_at'
            ])
            logger.info(f"套件执行已停止: {suite.name}, 已通过: {passed}, 已失败: {failed}")
            return

        # 更新套件统计
        suite.execution_status = 'completed'
        if passed == 0 and failed == 0:
            suite.execution_result = 'skipped'
        elif failed == 0:
            suite.execution_result = 'passed'
        else:
            suite.execution_result = 'failed'
        suite.passed_count = passed
        suite.failed_count = failed
        suite.last_run_at = timezone.now()
        suite.save(update_fields=['execution_status', 'execution_result', 'passed_count', 'failed_count', 'last_run_at'])

        logger.info(f"套件执行完成: {suite.name}, 通过: {passed}, 失败: {failed}")

        # 定时任务通知
        if scheduled_task_id:
            try:
                from .models import AppScheduledTask
                st = AppScheduledTask.objects.get(id=scheduled_task_id)
                is_success = failed == 0
                if is_success:
                    st.successful_runs += 1
                else:
                    st.failed_runs += 1
                st.last_result = {
                    'status': suite.execution_status,
                    'result': suite.execution_result,
                    'message': f'通过: {passed}, 失败: {failed}',
                    'execution_ids': [execution.id for execution in executions],
                }
                st.save(update_fields=['successful_runs', 'failed_runs', 'last_result'])
                send_scheduled_task_notification(scheduled_task_id, success=is_success)
            except Exception as ne:
                logger.error(f"更新定时任务状态失败: {ne}")
        else:
            send_manual_suite_completion_notification(suite, executions, passed, failed)

    except AppTestSuite.DoesNotExist:
        logger.error(f"测试套件不存在: {suite_id}")
    except Exception as e:
        logger.error(f"执行套件失败: {str(e)}", exc_info=True)
        if suite:
            suite.refresh_from_db()
            if suite.execution_status == 'stopped':
                logger.info(f"套件停止后忽略后续异常: suite_id={suite_id}, error={e}")
                return
            suite.execution_status = 'error'
            suite.execution_result = None
            suite.failed_count = failed
            suite.passed_count = passed
            suite.last_run_at = timezone.now()
            suite.save(update_fields=['execution_status', 'execution_result', 'passed_count', 'failed_count', 'last_run_at'])
            if scheduled_task_id:
                try:
                    from .models import AppScheduledTask
                    st = AppScheduledTask.objects.get(id=scheduled_task_id)
                    st.failed_runs += 1
                    st.last_result = {
                        'status': suite.execution_status,
                        'result': suite.execution_result,
                        'message': str(e),
                        'execution_ids': [execution.id for execution in executions],
                    }
                    st.save(update_fields=['failed_runs', 'last_result'])
                    send_scheduled_task_notification(scheduled_task_id, success=False)
                except Exception as ne:
                    logger.error(f"更新套件定时任务异常状态失败: {ne}")
            else:
                send_manual_suite_failure_notification(suite, executions, passed, failed or 1, reason=str(e))
    finally:
        # 释放设备
        try:
            if device:
                device.refresh_from_db()
                if device.status == 'locked':
                    device.unlock()
                    logger.info(f"设备已释放: {device.device_id}")
        except Exception as e:
            logger.error(f"释放设备失败: {str(e)}")


@shared_task
def check_and_release_expired_devices():
    """
    检查并释放过期锁定的设备
    """
    from .models import AppDevice
    
    try:
        devices = AppDevice.objects.filter(status='locked')
        released_count = 0
        
        for device in devices:
            if device.is_lock_expired():
                device.unlock()
                released_count += 1
                logger.info(f"释放过期锁定的设备: {device.device_id}")
        
        logger.info(f"检查设备锁定完成，释放 {released_count} 个设备")
        
    except Exception as e:
        logger.error(f"检查设备锁定失败: {str(e)}", exc_info=True)


@worker_ready.connect
def recover_app_executions_on_worker_ready(sender=None, **kwargs):
    """Worker 重启后收口被旧进程打断的 APP 自动化执行。"""
    try:
        recovered = recover_interrupted_app_executions()
        if recovered:
            logger.warning(f"APP自动化 Worker 启动清理中断执行: {recovered} 条")
    except Exception as exc:
        logger.error(f"APP自动化 Worker 启动清理失败: {exc}", exc_info=True)
