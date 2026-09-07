import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import yaml
from django.conf import settings
from django.utils.text import slugify


MAX_SKILL_FILES = 120
MAX_SKILL_FILE_SIZE = 2 * 1024 * 1024
MAX_SKILL_TOTAL_SIZE = 12 * 1024 * 1024
MAX_SKILL_CONTEXT_SIZE = 80_000
ALLOWED_EXTENSIONS = {
    '.md', '.markdown', '.txt', '.json', '.yaml', '.yml', '.csv', '.xml',
    '.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.html', '.css',
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.pdf',
}
CONTEXT_EXTENSIONS = {
    '.md', '.markdown', '.txt', '.json', '.yaml', '.yml', '.csv', '.xml',
    '.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.html', '.css',
}
SUPPORTED_EXECUTION_RUNTIMES = {'python', 'shell', 'node', 'command'}
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 900


class SkillPackageError(ValueError):
    pass


def _normalized_path(raw_path):
    value = str(raw_path or '').replace('\\', '/').strip()
    raw_parts = value.split('/')
    path = PurePosixPath(value)
    if (
        not value
        or '\0' in value
        or path.is_absolute()
        or any(part in {'', '.', '..'} for part in raw_parts)
        or (raw_parts and raw_parts[0].endswith(':'))
    ):
        raise SkillPackageError(f'Skill包含不安全的文件路径: {raw_path}')
    return path


def _is_hidden_path(path):
    return any(part.startswith('.') and part not in {'.well-known'} for part in path.parts)


def _is_generated_cache_path(path):
    return '__pycache__' in path.parts or Path(path.name).suffix.lower() in {'.pyc', '.pyo'}


