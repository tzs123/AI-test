from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import BulkTestDataJob, BulkTestDataRow, TestDataAsset, TestDataAssetLease, TestDataAssetRequirement, TestDataDecisionLog
from .runtime_orchestration import normalize_target_type


APP_AGENT_DATA_ALIAS_PREFIXES = ('agent_app_', 'agent_data_app_')


_ASSET_MUTABLE_FIELDS = {
    'asset_type', 'name', 'status', 'payload', 'tags', 'expires_at',
    'lock_ttl_seconds', 'bulk_job', 'bulk_row_cursor',
}

_BINDING_MUTABLE_FIELDS = {
    'target_case_name', 'asset_type', 'quantity', 'filters', 'tags',
    'release_policy', 'inject_path', 'lock_ttl_seconds', 'is_active',
}


def _normalize_binding(binding: Dict[str, Any], asset: TestDataAsset) -> Dict[str, Any]:
    target_type = normalize_target_type(binding.get('target_type'))
    try:
        target_case_id = int(binding.get('target_case_id'))
    except (TypeError, ValueError) as exc:
        raise ValueError('target_case_id 必须是正整数') from exc
    if target_case_id <= 0:
        raise ValueError('target_case_id 必须是正整数')

    alias = str(binding.get('alias') or '').strip()
    if not alias:
        raise ValueError('数据别名不能为空')
    if len(alias) > 100:
        raise ValueError('数据别名不能超过 100 个字符')

    release_policy = str(binding.get('release_policy') or 'release')
    valid_release_policies = {
        choice[0] for choice in TestDataAssetRequirement.RELEASE_POLICY_CHOICES
    }
    if release_policy not in valid_release_policies:
        raise ValueError('释放策略无效')
    if asset.bulk_job_id and release_policy != 'release':
        raise ValueError('大批量数据资产必须保持可用，才能按顺序引用全部数据')

    normalized = {
        key: value for key, value in binding.items()
        if key in _BINDING_MUTABLE_FIELDS
    }
    normalized.update({
        'target_type': target_type,
        'target_case_id': target_case_id,
        'target_case_name': str(binding.get('target_case_name') or ''),
        'alias': alias,
        'asset_type': asset.asset_type,
        'quantity': 1,
        'filters': binding.get('filters') or {},
        'tags': binding.get('tags') or [],
        'release_policy': release_policy,
        'source_asset': asset,
        'is_active': binding.get('is_active', True),
    })
    return normalized


@transaction.atomic
def create_or_update_asset_with_bindings(
    *,
    asset_values: Dict[str, Any],
    bindings: Iterable[Dict[str, Any]],
    user=None,
    asset: Optional[TestDataAsset] = None,
    replace_bindings: bool = False,
) -> Tuple[TestDataAsset, List[TestDataAssetRequirement]]:
    """原子保存数据资产及其用例关联，供页面和 Agent 共用。"""
    values = {
        key: value for key, value in (asset_values or {}).items()
        if key in _ASSET_MUTABLE_FIELDS
    }
    if asset is None:
        asset = TestDataAsset.objects.create(created_by=user, **values)
    else:
        for key, value in values.items():
            setattr(asset, key, value)
        if values:
            asset.save(update_fields=[*values.keys(), 'updated_at'])

    saved_requirements: List[TestDataAssetRequirement] = []
    for raw_binding in bindings or []:
        binding = dict(raw_binding or {})
        binding_id = binding.pop('id', None) or binding.pop('binding_id', None)
        normalized = _normalize_binding(binding, asset)

        requirement = None
        if binding_id:
            requirement = TestDataAssetRequirement.objects.select_for_update().filter(
                id=binding_id,
                source_asset=asset,
            ).first()
            if requirement is None:
                raise ValueError('关联记录不存在或不属于当前数据资产')
        if requirement is None:
            requirement = TestDataAssetRequirement.objects.select_for_update().filter(
                target_type=normalized['target_type'],
                target_case_id=normalized['target_case_id'],
                alias=normalized['alias'],
            ).order_by('id').first()

        if requirement is None:
            requirement = TestDataAssetRequirement.objects.create(
                created_by=user,
                **normalized,
            )
        else:
            for key, value in normalized.items():
                setattr(requirement, key, value)
            requirement.save(update_fields=[*normalized.keys(), 'updated_at'])
        saved_requirements.append(requirement)

    if replace_bindings:
        retained_ids = [item.id for item in saved_requirements]
        stale = TestDataAssetRequirement.objects.filter(source_asset=asset)
        if retained_ids:
            stale = stale.exclude(id__in=retained_ids)
        stale.delete()

    return asset, saved_requirements


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _get_path(source: Any, path: str, default: Any = None) -> Any:
    current = source
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


