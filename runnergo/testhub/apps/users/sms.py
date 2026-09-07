"""
阿里云短信发送模块
复用 zhiqiong 项目的短信通道配置
包含发送频率限制（同一手机号 60 秒内只能发一次）
"""
import json
import hmac
import hashlib
import base64
import uuid
import time
from datetime import datetime
from urllib.parse import quote
import requests
from django.conf import settings
from .redis_client import get_redis


def _sign(access_key_secret, string_to_sign):
    """阿里云签名 V1 算法"""
    h = hmac.new(
        access_key_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha1
    )
    return base64.b64encode(h.digest()).decode('utf-8')


def _percent_encode(s):
    """URL 编码（阿里云特殊规则）"""
    return quote(s, safe='-_.~')


def _build_signature(params, access_key_secret):
    """构建阿里云请求签名"""
    sorted_keys = sorted(params.keys())
    canonicalized_query = '&'.join([
        f"{_percent_encode(k)}={_percent_encode(str(params[k]))}"
        for k in sorted_keys
    ])
    string_to_sign = 'POST&%2F&' + _percent_encode(canonicalized_query)
    return _sign(access_key_secret + '&', string_to_sign)


def send_sms(phone_number, template_code, template_param, sign_name=None):
    """
    通过阿里云 SMS 发送短信

    Args:
        phone_number: 手机号
        template_code: 短信模板编号
        template_param: 模板参数 dict，如 {'code': '123456'}
        sign_name: 签名名称，默认使用配置中的 SMS_SIGN_NAME
    """
    access_key_id = settings.SMS_ACCESS_KEY_ID
    access_key_secret = settings.SMS_ACCESS_KEY_SECRET
    if sign_name is None:
        sign_name = settings.SMS_SIGN_NAME

    params = {
        'AccessKeyId': access_key_id,
        'Action': 'SendSms',
        'Format': 'JSON',
        'PhoneNumbers': phone_number,
        'SignName': sign_name,
        'SignatureMethod': 'HMAC-SHA1',
        'SignatureNonce': str(uuid.uuid4()),
        'SignatureVersion': '1.0',
        'TemplateCode': template_code,
        'TemplateParam': json.dumps(template_param, ensure_ascii=False),
        'Timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'Version': '2017-05-25',
    }

    signature = _build_signature(params, access_key_secret)
    params['Signature'] = signature

    response = requests.post(
        'https://dysmsapi.aliyuncs.com/',
        data=params,
        timeout=10
    )
    result = response.json()
    return result.get('Code') == 'OK', result


# ========== 验证码相关业务逻辑 ==========

VERIFY_CODE_LENGTH = 6
VERIFY_CODE_TTL = 300        # 验证码 5 分钟有效
SMS_RATE_LIMIT_TTL = 60      # 同一手机号 60 秒内只能发一次


def _generate_verify_code():
    """生成 6 位数字验证码"""
    import random
    return ''.join([str(random.randint(0, 9)) for _ in range(VERIFY_CODE_LENGTH)])


def check_rate_limit(phone_number):
    """检查发送频率限制，返回 (allowed: bool, remaining_seconds: int)"""
    redis_client = get_redis()
    key = f"sms:limit:{phone_number}"
    ttl = redis_client.ttl(key)
    if ttl > 0:
        return False, ttl
    return True, 0


def send_register_verify_code(phone_number, captcha_token, captcha_code):
    """
    发送注册验证码流程：
    1. 校验图形验证码
    2. 校验发送频率限制
    3. 生成 6 位验证码存 Redis
    4. 调用阿里云发送短信
    """
    from apps.users.captcha import validate_captcha

    # 校验图形验证码
    if not validate_captcha(captcha_token, captcha_code):
        return {'success': False, 'error': '图形验证码错误或已过期'}

    # 校验发送频率
    allowed, remaining = check_rate_limit(phone_number)
    if not allowed:
        return {'success': False, 'error': f'发送过于频繁，请 {remaining} 秒后再试'}

    # 生成验证码
    code = _generate_verify_code()
    token = str(uuid.uuid4())

    # 存储到 Redis
    redis_client = get_redis()
    redis_client.setex(f"sms:verify_code:{token}", VERIFY_CODE_TTL, code)
    redis_client.setex(f"sms:limit:{phone_number}", SMS_RATE_LIMIT_TTL, '1')
    # 存储手机号，注册时需要校验手机号与验证码的对应关系
    redis_client.setex(f"sms:phone:{token}", VERIFY_CODE_TTL, phone_number)

    # 发送短信
    success, result = send_sms(
        phone_number=phone_number,
        template_code=settings.SMS_REGISTER_TEMPLATE_CODE,
        template_param={'code': code},
    )

    if not success:
        # 发送失败时清理 Redis
        redis_client.delete(f"sms:verify_code:{token}")
        redis_client.delete(f"sms:limit:{phone_number}")
        redis_client.delete(f"sms:phone:{token}")
        return {'success': False, 'error': f'短信发送失败: {result.get("Message", "未知错误")}'}

    return {
        'success': True,
        'verify_code_token': token,
        'message': '验证码已发送',
    }


def validate_verify_code(phone_number, verify_code_token, verify_code):
    """校验短信验证码，返回 (valid: bool, error: str)"""
    redis_client = get_redis()

    # 校验验证码
    code_key = f"sms:verify_code:{verify_code_token}"
    stored_code = redis_client.get(code_key)
    if stored_code is None:
        return False, '验证码已过期，请重新获取'

    stored_code = stored_code.decode('utf-8') if isinstance(stored_code, bytes) else stored_code
    if stored_code != verify_code:
        return False, '验证码错误'

    # 校验手机号是否匹配
    phone_key = f"sms:phone:{verify_code_token}"
    stored_phone = redis_client.get(phone_key)
    stored_phone = stored_phone.decode('utf-8') if isinstance(stored_phone, bytes) else stored_phone
    if stored_phone != phone_number:
        return False, '手机号与验证码不匹配'

    # 校验通过，清理验证码
    redis_client.delete(code_key)
    redis_client.delete(phone_key)

    return True, ''