def _safe_path(raw_path):
    path = _normalized_path(raw_path)
    if _is_hidden_path(path):
        raise SkillPackageError(f'Skill不支持隐藏文件或目录: {raw_path}')
    suffix = Path(path.name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise SkillPackageError(f'Skill不支持文件类型: {path.name}')
    return path.as_posix()


def _safe_upload_path(raw_path):
    path = _normalized_path(raw_path)
    if _is_hidden_path(path) or _is_generated_cache_path(path):
        return None
    suffix = Path(path.name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise SkillPackageError(f'Skill不支持文件类型: {path.name}')
    return path.as_posix()


def _validate_package(files):
    if not files:
        raise SkillPackageError('请选择包含 SKILL.md 的文件夹或 ZIP 文件')
    if len(files) > MAX_SKILL_FILES:
        raise SkillPackageError(f'Skill文件数量不能超过 {MAX_SKILL_FILES} 个')

    total_size = 0
    normalized = {}
    normalized_paths = set()
    for raw_path, content in files.items():
        path = _safe_path(raw_path)
        normalized_path = path.casefold()
        if normalized_path in normalized_paths:
            raise SkillPackageError(f'Skill包含重复文件: {path}')
        if len(content) > MAX_SKILL_FILE_SIZE:
            raise SkillPackageError(f'Skill单个文件不能超过 2MB: {path}')
        total_size += len(content)
        if total_size > MAX_SKILL_TOTAL_SIZE:
            raise SkillPackageError('Skill文件总大小不能超过 12MB')
        normalized[path] = content
        normalized_paths.add(normalized_path)

    entrypoints = [path for path in normalized if Path(path).name.lower() == 'skill.md']
    if len(entrypoints) != 1:
        raise SkillPackageError('Skill根目录必须且只能包含一个 SKILL.md')

    entrypoint = entrypoints[0]
    prefix = PurePosixPath(entrypoint).parent
    if str(prefix) != '.':
        prefix_parts = prefix.parts
        stripped = {}
        for path, content in normalized.items():
            parts = PurePosixPath(path).parts
            if parts[:len(prefix_parts)] != prefix_parts:
                raise SkillPackageError('所有 Skill 文件必须位于 SKILL.md 所在目录内')
            relative = PurePosixPath(*parts[len(prefix_parts):]).as_posix()
            stripped[relative] = content
        normalized = stripped

    actual_entrypoint = next(path for path in normalized if path.lower() == 'skill.md')
    if actual_entrypoint != 'SKILL.md':
        normalized['SKILL.md'] = normalized.pop(actual_entrypoint)

    return normalized, total_size


def _read_zip(upload):
    files = {}
    normalized_paths = set()
    declared_total_size = 0
    try:
        with zipfile.ZipFile(upload) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.filename.startswith('__MACOSX/') or info.filename.endswith('/.DS_Store'):
                    continue
                safe_path = _safe_upload_path(info.filename)
                if safe_path is None:
                    continue
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    raise SkillPackageError(f'Skill ZIP 不支持软链接: {info.filename}')
                if len(files) >= MAX_SKILL_FILES:
                    raise SkillPackageError(f'Skill文件数量不能超过 {MAX_SKILL_FILES} 个')
                if info.file_size > MAX_SKILL_FILE_SIZE:
                    raise SkillPackageError(f'Skill单个文件不能超过 2MB: {info.filename}')
                declared_total_size += info.file_size
                if declared_total_size > MAX_SKILL_TOTAL_SIZE:
                    raise SkillPackageError('Skill文件总大小不能超过 12MB')
                normalized_path = safe_path.casefold()
                if normalized_path in normalized_paths:
                    raise SkillPackageError(f'Skill包含重复文件: {safe_path}')
                files[safe_path] = archive.read(info)
                normalized_paths.add(normalized_path)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise SkillPackageError('Skill ZIP 文件无效或已损坏') from exc
    return files


def read_uploaded_package(uploads, relative_paths=None):
    uploads = list(uploads or [])
    relative_paths = list(relative_paths or [])
    package_name_hint = ''
    if len(uploads) == 1 and str(uploads[0].name).lower().endswith('.zip'):
        package_name_hint = Path(str(uploads[0].name)).stem
        files = _read_zip(uploads[0])
    else:
        if relative_paths and len(relative_paths) != len(uploads):
            raise SkillPackageError('上传文件与相对路径数量不一致')
        files = {}
        normalized_paths = set()
        declared_total_size = 0
        for index, upload in enumerate(uploads):
            path = relative_paths[index] if relative_paths else upload.name
            safe_path = _safe_upload_path(path)
            if safe_path is None:
                continue
            if len(files) >= MAX_SKILL_FILES:
                raise SkillPackageError(f'Skill文件数量不能超过 {MAX_SKILL_FILES} 个')
            if not package_name_hint:
                path_parts = PurePosixPath(safe_path).parts
                if len(path_parts) > 1:
                    package_name_hint = path_parts[0]
            normalized_path = safe_path.casefold()
            if normalized_path in normalized_paths:
                raise SkillPackageError(f'Skill包含重复文件: {safe_path}')
            upload_size = int(getattr(upload, 'size', 0) or 0)
            if upload_size > MAX_SKILL_FILE_SIZE:
                raise SkillPackageError(f'Skill单个文件不能超过 2MB: {safe_path}')
            declared_total_size += upload_size
            if declared_total_size > MAX_SKILL_TOTAL_SIZE:
                raise SkillPackageError('Skill文件总大小不能超过 12MB')
            files[safe_path] = upload.read()
            normalized_paths.add(normalized_path)

    files, total_size = _validate_package(files)
    skill_text = files['SKILL.md'].decode('utf-8-sig', errors='strict')
    metadata = parse_skill_metadata(skill_text, fallback_name=package_name_hint)
    manifest = []
    digest = hashlib.sha256()
    for path in sorted(files):
        content = files[path]
        file_hash = hashlib.sha256(content).hexdigest()
        digest.update(path.encode('utf-8'))
        digest.update(b'\0')
        digest.update(content)
        manifest.append({
            'path': path,
            'size': len(content),
            'sha256': file_hash,
            'content_type': mimetypes.guess_type(path)[0] or 'application/octet-stream',
            'context_enabled': Path(path).suffix.lower() in CONTEXT_EXTENSIONS,
        })
    return {
        'files': files,
        'metadata': metadata,
        'execution_config': detect_execution_config(files, skill_text),
        'manifest': manifest,
        'total_size': total_size,
        'content_hash': digest.hexdigest(),
    }


def _markdown_metadata_fallback(skill_text, fallback_name=''):
    body = str(skill_text or '')
    heading_match = re.search(r'^\s*#\s+(.+?)\s*$', body, flags=re.MULTILINE)
    heading = heading_match.group(1).strip().strip('#').strip() if heading_match else ''

    description = ''
    paragraphs = re.split(r'\n\s*\n', body)
    for paragraph in paragraphs:
        candidate = paragraph.strip()
        if not candidate or candidate.startswith(('#', '---', '```')):
            continue
        candidate = re.sub(r'^[-*+]\s+', '', candidate, flags=re.MULTILINE)
        candidate = re.sub(r'\s+', ' ', candidate).strip()
        if candidate:
            description = candidate
            break

    name = heading or str(fallback_name or '').strip() or 'Imported Skill'
    if not description:
        description = f'从 {name} 的 SKILL.md 导入，详细能力请查看 Skill 内容。'
    return name, description


def _parse_skill_frontmatter(skill_text):
    skill_text = str(skill_text or '').lstrip('\ufeff')
    metadata = {}
    normalized_text = skill_text.lstrip()
    body_text = normalized_text
    if normalized_text.startswith('---'):
        parts = normalized_text.split('---', 2)
        if len(parts) == 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as exc:
                raise SkillPackageError(f'SKILL.md 元数据格式无效: {exc}') from exc
            body_text = parts[2]
    if not isinstance(metadata, dict):
        raise SkillPackageError('SKILL.md 元数据必须是对象')
    return metadata, body_text


def parse_skill_metadata(skill_text, fallback_name=''):
    metadata, body_text = _parse_skill_frontmatter(skill_text)
    inferred_name, inferred_description = _markdown_metadata_fallback(
        body_text,
        fallback_name=fallback_name,
    )
    name = str(
        metadata.get('name')
        or metadata.get('title')
        or metadata.get('display_name')
        or inferred_name
    ).strip()
    description = str(
        metadata.get('description')
        or metadata.get('summary')
        or metadata.get('about')
        or inferred_description
    ).strip()
    identifier = slugify(name, allow_unicode=False) or hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]
    tags = metadata.get('tags') or []
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(',') if item.strip()]
    if not isinstance(tags, list):
        raise SkillPackageError('SKILL.md 的 tags 必须是数组或逗号分隔文本')
    return {
        'name': name[:120],
        'identifier': identifier[:120],
        'version': str(metadata.get('version') or '1.0.0').strip()[:40],
        'description': description[:2000],
        'tags': [str(item).strip()[:40] for item in tags[:20] if str(item).strip()],
    }


