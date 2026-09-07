# -*- coding: utf-8 -*-
"""多模态原型理解与 UI 自动化步骤草稿生成。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from django.core.files.uploadedfile import UploadedFile

from .models import PrototypeUnderstandingAsset
from .services import DocumentProcessor


SUPPORTED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.csv', '.tsv',
    '.json', '.yaml', '.yml', '.txt', '.md', '.markdown', '.har', '.xml',
}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}

PAGE_PATTERNS = (
    ('login', '登录页', ('登录', '手机号', '手机', '验证码', '密码', '获取验证码', 'login', 'sign in')),
    ('register', '注册页', ('注册', '手机号', '验证码', '设置密码', 'register', 'sign up')),
    ('search', '搜索页', ('搜索', '关键词', '结果', 'search')),
    ('checkout', '下单页', ('购物车', '结算', '提交订单', '收货地址', 'checkout', 'order')),
    ('payment', '支付页', ('支付', '付款', '银行卡', '金额', 'pay')),
    ('profile', '个人中心页', ('我的', '个人中心', '设置', 'profile')),
)
ELEMENT_PATTERNS = (
    ('phone_input', '手机号输入框', 'input', ('手机号', '手机号码', 'mobile', 'phone')),
    ('username_input', '用户名输入框', 'input', ('用户名', '账号', 'username', 'account')),
    ('password_input', '密码输入框', 'input', ('密码', 'password')),
    ('get_code_button', '获取验证码按钮', 'click', ('获取验证码', '发送验证码', 'send code')),
    ('verify_code_input', '验证码输入框', 'input', ('验证码', 'verification code', 'verify code')),
    ('login_button', '登录按钮', 'click', ('登录', 'login', 'sign in')),
    ('home_assertion', '首页标识', 'assert', ('首页', 'home', '我的', '个人中心')),
    ('search_input', '搜索输入框', 'input', ('搜索', '关键词', 'search')),
    ('submit_button', '提交按钮', 'click', ('提交', '确认', '完成', 'submit', 'confirm')),
)
LOGIN_ORDER = (
    'phone_input',
    'username_input',
    'password_input',
    'get_code_button',
    'verify_code_input',
    'login_button',
    'home_assertion',
)


def validate_prototype_upload(file_obj: UploadedFile) -> None:
    ext = os.path.splitext(str(file_obj.name or '').lower())[1]
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError('仅支持产品原型图、图片、PDF、Word、Excel、Swagger、Postman、HAR、JSON/YAML 和文本文件')
    if getattr(file_obj, 'size', 0) and file_obj.size > 20 * 1024 * 1024:
        raise ValueError('文件不能超过 20MB')


def detect_asset_type(filename: str, content_type: str = '') -> str:
    name = str(filename or '').lower()
    ext = os.path.splitext(name)[1]
    content_type = str(content_type or '').lower()
    if ext in IMAGE_EXTENSIONS or content_type.startswith('image/'):
        return 'image'
    if ext == '.pdf':
        return 'pdf'
    if ext in ('.doc', '.docx'):
        return 'word'
    if ext in ('.xls', '.xlsx', '.xlsm', '.csv', '.tsv'):
        return 'excel'
    if ext in ('.json', '.yaml', '.yml', '.har'):
        if 'postman' in name or 'collection' in name:
            return 'postman'
        if 'swagger' in name or 'openapi' in name or 'api' in name:
            return 'swagger'
        return 'text'
    if ext in ('.txt', '.md', '.markdown', '.xml'):
        return 'text'
    return 'other'


def file_sha256(file_obj: UploadedFile) -> str:
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    digest = hashlib.sha256()
    for chunk in file_obj.chunks():
        digest.update(chunk)
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    return digest.hexdigest()


def extract_asset_text(asset: PrototypeUnderstandingAsset) -> str:
    file_path = asset.file.path
    ext = os.path.splitext(asset.original_name.lower())[1]
    if asset.asset_type == 'pdf':
        return DocumentProcessor.extract_text_from_pdf(file_path)
    if asset.asset_type == 'word':
        return (
            DocumentProcessor.extract_text_from_docx(file_path)
            if ext == '.docx' else DocumentProcessor.extract_text_from_doc(file_path)
        )
    if asset.asset_type == 'excel':
        return DocumentProcessor.extract_text_from_excel(file_path) if ext not in ('.csv', '.tsv') else DocumentProcessor.extract_text_from_txt(file_path)
    if asset.asset_type == 'image':
        text = DocumentProcessor.extract_text_from_image(file_path)
        return f"{asset.original_name}\n{text}"
    if ext in ('.json', '.yaml', '.yml', '.har', '.xml'):
        document_type = 'yaml' if ext in ('.yaml', '.yml') else ext.lstrip('.')
        return DocumentProcessor.extract_text_from_structured_file(file_path, document_type)
    return DocumentProcessor.extract_text_from_txt(file_path)


def analyze_asset(asset: PrototypeUnderstandingAsset) -> Dict[str, Any]:
    extracted_text = extract_asset_text(asset)
    analysis = analyze_text(
        f"{asset.title}\n{asset.original_name}\n{extracted_text}",
        asset_type=asset.asset_type,
    )
    asset.extracted_text = extracted_text[:20000]
    asset.analysis_result = analysis
    asset.status = 'analyzed'
    asset.error_message = ''
    asset.save(update_fields=['extracted_text', 'analysis_result', 'status', 'error_message', 'updated_at'])
    return analysis


def generate_ui_flow_draft(asset: PrototypeUnderstandingAsset) -> Dict[str, Any]:
    analysis = asset.analysis_result or analyze_asset(asset)
    steps = _steps_from_analysis(analysis)
    variables = _variables_from_steps(steps)
    warnings = [
        '这是 AI 生成的 UI 自动化步骤草稿，需要人工确认元素、测试数据和断言后再接入 UI 自动化执行。',
    ]
    if asset.asset_type in ('image', 'pdf'):
        warnings.append('图片/PDF 已通过 OCR 和规则识别生成候选结构；接入视觉模型后可进一步提升控件定位精度。')
    if analysis.get('api_contracts'):
        warnings.append('已识别 Swagger/Postman/HAR 接口契约，可作为验证码模拟、登录态准备或接口断言补充。')
    draft = {
        'schema_version': 'ui_automation_flow_draft.v1',
        'source': 'prototype_understanding',
        'asset_id': asset.id,
        'asset_name': asset.original_name,
        'page': analysis.get('page') or {},
        'steps': steps,
        'variables': variables,
        'assertions': analysis.get('assertions') or [],
        'api_contracts': analysis.get('api_contracts') or [],
        'warnings': warnings,
        'review_required': True,
        'execution_ready': False,
    }
    asset.ui_flow_draft = draft
    asset.status = 'draft_generated'
    asset.error_message = ''
    asset.save(update_fields=['ui_flow_draft', 'status', 'error_message', 'updated_at'])
    return draft


def analyze_text(text: str, *, asset_type: str = 'text') -> Dict[str, Any]:
    normalized = str(text or '').lower()
    page = _detect_page(normalized)
    elements = _detect_elements(normalized, page['type'])
    api_contracts = _extract_api_contracts(text, asset_type)
    assertions = _detect_assertions(normalized, page, api_contracts)
    return {
        'schema_version': 'prototype_understanding.v1',
        'asset_type': asset_type,
        'page': page,
        'elements': elements,
        'api_contracts': api_contracts,
        'assertions': assertions,
        'text_excerpt': re.sub(r'\s+', ' ', str(text or '')).strip()[:1200],
    }


def _detect_page(normalized: str) -> Dict[str, Any]:
    best = {'type': 'unknown', 'name': '未知页面', 'confidence': 0.35}
    for page_type, name, keywords in PAGE_PATTERNS:
        hits = sum(1 for keyword in keywords if keyword.lower() in normalized)
        if hits:
            confidence = round(min(0.55 + hits * 0.1, 0.94), 2)
            if confidence > best['confidence']:
                best = {'type': page_type, 'name': name, 'confidence': confidence}
    return best


def _detect_elements(normalized: str, page_type: str) -> List[Dict[str, Any]]:
    elements = []
    for key, label, role, keywords in ELEMENT_PATTERNS:
        hits = [keyword for keyword in keywords if keyword.lower() in normalized]
        if hits or (page_type == 'login' and key in LOGIN_ORDER):
            elements.append({
                'key': key,
                'label': label,
                'role': role,
                'keywords': list(keywords),
                'confidence': round(min(0.56 + len(hits) * 0.12, 0.92), 2) if hits else 0.52,
                'source': 'text_match' if hits else 'page_template',
            })
    if page_type == 'login':
        order = {key: index for index, key in enumerate(LOGIN_ORDER)}
        elements.sort(key=lambda item: order.get(item['key'], 99))
    return elements


def _steps_from_analysis(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps = [{
        'order': 1,
        'action': 'open_app',
        'target': 'APP',
        'value': '',
        'description': '打开 APP',
        'confidence': 0.9,
    }]
    for element in analysis.get('elements') or []:
        role = element.get('role')
        if role == 'input':
            value = _input_value(element)
            action = 'input'
            description = f"输入 {value}"
        elif role == 'assert':
            value = ''
            action = 'assert_visible'
            description = f"验证 {element.get('label')} 出现"
        else:
            value = ''
            action = 'click'
            description = f"点击 {element.get('label')}"
        steps.append({
            'order': len(steps) + 1,
            'action': action,
            'target': element.get('label'),
            'target_key': element.get('key'),
            'value': value,
            'description': description,
            'confidence': element.get('confidence', 0.5),
            'source': element.get('source'),
        })
    if not any(step['action'] == 'assert_visible' for step in steps):
        steps.append({
            'order': len(steps) + 1,
            'action': 'assert_visible',
            'target': '目标页面成功态',
            'value': '',
            'description': '验证目标页面或成功提示出现',
            'confidence': 0.45,
        })
    return steps


def _input_value(element: Dict[str, Any]) -> str:
    key = element.get('key') or ''
    label = element.get('label') or ''
    if 'phone' in key or '手机' in label:
        return '${PHONE}'
    if 'password' in key or '密码' in label:
        return '${PASSWORD}'
    if 'verify' in key or '验证码' in label:
        return '${VERIFY_CODE}'
    if 'username' in key or '账号' in label:
        return '${USERNAME}'
    if 'search' in key or '搜索' in label:
        return '${KEYWORD}'
    return '${INPUT_VALUE}'


def _variables_from_steps(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    labels = {
        'PHONE': ('手机号', False),
        'PASSWORD': ('密码', True),
        'VERIFY_CODE': ('验证码', True),
        'USERNAME': ('用户名/账号', False),
        'KEYWORD': ('搜索关键词', False),
        'INPUT_VALUE': ('输入内容', False),
    }
    names = []
    for step in steps:
        for match in re.finditer(r'\$\{([^}]+)\}', str(step.get('value') or '')):
            name = match.group(1)
            if name not in names:
                names.append(name)
    return [
        {
            'name': name,
            'label': labels.get(name, (name, False))[0],
            'sensitive': labels.get(name, (name, False))[1],
        }
        for name in names
    ]


def _extract_api_contracts(text: str, asset_type: str) -> List[Dict[str, Any]]:
    parsed = _parse_structured(text)
    if not parsed:
        return []
    if isinstance(parsed, dict) and 'paths' in parsed:
        contracts = []
        for path, methods in (parsed.get('paths') or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, config in methods.items():
                if str(method).lower() in ('get', 'post', 'put', 'patch', 'delete'):
                    contracts.append({
                        'source': 'swagger',
                        'method': str(method).upper(),
                        'path': str(path),
                        'summary': str((config or {}).get('summary') or (config or {}).get('operationId') or '')[:200],
                    })
        return contracts[:50]
    if isinstance(parsed, dict) and 'item' in parsed:
        return _walk_postman(parsed.get('item') or [])[:50]
    return []


def _detect_assertions(normalized: str, page: Dict[str, Any], api_contracts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    assertions = []
    if page.get('type') == 'login':
        assertions.append({'type': 'page_visible', 'target': '首页', 'reason': '登录成功后应进入首页'})
    if any(word in normalized for word in ('成功', '首页', '个人中心')):
        assertions.append({'type': 'text_visible', 'target': '成功态文本', 'reason': '原型/需求中包含成功态描述'})
    if api_contracts:
        assertions.append({'type': 'api_contract', 'target': '接口响应码/错误结构', 'reason': '接口契约可补充断言'})
    return assertions


def _parse_structured(text: str) -> Any:
    value = str(text or '').strip()
    start_candidates = [index for index in (value.find('{'), value.find('[')) if index >= 0]
    if start_candidates:
        value = value[min(start_candidates):]
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        import yaml
        return yaml.safe_load(value)
    except Exception:
        return None


def _walk_postman(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    contracts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('item'):
            contracts.extend(_walk_postman(item.get('item') or []))
            continue
        request = item.get('request') or {}
        url = request.get('url') or {}
        path = '/' + '/'.join(map(str, url.get('path') or [])) if isinstance(url, dict) else str(url)
        contracts.append({
            'source': 'postman',
            'method': str(request.get('method') or 'GET').upper(),
            'path': path,
            'summary': str(item.get('name') or '')[:200],
        })
    return contracts
