from __future__ import annotations

import random
import re
import string
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .data_assets import _asset_matches, _expire_unavailable_assets, materialize_asset_payload
from .models import (
    TestDataAsset,
    TestDataAssetLease,
    TestDataAssetRequirement,
    TestDataDecisionLog,
)
from .runtime_case import normalize_overrides, public_input_fields
from .runtime_orchestration import get_case_payload, merge_runtime_overrides, normalize_target_type


EXPLICIT_VALUE_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_.-]+)\s*\}")
FIELD_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fff]+")


FIELD_RULES = (
    ('phone', 'PHONE', ('手机号', '手机', '电话', 'mobile', 'phone', 'tel')),
    ('verification_code', 'CUSTOM', ('验证码', '短信码', 'code', 'otp', 'captcha')),
    ('password', 'USER', ('密码', 'password', 'pwd')),
    ('email', 'USER', ('邮箱', '邮件', 'email', 'mail')),
    ('username', 'USER', ('账号', '用户名', '用户', 'user', 'username', 'account', 'login')),
    ('name', 'USER', ('姓名', '名称', '昵称', 'name', 'nick')),
    ('id_card', 'CUSTOM', ('身份证', '证件', 'idcard', 'id_card', 'identity')),
    ('license_plate', 'CUSTOM', ('车牌', 'plate', 'license')),
    ('product', 'PRODUCT', ('商品', '产品', 'sku', 'product')),
    ('order', 'ORDER', ('订单', 'order')),
    ('amount', 'CUSTOM', (
        '金额', '价格', '费用', '收入', '年薪', '薪资', '工资',
        'amount', 'price', 'money', 'income', 'salary',
    )),
    ('date', 'CUSTOM', ('日期', '时间', 'date', 'time')),
    ('url', 'CUSTOM', ('链接', '网址', 'url', 'uri', 'link', 'website')),
    ('address', 'CUSTOM', ('收货地址', '地址', 'address')),
    ('number', 'CUSTOM', ('数量', '次数', 'number', 'count', 'num')),
)


@dataclass(frozen=True)
class SmartDataRequirement:
    field: Dict[str, Any]
    field_key: str
    field_label: str
    field_type: str
    asset_type: str
    alias: str
    context_path: str
    dependencies: Tuple[str, ...] = ()


def _normalize_label(value: Any) -> str:
    return str(value or '').strip()


def _field_key(field: Dict[str, Any]) -> str:
    return str(field.get('stepPath') or field.get('stepId') or field.get('element') or '').strip()


def _slug(value: str, fallback: str) -> str:
    tokens = FIELD_TOKEN_RE.findall(str(value or '').lower())
    text = '_'.join(tokens).strip('_-')
    text = re.sub(r'[^A-Za-z0-9_\u4e00-\u9fff]+', '_', text).strip('_')
    return (text or fallback)[:80]


def _recognize_field(field: Dict[str, Any]) -> Tuple[str, str, str]:
    label_parts = [
        field.get('element'),
        field.get('step'),
        field.get('stepId'),
        field.get('stepPath'),
    ]
    label_text = ' '.join(_normalize_label(item).lower() for item in label_parts if _normalize_label(item))
    for field_type, asset_type, keywords in FIELD_RULES:
        if any(keyword.lower() in label_text for keyword in keywords):
            alias = field_type
            if field_type in {'username', 'password', 'email', 'name'}:
                alias = 'user'
            return field_type, asset_type, alias
    return 'text', 'CUSTOM', 'field'