def _validate_execution_path(raw_path, files, *, label='入口文件'):
    try:
        path = _normalized_path(raw_path).as_posix()
    except SkillPackageError as exc:
        raise SkillPackageError(f'Skill execution {label}无效: {raw_path}') from exc
    if path not in files:
        raise SkillPackageError(f'Skill execution {label}不存在: {path}')
    return path


def detect_execution_config(files, skill_text=''):
    """Resolve an explicit execution declaration or a conservative conventional entrypoint."""
    file_paths = set(files.keys() if isinstance(files, dict) else files or [])
    metadata = {}
    if skill_text:
        metadata, _ = _parse_skill_frontmatter(skill_text)
    declared = metadata.get('execution')
    if declared is not None and not isinstance(declared, dict):
        raise SkillPackageError('SKILL.md 的 execution 必须是对象')

    if declared:
        runtime = str(declared.get('runtime') or '').strip().lower()
        entrypoint = str(declared.get('entrypoint') or '').strip()
        command = declared.get('command') or []
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or any(not isinstance(item, (str, int, float)) for item in command):
            raise SkillPackageError('Skill execution command 必须是字符串数组')
        command = [str(item) for item in command]
        if not runtime:
            suffix = Path(entrypoint).suffix.lower()
            runtime = {'.py': 'python', '.sh': 'shell', '.js': 'node'}.get(suffix, 'command')
        if runtime not in SUPPORTED_EXECUTION_RUNTIMES:
            raise SkillPackageError(f'Skill execution runtime 不支持: {runtime}')
        if entrypoint:
            entrypoint = _validate_execution_path(entrypoint, file_paths)
        elif runtime != 'command':
            raise SkillPackageError('Skill execution 必须声明 entrypoint')
        if runtime == 'command' and not command:
            raise SkillPackageError('command runtime 必须声明 command')

        args = declared.get('args') or []
        if isinstance(args, str):
            args = [args]
        if not isinstance(args, list) or any(not isinstance(item, (str, int, float)) for item in args):
            raise SkillPackageError('Skill execution args 必须是字符串数组')
        output_globs = declared.get('output_globs') or ['output/**/*']
        if isinstance(output_globs, str):
            output_globs = [output_globs]
        if not isinstance(output_globs, list) or any(not isinstance(item, str) for item in output_globs):
            raise SkillPackageError('Skill execution output_globs 必须是字符串数组')
        for pattern in output_globs:
            normalized = pattern.replace('\\', '/')
            if normalized.startswith('/') or '..' in PurePosixPath(normalized).parts:
                raise SkillPackageError(f'Skill execution 输出路径无效: {pattern}')
        try:
            timeout_seconds = int(declared.get('timeout_seconds') or DEFAULT_EXECUTION_TIMEOUT_SECONDS)
        except (TypeError, ValueError) as exc:
            raise SkillPackageError('Skill execution timeout_seconds 必须是整数') from exc
        return {
            'mode': 'server',
            'runtime': runtime,
            'entrypoint': entrypoint,
            'command': command,
            'args': [str(item) for item in args],
            'output_globs': output_globs[:32],
            'timeout_seconds': max(1, min(timeout_seconds, 3600)),
            'install_dependencies': bool(declared.get('install_dependencies', True)),
        }

    conventional_entrypoints = [
        ('scripts/skill.py', 'python', ['generate', '-i', '{input_file}']),
        ('skill.py', 'python', ['generate', '-i', '{input_file}']),
        ('scripts/run.py', 'python', ['{input_file}']),
        ('run.py', 'python', ['{input_file}']),
        ('scripts/run.sh', 'shell', ['{input_file}']),
        ('run.sh', 'shell', ['{input_file}']),
        ('scripts/skill.js', 'node', ['{input_file}']),
        ('skill.js', 'node', ['{input_file}']),
    ]
    for entrypoint, runtime, args in conventional_entrypoints:
        if entrypoint in file_paths:
            return {
                'mode': 'server',
                'runtime': runtime,
                'entrypoint': entrypoint,
                'command': [],
                'args': args,
                'output_globs': [
                    'output/*.md', 'output/**/*.md',
                    'output/*.csv', 'output/**/*.csv',
                    'output/*.json', 'output/**/*.json',
                ],
                'timeout_seconds': DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                'install_dependencies': True,
                'detected': True,
            }
    return {
        'mode': 'model',
        'runtime': 'model',
        'entrypoint': 'SKILL.md',
        'command': [],
        'args': [],
        'output_globs': [],
        'timeout_seconds': DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        'install_dependencies': False,
    }


