# -*- coding: utf-8 -*-
"""APP 测试用例中的元素引用完整性检查。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Set


def collect_element_reference_ids(payload: Any) -> Set[int]:
    """递归收集 element_id、*_element_id 形式的有效正整数引用。"""
    reference_ids: Set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'element_id' or key.endswith('_element_id'):
                    try:
                        element_id = int(item)
                    except (TypeError, ValueError):
                        element_id = 0
                    if element_id > 0:
                        reference_ids.add(element_id)
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    return reference_ids


def missing_active_element_ids(payload: Any) -> List[int]:
    """返回流程中不存在或已停用的元素 ID。"""
    from ..models import AppElement

    reference_ids = collect_element_reference_ids(payload)
    if not reference_ids:
        return []
    active_ids = set(
        AppElement.objects.filter(id__in=reference_ids, is_active=True)
        .values_list('id', flat=True)
    )
    return sorted(reference_ids - active_ids)


def find_test_case_references(element_ids: Iterable[int]) -> List[Dict[str, Any]]:
    """查找引用指定元素的测试用例，兼容 list/dict 两种 ui_flow 结构。"""
    from ..models import AppTestCase

    normalized_ids = {
        int(element_id)
        for element_id in element_ids
        if str(element_id).strip().isdigit() and int(element_id) > 0
    }
    if not normalized_ids:
        return []

    references = []
    for test_case in AppTestCase.objects.only('id', 'name', 'ui_flow').iterator():
        matched_ids = sorted(
            collect_element_reference_ids(test_case.ui_flow) & normalized_ids
        )
        if matched_ids:
            references.append({
                'id': test_case.id,
                'name': test_case.name,
                'element_ids': matched_ids,
            })
    return references


def group_references_by_element(
    references: Iterable[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """把用例引用结果按元素 ID 分组，便于 API 返回明确冲突。"""
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for reference in references:
        for element_id in reference.get('element_ids') or []:
            grouped[int(element_id)].append({
                'id': reference.get('id'),
                'name': reference.get('name'),
            })
    return dict(grouped)