def _dependencies_for(field_type: str, fields: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    available = {_recognize_field(field)[0] for field in fields or []}
    if field_type == 'verification_code' and 'phone' in available:
        return ('phone',)
    if field_type in {'password', 'email'} and 'username' in available:
        return ('username',)
    if field_type == 'order':
        deps = [item for item in ('user', 'product') if item in available]
        return tuple(deps)
    return ()


def analyze_data_requirements(case_payload: Any) -> List[Dict[str, Any]]:
    """Analyze UI/APP/API input fields and infer runtime data needs."""
    fields = public_input_fields(case_payload)
    requirements = []
    seen: Set[str] = set()
    for index, field in enumerate(fields, 1):
        key = _field_key(field)
        if not key or key in seen:
            continue
        seen.add(key)
        field_type, asset_type, alias_base = _recognize_field(field)
        label = _normalize_label(field.get('element') or field.get('step') or key)
        alias = _slug(alias_base if alias_base != 'field' else label, f'field_{index}')
        if alias in {item['alias'] for item in requirements}:
            alias = f'{alias}_{index}'
        context_path = f"smartData.fields.{alias}.value"
        requirements.append({
            'field': field,
            'fieldKey': key,
            'fieldLabel': label,
            'fieldType': field_type,
            'assetType': asset_type,
            'alias': alias,
            'contextPath': context_path,
            'dependencies': list(_dependencies_for(field_type, fields)),
        })
    return requirements


def _runtime_override_keys(overrides: Iterable[Dict[str, Any]]) -> Set[str]:
    keys = set()
    for item in normalize_overrides(overrides or []):
        if item.get('stepId'):
            keys.add(str(item['stepId']))
        if item.get('stepPath'):
            keys.add(str(item['stepPath']))
    return keys


def _context_get(source: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = source
    for part in str(path or '').split('.'):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part, default)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return default
        else:
            return default
    return current


def _context_set(source: Dict[str, Any], path: str, value: Any) -> None:
    current = source
    parts = [part for part in str(path or '').split('.') if part]
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    if parts:
        current[parts[-1]] = value


def _asset_value_for_type(payload: Dict[str, Any], field_type: str) -> Any:
    candidates = {
        'phone': ('phone', 'mobile', 'tel', 'value'),
        'verification_code': ('code', 'otp', 'captcha', 'value'),
        'password': ('password', 'pwd', 'value'),
        'email': ('email', 'mail', 'value'),
        'username': ('username', 'account', 'userName', 'phone', 'value'),
        'name': ('name', 'realName', 'nickname', 'value'),
        'id_card': ('id_card', 'idCard', 'identity', 'value'),
        'license_plate': ('license_plate', 'plate', 'carPlate', 'value'),
        'amount': ('amount', 'price', 'money', 'value'),
        'date': ('date', 'time', 'value'),
        'url': ('url', 'uri', 'link', 'value'),
        'address': ('address', 'shippingAddress', 'value'),
        'number': ('number', 'count', 'num', 'value'),
    }.get(field_type, ('value',))
    for key in candidates:
        value = _context_get(payload, key, None)
        if value not in (None, ''):
            return value
    return payload


def _random_digits(length: int) -> str:
    return ''.join(random.choice(string.digits) for _ in range(length))


def _generate_value(requirement: SmartDataRequirement, context: Dict[str, Any]) -> Any:
    field_type = requirement.field_type
    if field_type == 'phone':
        return '1' + random.choice('3456789') + _random_digits(9)
    if field_type == 'verification_code':
        return _random_digits(6)
    if field_type == 'password':
        return 'Rg@' + uuid.uuid4().hex[:10]
    if field_type == 'email':
        return f"test_{uuid.uuid4().hex[:10]}@runnergo.test"
    if field_type == 'username':
        phone = _context_get(context, 'smartData.fields.phone.value')
        return phone or f"user_{uuid.uuid4().hex[:8]}"
    if field_type == 'name':
        return '测试用户' + _random_digits(4)
    if field_type == 'id_card':
        return '1101011990' + _random_digits(8)
    if field_type == 'license_plate':
        return '京A' + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5))
    if field_type == 'amount':
        return str(random.randint(1, 999))
    if field_type == 'date':
        return (timezone.localtime(timezone.now()) + timedelta(days=1)).strftime('%Y-%m-%d')
    if field_type == 'url':
        return f"https://runnergo.test/{uuid.uuid4().hex[:8]}"
    if field_type == 'address':
        return f"北京市朝阳区测试路{random.randint(1, 999)}号"
    if field_type == 'number':
        return random.randint(1, 9)
    return 'test_' + uuid.uuid4().hex[:8]


def _as_requirement(item: Dict[str, Any]) -> SmartDataRequirement:
    return SmartDataRequirement(
        field=item['field'],
        field_key=item['fieldKey'],
        field_label=item['fieldLabel'],
        field_type=item['fieldType'],
        asset_type=item['assetType'],
        alias=item['alias'],
        context_path=item['contextPath'],
        dependencies=tuple(item.get('dependencies') or ()),
    )


def _recent_repaired_asset_ids(target_type: str, case_id: int, field_key: str) -> Set[int]:
    since = timezone.now() - timedelta(days=7)
    return set(TestDataDecisionLog.objects.filter(
        target_type=target_type,
        target_case_id=case_id,
        field_key=field_key,
        status=TestDataDecisionLog.STATUS_REPAIRED,
        asset_id__isnull=False,
        updated_at__gte=since,
    ).values_list('asset_id', flat=True))


