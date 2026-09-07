"""流式生成大批量测试数据；登记为资产时按批次保存真实行，运行时按索引读取。"""

import csv
import datetime as dt
import hashlib
import json
import random
import re
import string
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from fakerx import FakerX

from .models import BulkTestDataJob, BulkTestDataRow


MAX_COUNT = 10_000_000
MAX_FIELDS = 50
MIN_BATCH_SIZE = 1_000
MAX_BATCH_SIZE = 100_000
SUPPORTED_SQL_DIALECTS = {'mysql', 'postgresql', 'sqlite'}
SUPPORTED_FIELD_TYPES = {
    'sequence', 'fixed', 'integer', 'decimal', 'string', 'boolean', 'uuid',
    'name', 'phone', 'verification_code', 'email', 'address', 'company', 'id_card', 'date',
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _normalize_asset_config(data: Dict[str, Any], job_name: str) -> Dict[str, Any]:
    raw = data.get('asset_config') if isinstance(data.get('asset_config'), dict) else {}
    save_to_asset = _as_bool(raw.get('save_to_asset', data.get('save_to_asset')), False)
    if not save_to_asset:
        return {'save_to_asset': False}

    tags = raw.get('tags', data.get('asset_tags', []))
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(',') if item.strip()]
    if not isinstance(tags, list):
        raise ValueError('数据资产标签必须是数组或逗号分隔字符串')

    auto_create_requirement = _as_bool(
        raw.get('auto_create_requirement', data.get('auto_create_requirement')),
        False,
    )
    target_case_id = raw.get('target_case_id', data.get('target_case_id'))
    alias = str(raw.get('alias', data.get('alias')) or '').strip()
    if auto_create_requirement:
        try:
            target_case_id = int(target_case_id)
        except (TypeError, ValueError) as exc:
            raise ValueError('自动绑定数据资产时必须填写有效的用例ID') from exc
        if target_case_id < 1 or not alias:
            raise ValueError('自动绑定数据资产时必须填写用例ID和数据别名')

    target_type = str(raw.get('target_type', data.get('target_type')) or '').strip()
    if target_type and target_type not in {'api_automation', 'ui_automation', 'app_automation'}:
        raise ValueError('数据资产绑定类型只支持接口、UI或APP自动化')

    release_policy = str(raw.get('release_policy', data.get('release_policy')) or 'release').strip()
    if release_policy not in {'release', 'mark_used', 'expire', 'keep_locked'}:
        raise ValueError('数据资产释放策略不支持')

    filters = raw.get('filters', data.get('filters', {}))
    if not isinstance(filters, dict):
        raise ValueError('数据资产过滤条件必须是对象')

    output_format = str(data.get('output_format') or 'csv').lower()
    if save_to_asset and output_format == 'sql':
        raise ValueError('SQL 仅用于数据库导入，保存为可运行数据资产时请选择 CSV 或 JSONL')

    return {
        'save_to_asset': True,
        'asset_name': str(raw.get('asset_name', data.get('asset_name')) or job_name).strip()[:200],
        'tags': list(dict.fromkeys(str(item).strip() for item in tags if str(item).strip()))[:50],
        'auto_create_requirement': auto_create_requirement,
        'target_type': target_type or 'api_automation',
        'target_case_id': target_case_id,
        'target_case_name': str(raw.get('target_case_name', data.get('target_case_name')) or '').strip()[:500],
        'alias': alias[:100],
        'release_policy': release_policy,
        'filters': filters,
    }


def normalize_field_definitions(field_definitions: Any) -> List[Dict[str, Any]]:
    if not isinstance(field_definitions, list) or not field_definitions:
        raise ValueError('至少需要定义一个数据字段')
    if len(field_definitions) > MAX_FIELDS:
        raise ValueError(f'字段最多支持 {MAX_FIELDS} 个')

    normalized = []
    names = set()
    for index, definition in enumerate(field_definitions, start=1):
        if not isinstance(definition, dict):
            raise ValueError(f'第 {index} 个字段定义必须是对象')
        name = str(definition.get('name') or '').strip()
        field_type = str(definition.get('type') or '').strip().lower()
        config = definition.get('config') if isinstance(definition.get('config'), dict) else {}
        if not name:
            raise ValueError(f'第 {index} 个字段缺少 name')
        if name in names:
            raise ValueError(f'字段名重复：{name}')
        if field_type not in SUPPORTED_FIELD_TYPES:
            raise ValueError(f'字段 {name} 的类型不支持：{field_type}')
        names.add(name)
        normalized.append({'name': name, 'type': field_type, 'config': config})
    return normalized