def _has_key_case_insensitive(source: Dict[str, Any], key: str) -> bool:
    return any(str(item_key).lower() == str(key).lower() for item_key in source)


def _merge_direct_data_fields(target: Dict[str, Any], value: Any) -> None:
    if not isinstance(target, dict) or not isinstance(value, dict):
        return
    for key, item in value.items():
        if _has_key_case_insensitive(target, str(key)):
            continue
        target[key] = deepcopy(item)


def materialize_asset_payload(asset: TestDataAsset) -> Dict[str, Any]:
    """将大批量数据资产引用按游标解析为当前执行实际使用的一行。"""
    payload = asset.payload if isinstance(asset.payload, dict) else {}
    if isinstance(asset.payload, list):
        rows = [item for item in asset.payload if isinstance(item, dict)]
        if not rows:
            return {}
        row_index = int(asset.bulk_row_cursor or 0) % len(rows)
        asset.bulk_row_cursor = (row_index + 1) % len(rows)
        asset.save(update_fields=['bulk_row_cursor', 'updated_at'])
        return deepcopy(rows[row_index])
    source = payload.get('_runnergo_source')
    if source != 'bulk_test_data' and not asset.bulk_job_id:
        return deepcopy(payload)

    job = asset.bulk_job
    if job is None:
        raise ValueError('数据资产关联的大批量生成任务不存在')
    row_index = int(asset.bulk_row_cursor or 0)
    if job.total_count:
        row_index %= int(job.total_count)

    row = BulkTestDataRow.objects.filter(job=job, row_index=row_index).values_list('payload', flat=True).first()
    if row is None:
        # 兼容迁移前已经生成、只有输出文件没有行表的旧资产；新资产不会走这里。
        from .bulk_data_generation import read_generated_row
        row = read_generated_row(job, row_index)

    asset.bulk_row_cursor = row_index + 1
    if job.total_count and asset.bulk_row_cursor >= int(job.total_count):
        asset.bulk_row_cursor = 0
    asset.save(update_fields=['bulk_row_cursor', 'updated_at'])
    return row


def _legacy_bulk_asset_for_requirement(requirement: TestDataAssetRequirement) -> Optional[TestDataAsset]:
    """兼容旧版本没有 source_asset 外键时，通过唯一批次标签找到批量资产。"""
    required_tags = set(str(item) for item in _as_list(requirement.tags))
    if not required_tags:
        return None
    candidates = TestDataAsset.objects.filter(
        asset_type=requirement.asset_type,
        bulk_job__isnull=False,
    )
    matches = [
        asset for asset in candidates
        if required_tags.issubset(set(str(item) for item in _as_list(asset.tags)))
    ]
    return matches[0] if len(matches) == 1 else None


@transaction.atomic
def delete_generated_data_for_job(job: BulkTestDataJob) -> None:
    """删除批量任务及其所有真实行、资产和绑定需求。"""
    asset_ids = set(TestDataAsset.objects.filter(bulk_job_id=job.id).values_list('id', flat=True))
    if job.data_asset_id:
        asset_ids.add(job.data_asset_id)
    if asset_ids:
        TestDataAssetRequirement.objects.filter(source_asset_id__in=asset_ids).delete()
        TestDataAsset.objects.filter(id__in=asset_ids).delete()
    if job.output_file:
        job.output_file.delete(save=False)
    job.delete()