def _find_asset(requirement: SmartDataRequirement, target_type: str, case_id: int, now) -> Optional[TestDataAsset]:
    repaired_asset_ids = _recent_repaired_asset_ids(target_type, case_id, requirement.field_key)
    candidates = TestDataAsset.objects.select_for_update().filter(
        asset_type=requirement.asset_type,
        status=TestDataAsset.STATUS_AVAILABLE,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if repaired_asset_ids:
        candidates = candidates.exclude(id__in=repaired_asset_ids)
    pseudo_requirement = TestDataAssetRequirement(
        alias=requirement.alias,
        asset_type=requirement.asset_type,
        quantity=1,
        filters={},
        tags=[],
    )
    for asset in candidates.order_by('id').distinct():
        if _asset_matches(asset, pseudo_requirement):
            return asset
    return None


def _create_lease(
    asset: TestDataAsset,
    requirement: SmartDataRequirement,
    target_type: str,
    case_id: int,
    execution_type: str,
    execution_id: Any,
    user,
    case_name: str,
    now,
    payload_snapshot: Optional[Dict[str, Any]] = None,
) -> TestDataAssetLease:
    asset.status = TestDataAsset.STATUS_LOCKED
    asset.locked_by = user
    asset.locked_at = now
    asset.lock_ttl_seconds = asset.lock_ttl_seconds or 3600
    asset.save(update_fields=['status', 'locked_by', 'locked_at', 'lock_ttl_seconds', 'updated_at'])
    lease = TestDataAssetLease.objects.create(
        asset=asset,
        requirement=None,
        target_type=target_type,
        target_case_id=int(case_id),
        target_case_name=case_name,
        execution_type=execution_type or target_type,
        execution_id=str(execution_id or ''),
        alias=requirement.alias,
        release_policy='release',
        runtime_context_path=requirement.context_path,
        payload_snapshot=deepcopy(payload_snapshot if payload_snapshot is not None else asset.payload or {}),
        requested_by=user,
    )
    return lease


def _decision_log_payload(
    requirement: SmartDataRequirement,
    source: str,
    strategy: str,
    value: Any,
    asset: Optional[TestDataAsset],
    lease: Optional[TestDataAssetLease],
) -> Dict[str, Any]:
    is_sensitive = bool(requirement.field.get('sensitive') or requirement.field_type == 'password')
    snapshot_value = '******' if is_sensitive else value
    asset_payload = _mask_sensitive_payload(deepcopy(asset.payload or {})) if asset else {}
    return {
        'field_key': requirement.field_key,
        'field_label': requirement.field_label,
        'field_type': requirement.field_type,
        'alias': requirement.alias,
        'source': source,
        'strategy': strategy,
        'runtime_context_path': requirement.context_path,
        'decision': {
            'field': requirement.field,
            'dependencies': list(requirement.dependencies),
            'assetType': requirement.asset_type,
        },
        'value_snapshot': {
            'value': snapshot_value,
            'assetPayload': asset_payload,
        },
        'asset': asset,
        'lease': lease,
    }


def _mask_sensitive_payload(value: Any) -> Any:
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if any(token in str(key).lower() for token in ('password', 'pwd', 'secret', 'token')):
                masked[key] = '******'
            else:
                masked[key] = _mask_sensitive_payload(item)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive_payload(item) for item in value]
    return value


