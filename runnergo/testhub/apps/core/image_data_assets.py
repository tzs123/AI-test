"""从表单截图识别字段，并生成可复用测试数据资产的辅助服务。"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Tuple


MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/jpg', 'image/webp', 'image/bmp'}

# 更具体的字段先匹配，避免“用户名”先被识别为“姓名”。
FIELD_RULES: Tuple[Tuple[str, str, str, bool], ...] = (
    ('verification_code', r'验证码|校验码|verification\s*code|captcha', 'verification_code', False),
    ('password', r'密码|口令|password|passwd|pwd', 'string', True),
    ('id_card', r'身份证|证件号码|id\s*card', 'id_card', True),
    ('license_plate', r'车牌号|车牌|号牌|license\s*plate|plate', 'string', False),
    # OCR 经常把“手机”识别成“手 机”，也可能只保留输入框提示词“手机号”。
    ('phone', r'手\s*机(?:\s*(?:号\s*码?|号码?))?|联系电话|电话号码|电话|mobile|phone', 'phone', False),
    ('email', r'邮箱地址|电子邮箱|邮箱|email|e-mail', 'email', False),
    ('username', r'用户名|登录名|账号|帐户|user\s*name|account', 'string', False),
    ('name', r'姓名|真实姓名|联系人|name', 'name', False),
    ('address', r'收货地址|详细地址|地址|address', 'address', False),
    ('company', r'公司名称|企业名称|公司|company', 'company', False),
    ('date', r'日期|生日|出生日期|date|birthday', 'date', False),
    ('amount', r'金额|价格|单价|amount|price', 'decimal', False),
    ('order_id', r'订单号|订单编号|order\s*(?:id|no)', 'string', False),
    ('product_name', r'商品名称|产品名称|商品|产品|product', 'string', False),
)

SAMPLE_VALUES = {
    'verification_code': '123456',
    'password': 'Test@123456',
    'id_card': '110101199001011234',
    'license_plate': '京A12345',
    'phone': '13800138000',
    'email': 'tester@example.com',
    'username': 'test_user',
    'name': '张三',
    'address': '北京市朝阳区测试路 1 号',
    'company': 'RunnerGo 测试公司',
    'date': '2026-01-01',
    'amount': '99.90',
    'order_id': 'ORD202608050001',
    'product_name': '测试商品',
}

PLACEHOLDER_RE = re.compile(r'^(请输入|请填写|请选择|enter|input|select|placeholder|示例|例如)', re.I)
OCR_NOISE_RE = re.compile(
    r'(?:https?://|www\.|localhost|127\.0\.0\.1|\b\d{1,3}(?:\.\d{1,3}){3}\b|'
    r'\bchannelid?\b|\bproductid?\b|dimensions\s*:|responsive\b|no throttling|'
    r'devtools|chrome|safari|firefox|edge|runnergo|文件|已导入)',
    re.I,
)
PHONE_NUMBER_RE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')


def _normalise_line(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '').replace('｜', '|').replace('：', ':')).strip(' |\t')


def _is_ocr_noise(line: str) -> bool:
    """过滤浏览器地址栏、DevTools 和导航文案，避免 URL 被识别为业务字段。"""
    normalized = _normalise_line(line)
    if not normalized:
        return True
    if OCR_NOISE_RE.search(normalized):
        return True
    # URL 中的 query 参数经常被 Tesseract 拆成多行，单独过滤参数名也要保留。
    if re.search(r'\b(?:id|url|port|host|path)\s*[=:]', normalized, flags=re.I):
        return True
    return False


def _field_matches(line: str) -> List[Tuple[str, str, bool]]:
    matches = []
    for key, pattern, field_type, sensitive in FIELD_RULES:
        if re.search(pattern, line, flags=re.I):
            matches.append((key, field_type, sensitive))
    return matches


def _extract_value(line: str, key: str) -> str:
    parts = re.split(r'\s*[:=]\s*', line, maxsplit=1)
    if len(parts) == 2:
        value = parts[1].strip(' -|')
        if key == 'verification_code':
            code_match = re.search(r'(?<!\d)\d{4,8}(?!\d)', value)
            return code_match.group() if code_match else ''
        if value and not PLACEHOLDER_RE.match(value):
            return value
    for rule_key, pattern, _field_type, _sensitive in FIELD_RULES:
        if rule_key == key:
            value = re.sub(pattern, '', line, count=1, flags=re.I).strip(' -|')
            if key == 'verification_code':
                code_match = re.search(r'(?<!\d)\d{4,8}(?!\d)', value)
                return code_match.group() if code_match else ''
            if value and not PLACEHOLDER_RE.match(value) and not _field_matches(value):
                return value
    return ''


def _field_evidence_fingerprint(line: str, key: str) -> Tuple[str, str]:
    """生成 OCR 结果去重指纹，避免多通道识别产生 phone_2、phone_3。"""
    evidence = line
    for rule_key, pattern, _field_type, _sensitive in FIELD_RULES:
        if rule_key == key:
            evidence = re.sub(pattern, '', evidence, count=1, flags=re.I)
            break
    # 同一个输入框在不同 OCR 通道中可能分别只识别出“手机”、
    # “手机 请输入手机号”或“请输入手机号”，这些都视为同一条证据。
    has_action_text = bool(re.search(r'(?:点击发送|获取验证码|发送)', evidence, flags=re.I))
    evidence = re.sub(r'(?:请输入|请填写|请选择|点击发送|获取验证码|发送)', '', evidence, flags=re.I)
    for _rule_key, pattern, _field_type, _sensitive in FIELD_RULES:
        evidence = re.sub(pattern, '', evidence, flags=re.I)
    evidence = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '', evidence).casefold()
    # “1 验证码 点击发送 a”这类 OCR 结果是按钮旁噪声，不应生成第二个验证码字段。
    if has_action_text and not re.search(r'\d{4,8}', evidence):
        evidence = ''
    return key, evidence


def _unique_key(key: str, used: set[str]) -> str:
    if key not in used:
        return key
    index = 2
    while f'{key}_{index}' in used:
        index += 1
    return f'{key}_{index}'


def infer_fields_from_text(text: str) -> Dict[str, Any]:
    """把 OCR 文本转换为可编辑的字段定义和一条安全示例数据。"""
    lines = [_normalise_line(line) for line in str(text or '').splitlines()]
    lines = [line for line in lines if line and len(line) <= 300 and not _is_ocr_noise(line)]
    fields: List[Dict[str, Any]] = []
    used_keys: set[str] = set()
    unknown_lines: List[str] = []
    seen_evidence: set[Tuple[str, str]] = set()

    for line in lines:
        matches = _field_matches(line)
        # 即使字段标签被 OCR 漏掉，只要识别到了标准的中国大陆手机号，也能建立手机号字段。
        if not matches and PHONE_NUMBER_RE.search(line):
            matches = [('phone', 'phone', False)]
        if matches:
            for key, field_type, sensitive in matches:
                evidence_fingerprint = _field_evidence_fingerprint(line, key)
                if evidence_fingerprint in seen_evidence:
                    continue
                seen_evidence.add(evidence_fingerprint)
                actual_key = _unique_key(key, used_keys)
                used_keys.add(actual_key)
                raw_value = _extract_value(line, key)
                sample_value = SAMPLE_VALUES.get(key, '') if sensitive else (raw_value or SAMPLE_VALUES.get(key, ''))
                fields.append({
                    'name': actual_key,
                    'label': key,
                    'type': field_type,
                    'config': {},
                    'sample_value': sample_value,
                    'sensitive': sensitive,
                    'source_text': line,
                })
            continue

        unknown_lines.append(line)

    # 只有整张图片没有命中任何常用字段时，才把 OCR 文本保留为自定义字段，
    # 避免“用户注册/登录”等页面标题混入已识别的字段列表。
    if not fields:
        for line in unknown_lines:
            if len(line) < 2 or re.fullmatch(r'[\W_]+', line, flags=re.UNICODE):
                continue
            key = _unique_key(f'field_{len(fields) + 1}', used_keys)
            used_keys.add(key)
            fields.append({
                'name': key,
                'label': line[:80],
                'type': 'string',
                'config': {'length': 12},
                'sample_value': 'test_value',
                'sensitive': False,
                'source_text': line,
            })

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for field in fields:
        if field['name'] in seen:
            continue
        seen.add(field['name'])
        deduped.append(field)

    return {
        'fields': deduped[:50],
        'field_definitions': [
            {
                'name': field['name'],
                'type': field['type'],
                'config': field.get('config') or {},
                'label': field.get('label') or field['name'],
            }
            for field in deduped[:50]
        ],
        'sample_payload': {
            field['name']: field.get('sample_value', '') for field in deduped[:50]
        },
        'raw_text': str(text or '')[:20000],
    }


def _build_ocr_variants(image, Image, ImageOps, ImageEnhance, ImageFilter):
    """生成适合表单截图的 OCR 版本，覆盖小字号、浅色字和稀疏字段。"""
    resampling = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')

    def resize_for_ocr(source):
        width, height = source.size
        max_dimension = max(width, height)
        scale = min(2.0, max(1.0, 4200 / max_dimension))
        if scale == 1.0:
            return source.copy()
        return source.resize((max(1, int(width * scale)), max(1, int(height * scale))), resampling)

    def enhance(source):
        grayscale = ImageOps.grayscale(source)
        grayscale = ImageOps.autocontrast(grayscale)
        grayscale = ImageEnhance.Contrast(grayscale).enhance(2.2)
        return grayscale.filter(ImageFilter.SHARPEN)

    width, height = image.size
    scaled = resize_for_ocr(image)
    body = image.crop((0, int(height * 0.08), width, max(int(height * 0.98), 1)))
    focus = image.crop((int(width * 0.08), int(height * 0.35), int(width * 0.92), int(height * 0.96)))
    enhanced = enhance(scaled)
    enhanced_body = enhance(resize_for_ocr(body))
    enhanced_focus = enhance(resize_for_ocr(focus))
    threshold_focus = enhanced_focus.point(lambda pixel: 255 if pixel >= 180 else 0)

    return (
        (image, ('--psm 6', '--psm 11')),
        (scaled, ('--psm 6', '--psm 11')),
        (enhanced, ('--psm 6', '--psm 11')),
        (enhanced_body, ('--psm 11',)),
        (enhanced_focus, ('--psm 11',)),
        (threshold_focus, ('--psm 11',)),
    )


def recognize_image_fields(uploaded_file) -> Dict[str, Any]:
    """识别上传图片中的表单字段。图片只在内存中处理，不落盘。"""
    if uploaded_file is None:
        raise ValueError('请上传图片')
    content_type = str(getattr(uploaded_file, 'content_type', '') or '').lower()
    if content_type and content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError('仅支持 PNG、JPG、JPEG、WEBP、BMP 图片')
    if int(getattr(uploaded_file, 'size', 0) or 0) > MAX_IMAGE_BYTES:
        raise ValueError('图片大小不能超过 10MB')

    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        import pytesseract

        uploaded_file.seek(0)
        with Image.open(uploaded_file) as opened:
            image = ImageOps.exif_transpose(opened).convert('RGB')
            ocr_texts: List[str] = []
            seen_ocr_texts: set[str] = set()
            for variant, configs in _build_ocr_variants(image, Image, ImageOps, ImageEnhance, ImageFilter):
                for config in configs:
                    try:
                        text = pytesseract.image_to_string(variant, lang='chi_sim+eng', config=config)
                    except pytesseract.TesseractError:
                        text = pytesseract.image_to_string(variant, lang='eng', config=config)
                    text = str(text or '').strip()
                    normalized_text = re.sub(r'\s+', ' ', text)
                    if normalized_text and normalized_text not in seen_ocr_texts:
                        seen_ocr_texts.add(normalized_text)
                        ocr_texts.append(text)
            raw_text = '\n'.join(ocr_texts)
    except Exception as exc:
        raise ValueError(f'图片 OCR 失败：{exc}') from exc

    result = infer_fields_from_text(raw_text)
    if not result['fields']:
        raise ValueError('图片中未识别到可用字段，请上传清晰的表单截图')
    result['backend'] = 'tesseract'
    result['source_filename'] = str(getattr(uploaded_file, 'name', '') or '')
    try:
        uploaded_file.seek(0)
        result['image_sha256'] = hashlib.sha256(uploaded_file.read()).hexdigest()
    finally:
        uploaded_file.seek(0)
    return result