def persist_package(package):
    skills_root = Path(settings.MEDIA_ROOT).resolve() / 'ai_skills'
    skills_root.mkdir(parents=True, exist_ok=True)
    storage_name = uuid.uuid4().hex
    final_dir = skills_root / storage_name
    temp_dir = Path(tempfile.mkdtemp(prefix='.upload-', dir=skills_root))
    try:
        for relative_path, content in package['files'].items():
            target = (temp_dir / relative_path).resolve()
            if temp_dir not in target.parents:
                raise SkillPackageError(f'Skill包含不安全的文件路径: {relative_path}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        os.replace(temp_dir, final_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return f'ai_skills/{storage_name}'


def remove_package(storage_path):
    media_root = Path(settings.MEDIA_ROOT).resolve()
    target = (media_root / str(storage_path or '')).resolve()
    skills_root = media_root / 'ai_skills'
    if target != skills_root and skills_root in target.parents:
        shutil.rmtree(target, ignore_errors=True)


def read_skill_file(skill, relative_path):
    allowed_paths = {item.get('path') for item in (skill.manifest or [])}
    safe_path = _safe_path(relative_path)
    if safe_path not in allowed_paths:
        raise SkillPackageError('Skill文件不存在')
    media_root = Path(settings.MEDIA_ROOT).resolve()
    skill_root = (media_root / skill.storage_path).resolve()
    target = (skill_root / safe_path).resolve()
    if skill_root not in target.parents:
        raise SkillPackageError('Skill文件路径无效')
    return target.read_bytes()


def build_skill_snapshot(skill):
    files = []
    remaining = MAX_SKILL_CONTEXT_SIZE
    manifest_by_path = {item['path']: item for item in (skill.manifest or [])}
    ordered_paths = ['SKILL.md'] + sorted(
        path for path, item in manifest_by_path.items()
        if path != 'SKILL.md' and item.get('context_enabled')
    )
    for path in ordered_paths:
        item = manifest_by_path.get(path)
        if not item or remaining <= 0:
            break
        content = read_skill_file(skill, path).decode('utf-8', errors='replace')
        content = content[:remaining]
        remaining -= len(content)
        files.append({
            'path': path,
            'sha256': item['sha256'],
            'size': item['size'],
            'content': content,
        })
    execution_config = getattr(skill, 'execution_config', None) or detect_execution_config(
        manifest_by_path.keys(),
        next((item['content'] for item in files if item['path'] == 'SKILL.md'), ''),
    )
    return {
        'id': skill.id,
        'name': skill.name,
        'identifier': skill.identifier,
        'command': skill.command,
        'version': skill.version,
        'description': skill.description,
        'tags': skill.tags,
        'project_id': skill.project_id,
        'content_hash': skill.content_hash,
        'storage_path': getattr(skill, 'storage_path', ''),
        'execution': execution_config,
        'package_manifest': list(manifest_by_path.values()),
        'package_file_count': len(manifest_by_path),
        'package_total_size': getattr(skill, 'total_size', 0),
        'files': files,
    }
