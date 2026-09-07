import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path, PurePosixPath

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings

from .skill_service import SkillPackageError, detect_execution_config


class SkillRuntimeError(RuntimeError):
    pass


def normalize_skill_runtime_error(message):
    text = str(message or '').strip()
    upper_text = text.upper()
    if 'DAILY_LIMIT_EXCEEDED' in upper_text or 'DAILY USAGE LIMIT' in upper_text:
        return (
            'Skill 调用 AI 模型失败：当前模型账号今日额度已用完。'
            '请补充额度、等待供应商日限额重置，或切换其他可用模型。'
        )
    if '429 TOO MANY REQUESTS' in upper_text or 'RATE LIMIT' in upper_text:
        return 'Skill 调用 AI 模型失败：请求过于频繁或额度不足，请稍后再试或切换模型。'
    if 'UNSUPPORTED' in upper_text and ('RESPONSE_FORMAT' in upper_text or 'PARAMETER' in upper_text):
        return 'Skill 调用 AI 模型失败：当前模型接口不支持 Skill 使用的结构化输出参数，请切换兼容模型或调整 Skill 脚本。'
    if 'AUTHENTICATION' in upper_text or 'INVALID_API_KEY' in upper_text or 'INCORRECT API KEY' in upper_text:
        return 'Skill 调用 AI 模型失败：模型 API Key 无效或已过期，请检查模型配置。'
    if 'CONNECTION' in upper_text or 'CONNECTERROR' in upper_text:
        return 'Skill 调用 AI 模型失败：无法连接模型服务，请检查 Base URL、网络或模型服务状态。'
    if 'Traceback (most recent call last):' in text:
        import re
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if re.search(
                    r'(?:Error|Exception|Timeout|failed|HTTP\s*[45]\d{2}|拒绝|失败|超时)',
                    line,
                    re.IGNORECASE,
            ) and not line.startswith('Traceback'):
                return f'Skill脚本执行失败：{line}'[-1000:]
        return 'Skill脚本调用模型失败，但 Runner 未返回完整错误。请重试并检查模型接口与 Skill 参数兼容性。'
    return text[-4000:]


def resolve_snapshot_execution(snapshot):
    execution = snapshot.get('execution') or {}
    if execution:
        return execution
    files = snapshot.get('files') or []
    file_paths = [item.get('path') for item in files if item.get('path')]
    skill_text = next(
        (str(item.get('content') or '') for item in files if item.get('path') == 'SKILL.md'),
        '',
    )
    return detect_execution_config(file_paths, skill_text)


def get_effective_skill_timeout(execution):
    requested_timeout = max(1, int((execution or {}).get('timeout_seconds') or 900))
    maximum_timeout = int(
        getattr(settings, 'SKILL_RUNNER_MAX_EXECUTION_SECONDS', 900) or 0
    )
    if maximum_timeout <= 0:
        return requested_timeout
    return min(requested_timeout, maximum_timeout)


def _safe_relative_path(raw_path):
    value = str(raw_path or '').replace('\\', '/')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {'', '.', '..'} for part in path.parts):
        raise SkillRuntimeError(f'Skill文件路径无效: {raw_path}')
    return path


def _load_package_source(snapshot):
    storage_path = str(snapshot.get('storage_path') or '')
    manifest = snapshot.get('package_manifest') or []
    if not storage_path or not manifest:
        skill_id = snapshot.get('id')
        if not skill_id:
            raise SkillRuntimeError('Skill快照缺少服务器执行包信息')
        from .models import TestSkill

        skill = TestSkill.objects.filter(pk=skill_id).only(
            'storage_path', 'manifest', 'content_hash', 'execution_config'
        ).first()
        if not skill:
            raise SkillRuntimeError('Skill执行包已不存在，请重新选择 Skill 创建任务')
        storage_path = skill.storage_path
        manifest = skill.manifest or []
        if not snapshot.get('execution') and skill.execution_config:
            snapshot['execution'] = skill.execution_config

    media_root = Path(settings.MEDIA_ROOT).resolve()
    skill_root = (media_root / storage_path).resolve()
    skills_root = media_root / 'ai_skills'
    if skills_root not in skill_root.parents or not skill_root.is_dir():
        raise SkillRuntimeError('Skill服务器执行目录无效或已被删除')
    return skill_root, manifest


