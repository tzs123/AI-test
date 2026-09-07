# -*- coding: utf-8 -*-
"""Helpers for making action-recorded APP steps replayable."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


INPUT_ACTION_TYPES = {
    'input',
    'input_text',
    'smart_input',
    'bank_card_input',
    'money_input',
}

TARGET_CONFIG_KEYS = (
    'element_id',
    'selector_type',
    'selector',
    'source_resolution',
    'normalized_position',
    'coordinate_mode',
    'timeout',
    'wait_for_stable',
    'fingerprint',
    'fingerprint_version',
    'locator_strategies',
    'semantic_min_score',
    'resource_id',
    'accessibility_id',
    'content_desc',
    'text',
    'placeholder',
    'class_name',
    'guard_security_challenge',
    'image_scope',
    'image_threshold',
)


@dataclass
class RecordedStepRepairResult:
    steps: List[Dict[str, Any]]
    repairs: List[Dict[str, Any]] = field(default_factory=list)
    problems: List[Dict[str, Any]] = field(default_factory=list)


def step_action(step: Dict[str, Any]) -> str:
    return str(step.get('type') or step.get('action') or '').strip().lower()


def is_input_action(step: Dict[str, Any]) -> bool:
    return step_action(step) in INPUT_ACTION_TYPES


def is_generated_recording_click_name(value: Any) -> bool:
    text = str(value or '').strip()
    return bool(re.fullmatch(r'(?:iOS\s*)?录制点击\s*\d+', text, flags=re.IGNORECASE))


def step_config(step: Dict[str, Any]) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    nested = step.get('config')
    if isinstance(nested, dict):
        config.update(nested)
    for key, value in step.items():
        if key != 'config' and value not in (None, '', {}):
            config[key] = value
    return config


def _set_step_config_values(step: Dict[str, Any], values: Dict[str, Any]) -> None:
    target = step.setdefault('config', {}) if isinstance(step.get('config'), dict) else step
    if target is step and 'config' in step and not isinstance(step.get('config'), dict):
        step['config'] = {}
        target = step['config']
    for key, value in values.items():
        if key.startswith('_'):
            continue
        if key in {'selector_type', 'selector'}:
            target[key] = copy.deepcopy(value)
            continue
        if target.get(key) in (None, '', {}):
            target[key] = copy.deepcopy(value)


def _compact_text(value: Any) -> str:
    return re.sub(r'\s+', '', str(value or '')).strip().casefold()


def _text_input_class_name(value: Any) -> bool:
    class_name = str(value or '')
    return any(token in class_name for token in (
        'EditText',
        'TextField',
        'SecureTextField',
        'SearchField',
        'XCUIElementTypeTextView',
    ))


def _looks_like_text_input_placeholder(value: Any) -> bool:
    text = _compact_text(value)
    if not text or '请选择' in text:
        return False
    return any(marker in text for marker in ('请填写', '请输入', '填写', '输入'))


def step_targets_text_input(step: Dict[str, Any]) -> bool:
    config = step_config(step)
    if _text_input_class_name(config.get('class_name')):
        return True
    for key in ('resource_id', 'accessibility_id', 'content_desc', 'text', 'placeholder'):
        if _looks_like_text_input_placeholder(config.get(key)):
            return True
    fingerprint = config.get('fingerprint') or {}
    if not isinstance(fingerprint, dict):
        return False
    if _text_input_class_name(fingerprint.get('class_name')):
        return True
    return any(
        _looks_like_text_input_placeholder(fingerprint.get(key))
        for key in ('resource_id', 'content_desc', 'text', 'placeholder')
    )


def _blank_selector_input(step: Dict[str, Any]) -> bool:
    config = step_config(step)
    if not is_input_action(step):
        return False
    if config.get('element_id'):
        return False
    selector = config.get('selector')
    selector_type = str(config.get('selector_type') or 'image').strip().lower()
    return selector in (None, '') and selector_type in {'', 'image'}


def _remembered_target_from_step(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not step_targets_text_input(step):
        return None
    config = step_config(step)
    target = {
        key: copy.deepcopy(config[key])
        for key in TARGET_CONFIG_KEYS
        if config.get(key) not in (None, '', {})
    }
    fingerprint = target.get('fingerprint') or {}
    if isinstance(fingerprint, dict):
        for source_key, target_key in (
            ('resource_id', 'resource_id'),
            ('content_desc', 'content_desc'),
            ('text', 'text'),
            ('placeholder', 'placeholder'),
            ('class_name', 'class_name'),
        ):
            if target.get(target_key) in (None, '') and fingerprint.get(source_key):
                target[target_key] = fingerprint.get(source_key)
    if not any(target.get(key) for key in (
        'element_id',
        'selector',
        'fingerprint',
        'locator_strategies',
        'resource_id',
        'accessibility_id',
        'content_desc',
        'text',
    )):
        return None
    return target


def repair_recorded_input_steps(
    steps: List[Dict[str, Any]],
    *,
    reject_unfixed: bool = False,
) -> RecordedStepRepairResult:
    repaired_steps = copy.deepcopy(steps)
    repairs: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    remembered_target: Optional[Dict[str, Any]] = None

    for index, step in enumerate(repaired_steps, 1):
        if not isinstance(step, dict):
            continue
        if _blank_selector_input(step):
            if remembered_target:
                _set_step_config_values(step, remembered_target)
                step.setdefault('config', {})['_recording_input_target_repaired'] = True
                repairs.append({
                    'index': index,
                    'name': step.get('name') or f'步骤 {index}',
                    'reason': 'blank-input-selector-inherited',
                })
            else:
                problems.append({
                    'index': index,
                    'name': step.get('name') or f'步骤 {index}',
                    'reason': 'blank-input-selector-without-target',
                    'message': (
                        '输入步骤缺少稳定定位，且前序步骤没有可继承的输入框目标；'
                        '请重新录制该输入框，或为该步骤配置 selector/fingerprint。'
                    ),
                })

        next_target = _remembered_target_from_step(step)
        if next_target:
            remembered_target = next_target
        elif not is_generated_recording_click_name(step.get('name')):
            remembered_target = None

    if reject_unfixed and problems:
        return RecordedStepRepairResult(repaired_steps, repairs, problems)
    return RecordedStepRepairResult(repaired_steps, repairs, problems)


def _flow_steps(value: Any) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    if isinstance(value, list):
        return value, 'root'
    if isinstance(value, dict) and isinstance(value.get('steps'), list):
        return value['steps'], 'steps'
    return None, ''


def repair_recorded_input_targets_in_flow(
    ui_flow: Any,
    *,
    reject_unfixed: bool = False,
) -> Tuple[Any, RecordedStepRepairResult]:
    flow = copy.deepcopy(ui_flow)
    steps, location = _flow_steps(flow)
    if steps is None:
        return flow, RecordedStepRepairResult([])
    result = repair_recorded_input_steps(steps, reject_unfixed=reject_unfixed)
    if location == 'root':
        flow = result.steps
    else:
        flow['steps'] = result.steps
    return flow, result