@transaction.atomic
def prepare_smart_test_data(
    *,
    target_type: str,
    case_id: int,
    execution_type: str,
    execution_id: Any,
    user=None,
    runtime_context: Optional[Dict[str, Any]] = None,
    runtime_override: Optional[Iterable[Dict[str, Any]]] = None,
    case_payload: Any = None,
    case_name: str = '',
    enabled: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[TestDataDecisionLog], List[TestDataAssetLease]]:
    """Prepare V6 smart data via Runtime Context without mutating source cases."""
    target_type = normalize_target_type(target_type)
    if not enabled:
        return deepcopy(runtime_context or {}), list(runtime_override or []), [], []

    if case_payload is None:
        case_payload, loaded_name = get_case_payload(target_type, case_id)
        case_name = case_name or loaded_name

    now = timezone.now()
    _expire_unavailable_assets(now)
    next_context = deepcopy(runtime_context or {})
    next_context.setdefault('smartData', {})
    next_context['smartData'].setdefault('fields', {})
    next_context['smartData'].setdefault('decisions', [])
    next_context['smartData'].setdefault('dependencies', [])

    existing_keys = _runtime_override_keys(runtime_override or [])
    generated_overrides = []
    decision_logs: List[TestDataDecisionLog] = []
    leases: List[TestDataAssetLease] = []

    for raw_requirement in analyze_data_requirements(case_payload):
        requirement = _as_requirement(raw_requirement)
        if requirement.field.get('stepId') in existing_keys or requirement.field.get('stepPath') in existing_keys:
            next_context['smartData']['decisions'].append({
                'fieldKey': requirement.field_key,
                'fieldType': requirement.field_type,
                'source': TestDataDecisionLog.SOURCE_SKIPPED,
                'reason': 'runtime_override_exists',
            })
            continue

        asset = _find_asset(requirement, target_type, int(case_id), now)
        if asset:
            resolved_payload = materialize_asset_payload(asset)
            lease = _create_lease(
                asset,
                requirement,
                target_type,
                int(case_id),
                execution_type,
                execution_id,
                user,
                case_name,
                now,
                payload_snapshot=resolved_payload,
            )
            value = _asset_value_for_type(resolved_payload, requirement.field_type)
            source = TestDataDecisionLog.SOURCE_ASSET_POOL
            strategy = 'asset_pool_first'
            leases.append(lease)
        else:
            lease = None
            value = _generate_value(requirement, next_context)
            source = TestDataDecisionLog.SOURCE_RULE_GENERATION
            strategy = 'rule_generation_fallback'

        _context_set(next_context, requirement.context_path, value)
        _context_set(next_context, f'smartData.fields.{requirement.alias}.type', requirement.field_type)
        _context_set(next_context, f'smartData.fields.{requirement.alias}.source', source)
        _context_set(next_context, f'smartData.fields.{requirement.alias}.fieldKey', requirement.field_key)
        _context_set(next_context, f'smartData.fields.{requirement.alias}.dependencies', list(requirement.dependencies))
        if requirement.dependencies:
            next_context['smartData']['dependencies'].append({
                'field': requirement.alias,
                'dependsOn': list(requirement.dependencies),
            })

        decision = {
            'fieldKey': requirement.field_key,
            'fieldLabel': requirement.field_label,
            'fieldType': requirement.field_type,
            'alias': requirement.alias,
            'source': source,
            'strategy': strategy,
            'contextPath': requirement.context_path,
            'assetId': asset.id if asset else None,
            'leaseId': lease.id if lease else None,
            'dependencies': list(requirement.dependencies),
        }
        next_context['smartData']['decisions'].append(decision)
        log_data = _decision_log_payload(requirement, source, strategy, value, asset, lease)
        decision_logs.append(TestDataDecisionLog.objects.create(
            target_type=target_type,
            target_case_id=int(case_id),
            target_case_name=case_name,
            execution_type=execution_type or target_type,
            execution_id=str(execution_id or ''),
            created_by=user,
            **log_data,
        ))
        generated_overrides.append({
            'stepId': requirement.field.get('stepId'),
            'stepPath': requirement.field.get('stepPath'),
            'runtimeValue': '${' + requirement.context_path + '}',
        })

    next_context['smartData']['version'] = 'V6'
    next_context['smartData']['preparedAt'] = timezone.now().isoformat()
    merged_override = merge_runtime_overrides(generated_overrides, runtime_override or [])
    return next_context, merged_override, decision_logs, leases


@transaction.atomic
def repair_failed_execution_data(
    *,
    execution_type: str,
    execution_id: Any,
    failure_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record data repair decisions after a failed execution."""
    failure_info = failure_info or {}
    logs = list(TestDataDecisionLog.objects.select_for_update().filter(
        execution_type=execution_type,
        execution_id=str(execution_id or ''),
        status__in=[
            TestDataDecisionLog.STATUS_PREPARED,
            TestDataDecisionLog.STATUS_RELEASED,
            TestDataDecisionLog.STATUS_USED,
            TestDataDecisionLog.STATUS_EXPIRED,
        ],
    ).select_related('asset', 'lease'))

    result = {'repaired': [], 'skipped': []}
    for log in logs:
        if log.source == TestDataDecisionLog.SOURCE_ASSET_POOL and log.asset_id:
            repair_result = {
                'action': 'avoid_asset_on_next_prepare',
                'assetId': log.asset_id,
                'reason': 'execution_failed',
            }
        elif log.source == TestDataDecisionLog.SOURCE_RULE_GENERATION:
            repair_result = {
                'action': 'regenerate_on_next_prepare',
                'reason': 'execution_failed',
            }
        else:
            result['skipped'].append(log.id)
            continue
        log.status = TestDataDecisionLog.STATUS_REPAIRED
        log.failure_info = failure_info
        log.repair_result = repair_result
        log.save(update_fields=['status', 'failure_info', 'repair_result', 'updated_at'])
        result['repaired'].append({
            'logId': log.id,
            'fieldKey': log.field_key,
            'source': log.source,
            'repair': repair_result,
        })
    return result