def validate_generation_request(data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get('name') or '').strip()
    if not name:
        raise ValueError('任务名称不能为空')

    total_count = int(data.get('total_count') or data.get('count') or 0)
    if total_count < 1 or total_count > MAX_COUNT:
        raise ValueError(f'生成条数必须在 1 至 {MAX_COUNT:,} 之间')

    batch_size = int(data.get('batch_size') or 5000)
    if batch_size < MIN_BATCH_SIZE or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f'批次大小必须在 {MIN_BATCH_SIZE} 至 {MAX_BATCH_SIZE} 之间')

    output_format = str(data.get('output_format') or 'csv').lower()
    if output_format not in {'csv', 'jsonl', 'sql'}:
        raise ValueError('输出格式只支持 csv、jsonl 或 sql')

    table_name = str(data.get('table_name') or 'test_data').strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', table_name):
        raise ValueError('SQL表名只能包含字母、数字、下划线，且不能以数字开头')
    sql_dialect = str(data.get('sql_dialect') or 'mysql').lower()
    if sql_dialect not in SUPPORTED_SQL_DIALECTS:
        raise ValueError('SQL方言只支持 mysql、postgresql 或 sqlite')

    seed = data.get('seed')
    if seed in (None, ''):
        seed = random.SystemRandom().randint(0, 2**63 - 1)
    seed = int(seed)
    if seed < 0 or seed > 2**63 - 1:
        raise ValueError('随机种子必须是 0 至 2^63-1 的整数')

    return {
        'name': name,
        'asset_type': str(data.get('asset_type') or 'CUSTOM').strip()[:50] or 'CUSTOM',
        'field_definitions': normalize_field_definitions(data.get('field_definitions') or data.get('fields')),
        'total_count': total_count,
        'batch_size': batch_size,
        'output_format': output_format,
        'table_name': table_name,
        'sql_dialect': sql_dialect,
        'include_create_table': bool(data.get('include_create_table', True)),
        'seed': seed,
        'asset_config': _normalize_asset_config(data, name),
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _random_date(rng: random.Random, config: Dict[str, Any]) -> str:
    start = dt.date.fromisoformat(str(config.get('start') or '2020-01-01'))
    end = dt.date.fromisoformat(str(config.get('end') or dt.date.today().isoformat()))
    if end < start:
        start, end = end, start
    return (start + dt.timedelta(days=rng.randint(0, (end - start).days))).isoformat()


def _build_value_generator(field: Dict[str, Any], rng: random.Random, fake: FakerX):
    field_type = field['type']
    config = field['config']
    if field_type == 'sequence':
        prefix = str(config.get('prefix') or '')
        start = _as_int(config.get('start'), 1)
        width = max(0, _as_int(config.get('width'), 0))
        return lambda index: f'{prefix}{str(start + index).zfill(width)}'
    if field_type == 'fixed':
        value = config.get('value', '')
        return lambda index: value
    if field_type == 'integer':
        minimum = _as_int(config.get('min'), 0)
        maximum = _as_int(config.get('max'), 1_000_000)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        return lambda index: rng.randint(minimum, maximum)
    if field_type == 'decimal':
        minimum = _as_float(config.get('min'), 0)
        maximum = _as_float(config.get('max'), 1_000_000)
        decimals = max(0, min(8, _as_int(config.get('decimals'), 2)))
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        return lambda index: round(rng.uniform(minimum, maximum), decimals)
    if field_type == 'string':
        length = max(1, min(1024, _as_int(config.get('length'), 12)))
        charset = str(config.get('charset') or (string.ascii_letters + string.digits))
        charset = charset[:256] or string.ascii_letters
        return lambda index: ''.join(rng.choice(charset) for _ in range(length))
    if field_type == 'boolean':
        return lambda index: bool(rng.getrandbits(1))
    if field_type == 'uuid':
        return lambda index: str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if field_type == 'name':
        return lambda index: fake.name()
    if field_type == 'phone':
        return lambda index: fake.phone_number().replace(' ', '').replace('-', '')
    if field_type == 'verification_code':
        length = max(4, min(12, _as_int(config.get('length'), 6)))
        return lambda index: ''.join(rng.choice(string.digits) for _ in range(length))
    if field_type == 'email':
        domain = str(config.get('domain') or '').strip()
        def email(index):
            value = fake.email()
            if domain:
                value = f'{value.split("@")[0]}@{domain}'
            return value
        return email
    if field_type == 'address':
        return lambda index: fake.address()
    if field_type == 'company':
        return lambda index: fake.company()
    if field_type == 'id_card':
        return lambda index: fake.ssn()
    if field_type == 'date':
        return lambda index: _random_date(rng, config)
    raise ValueError(f'不支持的字段类型：{field_type}')


def _iter_rows(field_definitions: List[Dict[str, Any]], total_count: int, seed: int) -> Iterable[Dict[str, Any]]:
    rng = random.Random(seed)
    fake = FakerX(locale='zh_CN')
    generators = [(field['name'], _build_value_generator(field, rng, fake)) for field in field_definitions]
    for index in range(total_count):
        yield {name: generator(index) for name, generator in generators}


def read_generated_row(job: BulkTestDataJob, row_index: int) -> Dict[str, Any]:
    """按行读取生成文件，供接口/UI/APP 运行时按需注入一条数据。"""
    if job.status != BulkTestDataJob.STATUS_COMPLETED or not job.output_file:
        raise ValueError('大批量数据尚未生成完成')
    index = int(row_index)
    if index < 0 or index >= int(job.total_count):
        raise IndexError('大批量数据行号超出范围')

    if job.output_format == BulkTestDataJob.FORMAT_SQL:
        for current, row in enumerate(_iter_rows(job.field_definitions, job.total_count, job.seed or 0)):
            if current == index:
                return row

    with job.output_file.open('r') as stream:
        if job.output_format == BulkTestDataJob.FORMAT_CSV:
            reader = csv.DictReader(stream)
            for current, row in enumerate(reader):
                if current == index:
                    return dict(row)
        else:
            for current, line in enumerate(stream):
                if current == index and line.strip():
                    return json.loads(line)
    raise IndexError('生成文件中不存在指定行')


def read_generated_rows(job: BulkTestDataJob, limit: int = 100) -> List[Dict[str, Any]]:
    """顺序读取生成文件前 limit 行，供 Agent 只读上下文接口使用。

    与 read_generated_row 保持一致的可用性约束：仅 COMPLETED 且输出格式为
    CSV/JSONL 的任务可读；SQL 格式不落盘文件，不在此接口中物化。
    """
    if job.status != BulkTestDataJob.STATUS_COMPLETED or not job.output_file:
        return []
    if job.output_format not in (BulkTestDataJob.FORMAT_CSV, BulkTestDataJob.FORMAT_JSONL):
        return []
    limit = max(0, int(limit))
    if limit == 0:
        return []

    rows: List[Dict[str, Any]] = []
    try:
        with job.output_file.open('r') as stream:
            if job.output_format == BulkTestDataJob.FORMAT_CSV:
                for row in csv.DictReader(stream):
                    rows.append(dict(row))
                    if len(rows) >= limit:
                        break
            else:
                for line in stream:
                    if line.strip():
                        rows.append(json.loads(line))
                    if len(rows) >= limit:
                        break
    except (OSError, ValueError):
        # 输出文件可能已被清理；与执行侧的文件回退一致，静默降级为空数据。
        return []
    return rows


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return value


def _checksum(path: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _output_path(job: BulkTestDataJob) -> Tuple[Path, str]:
    relative = Path('test-data-bulk') / timezone.now().strftime('%Y/%m/%d') / f'{uuid.uuid4().hex}.{job.output_format}'
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError('生成文件路径非法')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, relative.as_posix()


def _quote_identifier(value: str, dialect: str) -> str:
    return f'`{value}`' if dialect == 'mysql' else f'"{value}"'


def _sql_literal(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _sql_column_type(field: Dict[str, Any], dialect: str) -> str:
    field_type = field.get('type')
    if field_type == 'integer':
        return 'BIGINT'
    if field_type == 'decimal':
        return 'DECIMAL(18, 4)'
    if field_type == 'boolean':
        return 'BOOLEAN' if dialect != 'sqlite' else 'INTEGER'
    if field_type == 'date':
        return 'DATE'
    return 'VARCHAR(255)' if dialect != 'sqlite' else 'TEXT'


def _write_sql_header(stream, job: BulkTestDataJob, columns: List[str]) -> None:
    quoted_table = _quote_identifier(job.table_name, job.sql_dialect)
    quoted_columns = ', '.join(_quote_identifier(column, job.sql_dialect) for column in columns)
    if job.include_create_table:
        definitions = ', '.join(
            f'{_quote_identifier(field["name"], job.sql_dialect)} {_sql_column_type(field, job.sql_dialect)}'
            for field in job.field_definitions
        )
        stream.write(f'CREATE TABLE IF NOT EXISTS {quoted_table} ({definitions});\n')
    stream.write(f'-- RunnerGo generated data: {job.name}\n')
    stream.write(f'-- columns: {quoted_columns}\n')


def _write_sql_row(stream, job: BulkTestDataJob, columns: List[str], row: Dict[str, Any]) -> None:
    quoted_table = _quote_identifier(job.table_name, job.sql_dialect)
    quoted_columns = ', '.join(_quote_identifier(column, job.sql_dialect) for column in columns)
    values = ', '.join(_sql_literal(row.get(column)) for column in columns)
    stream.write(f'INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({values});\n')


@transaction.atomic
def _publish_data_asset(job: BulkTestDataJob) -> None:
    """把已完成的生成任务登记为数据资产元数据，真实行保存在 BulkTestDataRow。"""
    config = job.asset_config if isinstance(job.asset_config, dict) else {}
    if not config.get('save_to_asset') or job.data_asset_id:
        return

    from .models import TestDataAsset, TestDataAssetRequirement

    columns = [item['name'] for item in job.field_definitions]
    batch_tag = f'bulk-job-{job.id}'
    tags = list(dict.fromkeys(['bulk-generated', batch_tag, *config.get('tags', [])]))
    asset = TestDataAsset.objects.create(
        asset_type=job.asset_type,
        name=config.get('asset_name') or job.name,
        status=TestDataAsset.STATUS_AVAILABLE,
        payload={
            '_runnergo_source': 'bulk_test_data',
            'bulk_job_id': job.id,
            'columns': columns,
            'total_count': job.total_count,
            'output_format': job.output_format,
            'data_source': 'database_rows' if job.output_format != BulkTestDataJob.FORMAT_SQL else 'generated_output_file',
        },
        tags=tags,
        bulk_job=job,
        created_by=job.created_by,
    )
    job.data_asset = asset
    job.save(update_fields=['data_asset', 'updated_at'])

    if config.get('auto_create_requirement'):
        TestDataAssetRequirement.objects.update_or_create(
            target_type=config['target_type'],
            target_case_id=int(config['target_case_id']),
            alias=config['alias'],
            defaults={
                'target_case_name': config.get('target_case_name') or job.name,
                'asset_type': job.asset_type,
                'source_asset': asset,
                'quantity': 1,
                'filters': config.get('filters') or {},
                'tags': [batch_tag],
                # 一个资产代表整份大批量文件，必须保持可用，才能连续引用全部行。
                'release_policy': 'release',
                'is_active': True,
                'created_by': job.created_by,
            },
        )


def run_bulk_test_data_generation(job_id: int, task_id: str = '') -> Dict[str, Any]:
    job = BulkTestDataJob.objects.get(pk=job_id)
    if job.status == BulkTestDataJob.STATUS_CANCELED:
        return {'job_id': job.id, 'status': job.status}

    job.status = BulkTestDataJob.STATUS_RUNNING
    job.task_id = task_id or job.task_id
    job.started_at = timezone.now()
    job.error_message = ''
    job.save(update_fields=['status', 'task_id', 'started_at', 'error_message', 'updated_at'])

    path, relative_path = _output_path(job)
    store_runtime_rows = bool(
        isinstance(job.asset_config, dict)
        and job.asset_config.get('save_to_asset')
        and job.output_format != BulkTestDataJob.FORMAT_SQL
    )
    if store_runtime_rows:
        BulkTestDataRow.objects.filter(job_id=job.id).delete()

    row_buffer = []

    def flush_runtime_rows():
        if not row_buffer:
            return
        BulkTestDataRow.objects.bulk_create(row_buffer, batch_size=job.batch_size)
        row_buffer.clear()

    def store_runtime_row(index, row):
        if store_runtime_rows:
            row_buffer.append(BulkTestDataRow(job_id=job.id, row_index=index - 1, payload=row))
            if len(row_buffer) >= job.batch_size:
                flush_runtime_rows()

    try:
        columns = [item['name'] for item in job.field_definitions]
        rows = _iter_rows(job.field_definitions, job.total_count, job.seed or 0)
        with path.open('w', encoding='utf-8', newline='') as stream:
            if job.output_format == BulkTestDataJob.FORMAT_CSV:
                writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                for index, row in enumerate(rows, start=1):
                    writer.writerow({key: _json_value(row.get(key)) for key in columns})
                    store_runtime_row(index, row)
                    if index % job.batch_size == 0 or index == job.total_count:
                        job.refresh_from_db(fields=['cancel_requested'])
                        if job.cancel_requested:
                            raise _GenerationCanceled()
                        job.generated_count = index
                        job.progress = min(99, int(index * 100 / job.total_count))
                        job.save(update_fields=['generated_count', 'progress', 'updated_at'])
            elif job.output_format == BulkTestDataJob.FORMAT_JSONL:
                for index, row in enumerate(rows, start=1):
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                    store_runtime_row(index, row)
                    if index % job.batch_size == 0 or index == job.total_count:
                        job.refresh_from_db(fields=['cancel_requested'])
                        if job.cancel_requested:
                            raise _GenerationCanceled()
                        job.generated_count = index
                        job.progress = min(99, int(index * 100 / job.total_count))
                        job.save(update_fields=['generated_count', 'progress', 'updated_at'])
            else:
                _write_sql_header(stream, job, columns)
                for index, row in enumerate(rows, start=1):
                    _write_sql_row(stream, job, columns, row)
                    if index % job.batch_size == 0 or index == job.total_count:
                        job.refresh_from_db(fields=['cancel_requested'])
                        if job.cancel_requested:
                            raise _GenerationCanceled()
                        job.generated_count = index
                        job.progress = min(99, int(index * 100 / job.total_count))
                        job.save(update_fields=['generated_count', 'progress', 'updated_at'])

        flush_runtime_rows()

        output_size, checksum = _checksum(path)
        job.output_file.name = relative_path
        job.output_size = output_size
        job.checksum_sha256 = checksum
        job.generated_count = job.total_count
        job.progress = 100
        _publish_data_asset(job)
        job.status = BulkTestDataJob.STATUS_COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=[
            'output_file', 'output_size', 'checksum_sha256', 'generated_count',
            'progress', 'status', 'completed_at', 'updated_at',
        ])
        return {'job_id': job.id, 'status': job.status, 'generated_count': job.generated_count}
    except _GenerationCanceled:
        if store_runtime_rows:
            BulkTestDataRow.objects.filter(job_id=job.id).delete()
        if path.exists():
            path.unlink()
        job.status = BulkTestDataJob.STATUS_CANCELED
        job.progress = 100
        job.completed_at = timezone.now()
        job.error_message = '用户取消生成'
        job.save(update_fields=['status', 'progress', 'completed_at', 'error_message', 'updated_at'])
        return {'job_id': job.id, 'status': job.status}
    except Exception as exc:
        if store_runtime_rows:
            BulkTestDataRow.objects.filter(job_id=job.id).delete()
        if path.exists():
            path.unlink()
        job.status = BulkTestDataJob.STATUS_FAILED
        job.progress = 100
        job.completed_at = timezone.now()
        job.error_message = str(exc)
        job.save(update_fields=['status', 'progress', 'completed_at', 'error_message', 'updated_at'])
        raise


class _GenerationCanceled(Exception):
    pass