def _build_package_archive(snapshot):
    skill_root, manifest = _load_package_source(snapshot)
    archive_buffer = io.BytesIO()
    digest = hashlib.sha256()
    with zipfile.ZipFile(archive_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(manifest, key=lambda value: str(value.get('path') or '')):
            relative = _safe_relative_path(item.get('path'))
            target = skill_root.joinpath(*relative.parts).resolve()
            if skill_root not in target.parents or not target.is_file():
                raise SkillRuntimeError(f'Skill文件不存在: {relative.as_posix()}')
            content = target.read_bytes()
            expected_hash = str(item.get('sha256') or '')
            actual_hash = hashlib.sha256(content).hexdigest()
            if expected_hash and actual_hash != expected_hash:
                raise SkillRuntimeError(f'Skill文件已发生变化: {relative.as_posix()}')
            digest.update(relative.as_posix().encode('utf-8'))
            digest.update(b'\0')
            digest.update(content)
            archive.writestr(relative.as_posix(), content)
    expected_package_hash = str(snapshot.get('content_hash') or '')
    if expected_package_hash and digest.hexdigest() != expected_package_hash:
        raise SkillRuntimeError('Skill执行包完整性校验失败')
    return archive_buffer.getvalue(), len(manifest)


def _resolve_api_key(model_config):
    getter = getattr(model_config, 'get_api_key', None)
    return str(getter() if callable(getter) else getattr(model_config, 'api_key', '') or '')


def _build_model_environment(model_config):
    if not model_config:
        return {}
    api_key = _resolve_api_key(model_config)
    base_url = str(getattr(model_config, 'base_url', '') or '')
    model_name = str(getattr(model_config, 'model_name', '') or '')
    return {
        'DEEPSEEK_API_KEY': api_key,
        'DEEPSEEK_BASE_URL': base_url,
        'DEEPSEEK_MODEL': model_name,
        'OPENAI_API_KEY': api_key,
        'OPENAI_BASE_URL': base_url,
        'OPENAI_MODEL': model_name,
    }


async def execute_skill_snapshot(snapshot, input_text, original_requirement, model_config):
    execution = dict(resolve_snapshot_execution(snapshot))
    if execution.get('mode') != 'server':
        raise SkillRuntimeError('当前 Skill 没有服务器脚本入口')
    execution['timeout_seconds'] = get_effective_skill_timeout(execution)
    package_bytes, package_file_count = await sync_to_async(
        _build_package_archive,
        thread_sensitive=True,
    )(snapshot)
    payload = {
        'package_base64': base64.b64encode(package_bytes).decode('ascii'),
        'execution': execution,
        'input_text': str(input_text or ''),
        'original_requirement': str(original_requirement or ''),
        'environment': _build_model_environment(model_config),
    }
    runner_url = str(getattr(settings, 'SKILL_RUNNER_URL', '') or '').rstrip('/')
    if not runner_url:
        raise SkillRuntimeError('Skill Runner 未配置，无法实际执行 Skill 脚本')
    token = str(getattr(settings, 'SKILL_RUNNER_TOKEN', '') or '')
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    configured_timeout = int(getattr(settings, 'SKILL_RUNNER_REQUEST_TIMEOUT_SECONDS', 0) or 0)
    request_timeout = configured_timeout if configured_timeout > 0 else execution['timeout_seconds'] + 15
    timeout = httpx.Timeout(request_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f'{runner_url}/execute', json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise SkillRuntimeError(
            f'Skill Runner 超过 {request_timeout} 秒未返回，已停止等待'
        ) from exc
    except httpx.HTTPError as exc:
        raise SkillRuntimeError(f'Skill Runner 连接失败: {exc}') from exc
    try:
        result = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise SkillRuntimeError(f'Skill Runner 返回了无效响应（HTTP {response.status_code}）') from exc
    if response.status_code >= 400:
        raise SkillRuntimeError(str(result.get('error') or f'Skill Runner HTTP {response.status_code}'))
    if not result.get('success'):
        message = str(result.get('stderr') or result.get('error') or 'Skill执行失败').strip()
        if result.get('timed_out'):
            message = f"Skill执行超时（{execution.get('timeout_seconds')}秒）"
        raise SkillRuntimeError(normalize_skill_runtime_error(message))
    output = str(result.get('main_output') or '').strip()
    if not output:
        raise SkillRuntimeError('Skill脚本执行成功，但没有返回可用输出')
    result['main_output'] = output
    result['package_file_count'] = result.get('package_file_count') or package_file_count
    return result
