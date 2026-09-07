# -*- coding: utf-8 -*-
"""Helpers for normalizing iOS browser-based APP automation flows."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Sequence


IOS_SAFARI_BUNDLE_IDS = {'com.apple.mobilesafari'}
IOS_BROWSER_ADDRESS_KEYWORDS = (
    '搜索或输入网站名称',
    '地址',
    'address',
    'url',
    'website',
    'tabbaritemtitle',
)


def _merged_step_config(step: Dict[str, Any]) -> Dict[str, Any]:
    """Return locator config from both stored and runtime-expanded step shapes."""
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    merged = copy.deepcopy(config)
    for key, value in step.items():
        if key == 'config':
            continue
        if value not in (None, '', {}, []):
            merged[key] = value
    return merged


def ios_flow_should_start_from_home(ui_flow: Sequence[Dict[str, Any]]) -> bool:
    """Return whether the first step appears to launch Safari/browser from iOS home."""
    if not isinstance(ui_flow, list) or not ui_flow:
        return False

    first_step = ui_flow[0] if isinstance(ui_flow[0], dict) else {}
    config = _merged_step_config(first_step)
    fingerprint = config.get('fingerprint') if isinstance(config.get('fingerprint'), dict) else {}
    values = [
        first_step.get('name'),
        first_step.get('type'),
        config.get('selector'),
        config.get('resource_id'),
        config.get('accessibility_id'),
        config.get('text'),
        config.get('ocr_text'),
        fingerprint.get('resource_id'),
        fingerprint.get('text'),
        fingerprint.get('class_name'),
    ]
    haystack = ' '.join(str(value or '').lower() for value in values)
    return any(keyword in haystack for keyword in ('safari', '浏览器', 'browser'))


def is_ios_safari_package(package_name: str) -> bool:
    return str(package_name or '').strip().lower() in IOS_SAFARI_BUNDLE_IDS


def ios_should_open_browser_start_url(
    platform: str,
    package_name: str,
    ui_flow: Sequence[Dict[str, Any]],
    browser_start_url: str,
) -> bool:
    if str(platform or '').lower() != 'ios':
        return False
    if not str(browser_start_url or '').strip():
        return False
    if package_name and not is_ios_safari_package(package_name):
        return False
    if not package_name:
        return True
    return True


def ios_step_targets_browser_address_input(step: Dict[str, Any]) -> bool:
    if not isinstance(step, dict):
        return False
    step_type = str(step.get('type') or '').lower()
    if step_type not in ('input', 'self_heal_input'):
        return False

    config = _merged_step_config(step)
    strategies = config.get('locator_strategies') if isinstance(config.get('locator_strategies'), list) else []
    values = [
        step.get('name'),
        config.get('selector'),
        config.get('resource_id'),
        config.get('accessibility_id'),
        config.get('text'),
        config.get('ocr_text'),
        config.get('value'),
    ]
    for strategy in strategies:
        if isinstance(strategy, dict):
            values.append(strategy.get('value'))
    haystack = ' '.join(str(value or '').lower() for value in values)
    return any(keyword.lower() in haystack for keyword in IOS_BROWSER_ADDRESS_KEYWORDS)


def ios_step_targets_browser_address_bar(step: Dict[str, Any]) -> bool:
    if not isinstance(step, dict):
        return False
    step_type = str(step.get('type') or '').lower()
    if step_type not in ('click', 'touch', 'self_heal_click', 'smart_click'):
        return False

    config = _merged_step_config(step)
    strategies = config.get('locator_strategies') if isinstance(config.get('locator_strategies'), list) else []
    values = [
        step.get('name'),
        config.get('selector'),
        config.get('resource_id'),
        config.get('accessibility_id'),
        config.get('text'),
        config.get('ocr_text'),
    ]
    for strategy in strategies:
        if isinstance(strategy, dict):
            values.append(strategy.get('value'))
    haystack = ' '.join(str(value or '').lower() for value in values)
    return any(keyword.lower() in haystack for keyword in IOS_BROWSER_ADDRESS_KEYWORDS)


def strip_ios_browser_bootstrap_steps(
    ui_flow: Sequence[Dict[str, Any]],
    *,
    platform: str,
    package_name: str = '',
    browser_start_url: str = '',
) -> List[Dict[str, Any]]:
    """Remove generated Safari launch + address-bar input steps handled by test setup."""
    if not ios_should_open_browser_start_url(platform, package_name, ui_flow, browser_start_url):
        return copy.deepcopy(list(ui_flow or []))

    cleaned = copy.deepcopy(list(ui_flow or []))
    if cleaned and ios_flow_should_start_from_home(cleaned):
        cleaned = cleaned[1:]
    if cleaned and ios_step_targets_browser_address_bar(cleaned[0]):
        cleaned = cleaned[1:]
    if cleaned and ios_step_targets_browser_address_input(cleaned[0]):
        cleaned = cleaned[1:]
    return cleaned