@transaction.atomic
def delete_asset_and_generated_data(asset: TestDataAsset) -> None:
    """删除资产时同步删除其绑定需求；批量资产还会删除整份生成任务。"""
    if asset.bulk_job_id:
        delete_generated_data_for_job(asset.bulk_job)
        return
    TestDataAssetRequirement.objects.filter(source_asset_id=asset.id).delete()
    asset.delete()


@transaction.atomic
def delete_requirement_and_generated_data(requirement: TestDataAssetRequirement) -> None:
    """删除引用时同步删除对应数据，避免行表和生成文件长期残留。"""
    asset = requirement.source_asset or _legacy_bulk_asset_for_requirement(requirement)
    if asset:
        delete_asset_and_generated_data(asset)
        return
    requirement.delete()


def _asset_matches(asset: TestDataAsset, requirement: TestDataAssetRequirement) -> bool:
    if requirement.source_asset_id and asset.id != requirement.source_asset_id:
        return False
    required_tags = set(str(item) for item in _as_list(requirement.tags))
    asset_tags = set(str(item) for item in _as_list(asset.tags))
    if required_tags and not required_tags.issubset(asset_tags):
        return False

    filters = requirement.filters or {}
    if isinstance(filters, dict):
        for key, expected in filters.items():
            actual = _get_path(asset.payload or {}, key)
            if str(actual) != str(expected):
                return False
    return True


def _expire_unavailable_assets(now=None) -> None:
    now = now or timezone.now()
    TestDataAsset.objects.filter(
        expires_at__isnull=False,
        expires_at__lte=now,
    ).exclude(status=TestDataAsset.STATUS_EXPIRED).update(
        status=TestDataAsset.STATUS_EXPIRED,
        updated_at=now,
    )
    locked_assets = TestDataAsset.objects.filter(
        status=TestDataAsset.STATUS_LOCKED,
        locked_at__isnull=False,
    )
    for asset in locked_assets:
        locked_at = asset.locked_at
        ttl = asset.lock_ttl_seconds or 3600
        if locked_at and locked_at + timedelta(seconds=ttl) <= now:
            asset.status = TestDataAsset.STATUS_AVAILABLE
            asset.locked_by = None
            asset.locked_at = None
            asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])
            TestDataAssetLease.objects.filter(
                asset=asset,
                status=TestDataAssetLease.STATUS_LOCKED,
            ).update(
                status=TestDataAssetLease.STATUS_EXPIRED,
                released_at=now,
                cleanup_result={'reason': 'lock_ttl_expired'},
                updated_at=now,
            )


def _inject_asset(
    runtime_context: Dict[str, Any],
    requirement: TestDataAssetRequirement,
    payloads: List[Dict[str, Any]],
    leases: List[TestDataAssetLease],
) -> None:
    alias = requirement.alias
    value: Any = payloads[0] if requirement.quantity == 1 else payloads
    context_path = requirement.inject_path or f'dataAssets.{alias}'

    runtime_context.setdefault('dataAssets', {})
    runtime_context.setdefault('data', {})
    runtime_context['dataAssets'][alias] = value
    runtime_context['data'].setdefault(alias, value)
    _merge_direct_data_fields(runtime_context['data'], value)

    lease_ids = [lease.id for lease in leases]
    runtime_context.setdefault('dataAssetLeases', {})
    runtime_context['dataAssetLeases'][alias] = {
        'leaseIds': lease_ids,
        'assetIds': [lease.asset_id for lease in leases],
        'path': context_path,
        'releasePolicy': requirement.release_policy,
    }

    for lease in leases:
        lease.runtime_context_path = context_path
        lease.save(update_fields=['runtime_context_path', 'updated_at'])


@transaction.atomic
def request_assets_for_case(
    target_type: str,
    case_id: int,
    execution_type: str,
    execution_id: Any,
    user=None,
    runtime_context: Optional[Dict[str, Any]] = None,
    case_name: str = '',
    skip_alias_prefixes: Optional[Iterable[str]] = None,
    include_aliases: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, Any], List[TestDataAssetLease]]:
    """Apply active data requirements and inject locked assets into Runtime Context."""
    target_type = normalize_target_type(target_type)
    now = timezone.now()
    _expire_unavailable_assets(now)
    next_context = deepcopy(runtime_context or {})
    all_leases: List[TestDataAssetLease] = []

    requirements = TestDataAssetRequirement.objects.filter(
        target_type=target_type,
        target_case_id=int(case_id),
        is_active=True,
    ).order_by('id')
    include_alias_set = {
        str(alias) for alias in _as_list(include_aliases) if str(alias or '').strip()
    }
    skip_prefixes = tuple(
        str(prefix) for prefix in _as_list(skip_alias_prefixes) if str(prefix or '').strip()
    )
    if skip_prefixes:
        requirements = [
            requirement for requirement in requirements
            if requirement.alias in include_alias_set
            or not str(requirement.alias or '').startswith(skip_prefixes)
        ]
    elif include_aliases is not None:
        requirements = [
            requirement for requirement in requirements
            if requirement.alias in include_alias_set
        ]

    for requirement in requirements:
        if (
            requirement.source_asset_id
            and requirement.source_asset
            and requirement.source_asset.bulk_job_id
            and requirement.release_policy != 'release'
        ):
            raise ValueError(f'大批量数据资产 {requirement.source_asset_id} 必须使用释放为可用策略')
        quantity = max(1, int(requirement.quantity or 1))
        candidates = TestDataAsset.objects.select_for_update().filter(
            asset_type=requirement.asset_type,
            status=TestDataAsset.STATUS_AVAILABLE,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        selected = []
        for asset in candidates.order_by('id').distinct():
            if _asset_matches(asset, requirement):
                selected.append(asset)
            if len(selected) >= quantity:
                break
        if len(selected) < quantity:
            raise ValueError(f'数据资产不足: {requirement.alias} 需要 {quantity} 个 {requirement.asset_type}')

        leases = []
        payloads = []
        for asset in selected:
            resolved_payload = materialize_asset_payload(asset)
            asset.status = TestDataAsset.STATUS_LOCKED
            asset.locked_by = user
            asset.locked_at = now
            asset.lock_ttl_seconds = requirement.lock_ttl_seconds or asset.lock_ttl_seconds or 3600
            asset.save(update_fields=['status', 'locked_by', 'locked_at', 'lock_ttl_seconds', 'updated_at'])
            lease = TestDataAssetLease.objects.create(
                asset=asset,
                requirement=requirement,
                target_type=target_type,
                target_case_id=int(case_id),
                target_case_name=case_name or requirement.target_case_name,
                execution_type=execution_type or target_type,
                execution_id=str(execution_id or ''),
                alias=requirement.alias,
                release_policy=requirement.release_policy,
                payload_snapshot=deepcopy(resolved_payload),
                requested_by=user,
            )
            leases.append(lease)
            payloads.append(resolved_payload)

        _inject_asset(next_context, requirement, payloads, leases)
        all_leases.extend(leases)

    return next_context, all_leases


@transaction.atomic
def release_assets_for_execution(
    execution_type: str,
    execution_id: Any,
    cleanup_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Release or cleanup data assets leased by one execution. Idempotent."""
    now = timezone.now()
    result = {'released': [], 'used': [], 'expired': [], 'kept': [], 'skipped': []}
    leases = TestDataAssetLease.objects.select_for_update().filter(
        execution_type=execution_type,
        execution_id=str(execution_id or ''),
        status=TestDataAssetLease.STATUS_LOCKED,
    ).select_related('asset')

    for lease in leases:
        asset = lease.asset
        policy = lease.release_policy or 'release'
        lease.cleanup_result = cleanup_result or {}
        lease.released_at = now

        if policy == 'mark_used':
            asset.status = TestDataAsset.STATUS_USED
            asset.used_at = now
            asset.locked_by = None
            asset.locked_at = None
            asset.save(update_fields=['status', 'used_at', 'locked_by', 'locked_at', 'updated_at'])
            lease.status = TestDataAssetLease.STATUS_USED
            result['used'].append(asset.id)
        elif policy == 'expire':
            asset.status = TestDataAsset.STATUS_EXPIRED
            asset.locked_by = None
            asset.locked_at = None
            asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])
            lease.status = TestDataAssetLease.STATUS_EXPIRED
            result['expired'].append(asset.id)
        elif policy == 'keep_locked':
            lease.released_at = None
            result['kept'].append(asset.id)
        else:
            asset.status = TestDataAsset.STATUS_AVAILABLE
            asset.locked_by = None
            asset.locked_at = None
            asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])
            lease.status = TestDataAssetLease.STATUS_RELEASED
            result['released'].append(asset.id)

        lease.save(update_fields=['status', 'released_at', 'cleanup_result', 'updated_at'])
        status_map = {
            TestDataAssetLease.STATUS_RELEASED: TestDataDecisionLog.STATUS_RELEASED,
            TestDataAssetLease.STATUS_USED: TestDataDecisionLog.STATUS_USED,
            TestDataAssetLease.STATUS_EXPIRED: TestDataDecisionLog.STATUS_EXPIRED,
        }
        if lease.status in status_map:
            TestDataDecisionLog.objects.filter(lease=lease).update(
                status=status_map[lease.status],
                updated_at=now,
            )

    TestDataDecisionLog.objects.filter(
        execution_type=execution_type,
        execution_id=str(execution_id or ''),
        source=TestDataDecisionLog.SOURCE_RULE_GENERATION,
        status=TestDataDecisionLog.STATUS_PREPARED,
    ).update(status=TestDataDecisionLog.STATUS_RELEASED, updated_at=now)

    return result


@transaction.atomic
def release_lease(lease: TestDataAssetLease, policy: Optional[str] = None) -> Dict[str, Any]:
    if lease.status != TestDataAssetLease.STATUS_LOCKED:
        return {'skipped': [lease.asset_id], 'reason': 'lease_not_locked'}
    lease.release_policy = policy or lease.release_policy or 'release'
    now = timezone.now()
    asset = TestDataAsset.objects.select_for_update().get(id=lease.asset_id)
    result = {'released': [], 'used': [], 'expired': [], 'kept': [], 'skipped': []}

    if lease.release_policy == 'mark_used':
        asset.status = TestDataAsset.STATUS_USED
        asset.used_at = now
        asset.locked_by = None
        asset.locked_at = None
        lease.status = TestDataAssetLease.STATUS_USED
        result['used'].append(asset.id)
    elif lease.release_policy == 'expire':
        asset.status = TestDataAsset.STATUS_EXPIRED
        asset.locked_by = None
        asset.locked_at = None
        lease.status = TestDataAssetLease.STATUS_EXPIRED
        result['expired'].append(asset.id)
    elif lease.release_policy == 'keep_locked':
        result['kept'].append(asset.id)
    else:
        asset.status = TestDataAsset.STATUS_AVAILABLE
        asset.locked_by = None
        asset.locked_at = None
        lease.status = TestDataAssetLease.STATUS_RELEASED
        result['released'].append(asset.id)

    asset.save(update_fields=['status', 'used_at', 'locked_by', 'locked_at', 'updated_at'])
    lease.released_at = None if lease.release_policy == 'keep_locked' else now
    lease.save(update_fields=['release_policy', 'status', 'released_at', 'updated_at'])
    return result
