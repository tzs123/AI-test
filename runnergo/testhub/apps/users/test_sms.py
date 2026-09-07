"""
阿里云短信模块测试
覆盖 _sign / _percent_encode / _build_signature / _generate_verify_code /
check_rate_limit / send_register_verify_code / validate_verify_code
"""
import hmac
import hashlib
import base64
from unittest.mock import patch, MagicMock, call

from django.test import TestCase

from apps.users.sms import (
    _sign,
    _percent_encode,
    _build_signature,
    _generate_verify_code,
    check_rate_limit,
    send_register_verify_code,
    validate_verify_code,
    VERIFY_CODE_LENGTH,
    VERIFY_CODE_TTL,
    SMS_RATE_LIMIT_TTL,
)


class TestSign(TestCase):
    """_sign HMAC-SHA1 签名测试"""

    # ==================== 正向正常流程 ====================

    def test_hmac_sha1_correctness(self):
        """
        用例标题: HMAC-SHA1 签名正确性
        前置: 已知 access_key_secret 和 string_to_sign
        步骤: 调用 _sign('testsecret', 'POST&%2F&AccessKeyId%3Dtest')
        预期结果: 返回值与手动计算的 HMAC-SHA1 + base64 结果一致
        """
        secret = 'testsecret'
        message = 'POST&%2F&AccessKeyId%3Dtest'
        expected = base64.b64encode(
            hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha1).digest()
        ).decode('utf-8')

        result = _sign(secret, message)
        self.assertEqual(result, expected)

    def test_different_secrets_produce_different_signatures(self):
        """
        用例标题: 不同密钥产生不同签名
        前置: 无
        步骤: 分别用 'secret1' 和 'secret2' 对同一消息签名
        预期结果: 两个签名值不同
        """
        message = 'POST&%2F&test'
        sig1 = _sign('secret1', message)
        sig2 = _sign('secret2', message)
        self.assertNotEqual(sig1, sig2)

    def test_different_messages_produce_different_signatures(self):
        """
        用例标题: 不同消息产生不同签名
        前置: 无
        步骤: 用同一密钥对不同消息签名
        预期结果: 两个签名值不同
        """
        secret = 'testsecret'
        sig1 = _sign(secret, 'POST&%2F&msg1')
        sig2 = _sign(secret, 'POST&%2F&msg2')
        self.assertNotEqual(sig1, sig2)


class TestPercentEncode(TestCase):
    """_percent_encode URL 编码测试"""

    # ==================== 正向正常流程 ====================

    def test_alphanumeric_not_encoded(self):
        """
        用例标题: 普通字母数字不编码
        前置: 无
        步骤: 调用 _percent_encode('abcABC123')
        预期结果: 返回 'abcABC123'
        """
        self.assertEqual(_percent_encode('abcABC123'), 'abcABC123')

    def test_safe_chars_not_encoded(self):
        """
        用例标题: 安全字符 -_.~ 不编码
        前置: 无
        步骤: 调用 _percent_encode('-_.~')
        预期结果: 返回 '-_.~'
        """
        self.assertEqual(_percent_encode('-_.~'), '-_.~')

    def test_special_chars_encoded(self):
        """
        用例标题: 特殊字符被正确编码
        前置: 无
        步骤: 调用 _percent_encode('a&b=c')
        预期结果: & 和 = 被编码为 %26 和 %3D
        """
        result = _percent_encode('a&b=c')
        self.assertEqual(result, 'a%26b%3Dc')

    def test_space_encoded_as_percent(self):
        """
        用例标题: 空格被编码为 %20
        前置: 无
        步骤: 调用 _percent_encode('hello world')
        预期结果: 空格被编码为 %20
        """
        result = _percent_encode('hello world')
        self.assertEqual(result, 'hello%20world')

    # ==================== 边界极值 ====================

    def test_empty_string_encoding(self):
        """
        用例标题: 空字符串编码
        前置: 无
        步骤: 调用 _percent_encode('')
        预期结果: 返回空字符串
        """
        self.assertEqual(_percent_encode(''), '')

    def test_chinese_chars_encoded(self):
        """
        用例标题: 中文字符被编码
        前置: 无
        步骤: 调用 _percent_encode('测试')
        预期结果: 返回百分号编码格式
        """
        result = _percent_encode('测试')
        self.assertIn('%', result)


class TestBuildSignature(TestCase):
    """_build_signature 签名构建测试"""

    # ==================== 正向正常流程 ====================

    def test_sign_returns_non_empty_string(self):
        """
        用例标题: 签名构建返回非空字符串
        前置: 无
        步骤: 调用 _build_signature({'Action': 'SendSms'}, 'testsecret')
        预期结果: 返回非空字符串
        """
        result = _build_signature({'Action': 'SendSms'}, 'testsecret')
        self.assertTrue(isinstance(result, str))
        self.assertGreater(len(result), 0)

    def test_sign_uses_secret_with_ampersand_suffix(self):
        """
        用例标题: 签名构建使用密钥加 & 后缀
        前置: 无
        步骤: 手动计算 _sign('testsecret&', ...) 与 _build_signature 结果对比
        预期结果: _build_signature 内部调用 _sign 时密钥为 access_key_secret + '&'
        """
        params = {'Action': 'SendSms', 'Format': 'JSON'}
        secret = 'mysecret'
        # 手动构建签名
        from urllib.parse import quote
        sorted_keys = sorted(params.keys())
        canonicalized_query = '&'.join([
            f"{quote(k, safe='-_.~')}={quote(str(params[k]), safe='-_.~')}"
            for k in sorted_keys
        ])
        string_to_sign = 'POST&%2F&' + quote(canonicalized_query, safe='-_.~')
        expected = _sign(secret + '&', string_to_sign)

        result = _build_signature(params, secret)
        self.assertEqual(result, expected)

    def test_param_order_does_not_affect_signature(self):
        """
        用例标题: 参数顺序不影响签名
        前置: 无
        步骤: 用不同顺序的参数字典调用 _build_signature
        预期结果: 结果相同（因为内部会排序）
        """
        params1 = {'Action': 'SendSms', 'Format': 'JSON', 'Version': '2017-05-25'}
        params2 = {'Version': '2017-05-25', 'Action': 'SendSms', 'Format': 'JSON'}
        secret = 'testsecret'
        self.assertEqual(
            _build_signature(params1, secret),
            _build_signature(params2, secret),
        )


class TestGenerateVerifyCode(TestCase):
    """_generate_verify_code 验证码生成测试"""

    # ==================== 正向正常流程 ====================

    def test_code_length_is_6(self):
        """
        用例标题: 验证码长度为 6
        前置: 无
        步骤: 调用 _generate_verify_code()
        预期结果: 返回字符串长度为 VERIFY_CODE_LENGTH (6)
        """
        code = _generate_verify_code()
        self.assertEqual(len(code), VERIFY_CODE_LENGTH)

    def test_code_is_pure_digits(self):
        """
        用例标题: 验证码为纯数字
        前置: 无
        步骤: 多次调用 _generate_verify_code()，检查每个字符是否为数字
        预期结果: 所有字符均为数字
        """
        for _ in range(100):
            code = _generate_verify_code()
            self.assertTrue(code.isdigit())

    def test_codes_are_random(self):
        """
        用例标题: 多次生成验证码具有随机性
        前置: 无
        步骤: 生成 50 个验证码，检查去重后数量
        预期结果: 不应全部相同
        """
        codes = {_generate_verify_code() for _ in range(50)}
        self.assertGreater(len(codes), 1)


class TestCheckRateLimit(TestCase):
    """check_rate_limit 频率限制检查测试"""

    # ==================== 正向正常流程 ====================

    @patch('apps.users.sms.get_redis')
    def test_not_limited_returns_allowed(self, mock_get_redis):
        """
        用例标题: 未限制时返回允许
        前置: mock Redis ttl 返回 -2（key 不存在）
        步骤: 调用 check_rate_limit('13800138000')
        预期结果: 返回 (True, 0)
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = -2

        allowed, remaining = check_rate_limit('13800138000')

        self.assertTrue(allowed)
        self.assertEqual(remaining, 0)

    @patch('apps.users.sms.get_redis')
    def test_limited_returns_remaining_seconds(self, mock_get_redis):
        """
        用例标题: 已限制时返回剩余秒数
        前置: mock Redis ttl 返回 45（key 还剩 45 秒过期）
        步骤: 调用 check_rate_limit('13800138000')
        预期结果: 返回 (False, 45)
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = 45

        allowed, remaining = check_rate_limit('13800138000')

        self.assertFalse(allowed)
        self.assertEqual(remaining, 45)

    # ==================== 边界极值 ====================

    @patch('apps.users.sms.get_redis')
    def test_ttl_zero_returns_allowed(self, mock_get_redis):
        """
        用例标题: TTL 为 0 时返回允许
        前置: mock Redis ttl 返回 0（key 刚好过期）
        步骤: 调用 check_rate_limit('13800138000')
        预期结果: 返回 (True, 0)
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = 0

        allowed, remaining = check_rate_limit('13800138000')

        self.assertTrue(allowed)
        self.assertEqual(remaining, 0)

    @patch('apps.users.sms.get_redis')
    def test_ttl_negative_returns_allowed(self, mock_get_redis):
        """
        用例标题: TTL 为负值时返回允许
        前置: mock Redis ttl 返回 -1（key 无过期时间）或 -2（key 不存在）
        步骤: 调用 check_rate_limit('13800138000')
        预期结果: 返回 (True, 0)
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        for ttl_val in [-1, -2]:
            mock_redis.ttl.return_value = ttl_val
            allowed, remaining = check_rate_limit('13800138000')
            self.assertTrue(allowed)
            self.assertEqual(remaining, 0)

    @patch('apps.users.sms.get_redis')
    def test_ttl_one_returns_limited(self, mock_get_redis):
        """
        用例标题: TTL 为 1 秒时返回限制
        前置: mock Redis ttl 返回 1
        步骤: 调用 check_rate_limit('13800138000')
        预期结果: 返回 (False, 1)
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = 1

        allowed, remaining = check_rate_limit('13800138000')

        self.assertFalse(allowed)
        self.assertEqual(remaining, 1)


class TestSendRegisterVerifyCode(TestCase):
    """send_register_verify_code 发送注册验证码测试"""

    # ==================== 正向正常流程 ====================

    @patch('apps.users.sms.send_sms')
    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_send_code_successfully(self, mock_validate_captcha, mock_get_redis, mock_send_sms):
        """
        用例标题: 成功发送验证码
        前置: mock 图形验证码通过、频率限制允许、短信发送成功
        步骤: 调用 send_register_verify_code('13800138000', 'token', 'ABCD')
        预期结果: 返回 success=True，包含 verify_code_token 和 message
        """
        mock_validate_captcha.return_value = True
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = -2
        mock_send_sms.return_value = (True, {'Code': 'OK'})

        result = send_register_verify_code('13800138000', 'captcha-token', 'ABCD')

        self.assertTrue(result['success'])
        self.assertIn('verify_code_token', result)
        self.assertEqual(result['message'], '验证码已发送')

    @patch('apps.users.sms.send_sms')
    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_success_stores_three_redis_keys(self, mock_validate_captcha, mock_get_redis, mock_send_sms):
        """
        用例标题: 成功发送时 Redis 存储三个 key
        前置: mock 图形验证码通过、频率限制允许、短信发送成功
        步骤: 调用 send_register_verify_code，检查 setex 调用次数和参数
        预期结果: setex 被调用 3 次，分别存储验证码、频率限制、手机号
        """
        mock_validate_captcha.return_value = True
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = -2
        mock_send_sms.return_value = (True, {'Code': 'OK'})

        result = send_register_verify_code('13800138000', 'captcha-token', 'ABCD')
        token = result['verify_code_token']

        setex_calls = mock_redis.setex.call_args_list
        self.assertEqual(len(setex_calls), 3)

        keys = [c[0][0] for c in setex_calls]
        self.assertIn(f'sms:verify_code:{token}', keys)
        self.assertIn(f'sms:limit:13800138000', keys)
        self.assertIn(f'sms:phone:{token}', keys)

    # ==================== 反向异常入参 ====================

    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_captcha_validation_fails(self, mock_validate_captcha, mock_get_redis):
        """
        用例标题: 图形验证码校验失败
        前置: mock 图形验证码校验返回 False
        步骤: 调用 send_register_verify_code('13800138000', 'bad-token', 'XXXX')
        预期结果: 返回 success=False，error 为 '图形验证码错误或已过期'
        """
        mock_validate_captcha.return_value = False

        result = send_register_verify_code('13800138000', 'bad-token', 'XXXX')

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], '图形验证码错误或已过期')

    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_rate_limit_rejects_send(self, mock_validate_captcha, mock_get_redis):
        """
        用例标题: 频率限制拒绝发送
        前置: mock 图形验证码通过、Redis ttl 返回 30（频率限制中）
        步骤: 调用 send_register_verify_code('13800138000', 'token', 'ABCD')
        预期结果: 返回 success=False，error 包含 '发送过于频繁'
        """
        mock_validate_captcha.return_value = True
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = 30

        result = send_register_verify_code('13800138000', 'token', 'ABCD')

        self.assertFalse(result['success'])
        self.assertIn('发送过于频繁', result['error'])
        self.assertIn('30', result['error'])

    @patch('apps.users.sms.send_sms')
    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_sms_failure_cleans_redis(self, mock_validate_captcha, mock_get_redis, mock_send_sms):
        """
        用例标题: 短信发送失败时清理 Redis
        前置: mock 图形验证码通过、频率限制允许、短信发送失败
        步骤: 调用 send_register_verify_code，检查 delete 调用
        预期结果: delete 被调用 3 次，清理验证码、频率限制、手机号三个 key
        """
        mock_validate_captcha.return_value = True
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = -2
        mock_send_sms.return_value = (False, {'Code': 'isv.BUSINESS_LIMIT_CONTROL', 'Message': '业务限流'})

        result = send_register_verify_code('13800138000', 'captcha-token', 'ABCD')

        self.assertFalse(result['success'])
        self.assertIn('短信发送失败', result['error'])

        # 验证 delete 被调用了 3 次
        delete_calls = mock_redis.delete.call_args_list
        self.assertEqual(len(delete_calls), 3)

        delete_keys = [c[0][0] for c in delete_calls]
        # 验证三个 key 都被清理
        self.assertTrue(any('sms:verify_code:' in k for k in delete_keys))
        self.assertIn('sms:limit:13800138000', delete_keys)
        self.assertTrue(any('sms:phone:' in k for k in delete_keys))

    # ==================== 非法参数 ====================

    @patch('apps.users.sms.get_redis')
    @patch('apps.users.captcha.validate_captcha')
    def test_empty_phone_sends(self, mock_validate_captcha, mock_get_redis):
        """
        用例标题: 空手机号发送
        前置: mock 图形验证码通过
        步骤: 调用 check_rate_limit('')
        预期结果: 频率限制 key 使用空字符串，流程继续（参数校验应在视图层）
        """
        mock_validate_captcha.return_value = True
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.ttl.return_value = -2

        # 空手机号也能走到频率限制检查，说明参数校验应在视图层
        check_rate_limit('')
        mock_redis.ttl.assert_called_once_with('sms:limit:')


class TestValidateVerifyCode(TestCase):
    """validate_verify_code 验证码校验测试"""

    # ==================== 正向正常流程 ====================

    @patch('apps.users.sms.get_redis')
    def test_correct_code_passes(self, mock_get_redis):
        """
        用例标题: 验证码正确校验通过
        前置: mock Redis 返回存储的验证码 '123456' 和手机号 '13800138000'
        步骤: 调用 validate_verify_code('13800138000', 'token', '123456')
        预期结果: 返回 (True, '')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13800138000',
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '123456')

        self.assertTrue(valid)
        self.assertEqual(error, '')

    @patch('apps.users.sms.get_redis')
    def test_success_cleans_redis(self, mock_get_redis):
        """
        用例标题: 校验通过后清理 Redis 数据
        前置: mock Redis 返回存储的验证码和手机号
        步骤: 调用 validate_verify_code 成功后，检查 delete 调用
        预期结果: delete 被调用 2 次，分别删除验证码 key 和手机号 key
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13800138000',
        }.get(key)

        validate_verify_code('13800138000', 'token', '123456')

        delete_calls = mock_redis.delete.call_args_list
        self.assertEqual(len(delete_calls), 2)
        delete_keys = [c[0][0] for c in delete_calls]
        self.assertIn('sms:verify_code:token', delete_keys)
        self.assertIn('sms:phone:token', delete_keys)

    # ==================== 反向异常入参 ====================

    @patch('apps.users.sms.get_redis')
    def test_wrong_code_fails(self, mock_get_redis):
        """
        用例标题: 验证码错误校验失败
        前置: mock Redis 返回存储的验证码 '123456'
        步骤: 调用 validate_verify_code('13800138000', 'token', '654321')
        预期结果: 返回 (False, '验证码错误')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13800138000',
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '654321')

        self.assertFalse(valid)
        self.assertEqual(error, '验证码错误')

    @patch('apps.users.sms.get_redis')
    def test_expired_code_fails(self, mock_get_redis):
        """
        用例标题: 验证码过期校验失败
        前置: mock Redis 返回 None（验证码已过期）
        步骤: 调用 validate_verify_code('13800138000', 'expired-token', '123456')
        预期结果: 返回 (False, '验证码已过期，请重新获取')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = None

        valid, error = validate_verify_code('13800138000', 'expired-token', '123456')

        self.assertFalse(valid)
        self.assertEqual(error, '验证码已过期，请重新获取')

    @patch('apps.users.sms.get_redis')
    def test_phone_mismatch_fails(self, mock_get_redis):
        """
        用例标题: 手机号不匹配校验失败
        前置: mock Redis 返回验证码 '123456'，但手机号为 '13900139000'
        步骤: 调用 validate_verify_code('13800138000', 'token', '123456')
        预期结果: 返回 (False, '手机号与验证码不匹配')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13900139000',
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '123456')

        self.assertFalse(valid)
        self.assertEqual(error, '手机号与验证码不匹配')

    @patch('apps.users.sms.get_redis')
    def test_wrong_code_does_not_delete_redis(self, mock_get_redis):
        """
        用例标题: 验证码错误时不删除 Redis 数据
        前置: mock Redis 返回存储的验证码 '123456'
        步骤: 调用 validate_verify_code 使用错误验证码，检查 delete 是否被调用
        预期结果: delete 未被调用
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13800138000',
        }.get(key)

        validate_verify_code('13800138000', 'token', '654321')

        mock_redis.delete.assert_not_called()

    @patch('apps.users.sms.get_redis')
    def test_phone_mismatch_does_not_delete_redis(self, mock_get_redis):
        """
        用例标题: 手机号不匹配时不删除 Redis 数据
        前置: mock Redis 返回验证码和不同手机号
        步骤: 调用 validate_verify_code 使用不匹配手机号，检查 delete 是否被调用
        预期结果: delete 未被调用
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13900139000',
        }.get(key)

        validate_verify_code('13800138000', 'token', '123456')

        mock_redis.delete.assert_not_called()

    # ==================== 非法参数 ====================

    @patch('apps.users.sms.get_redis')
    def test_empty_code_fails(self, mock_get_redis):
        """
        用例标题: 空验证码校验失败
        前置: mock Redis 返回存储的验证码 '123456'
        步骤: 调用 validate_verify_code('13800138000', 'token', '')
        预期结果: 返回 (False, '验证码错误')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': b'13800138000',
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '')

        self.assertFalse(valid)
        self.assertEqual(error, '验证码错误')

    @patch('apps.users.sms.get_redis')
    def test_empty_token_fails(self, mock_get_redis):
        """
        用例标题: 空 token 校验失败
        前置: mock Redis 返回 None
        步骤: 调用 validate_verify_code('13800138000', '', '123456')
        预期结果: 返回 (False, '验证码已过期，请重新获取')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = None

        valid, error = validate_verify_code('13800138000', '', '123456')

        self.assertFalse(valid)
        self.assertEqual(error, '验证码已过期，请重新获取')

    # ==================== 边界极值 ====================

    @patch('apps.users.sms.get_redis')
    def test_string_type_redis_value_also_passes(self, mock_get_redis):
        """
        用例标题: Redis 存储为字符串类型也能校验通过
        前置: mock Redis 返回字符串类型的验证码和手机号（非 bytes）
        步骤: 调用 validate_verify_code('13800138000', 'token', '123456')
        预期结果: 返回 (True, '')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': '123456',
            'sms:phone:token': '13800138000',
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '123456')

        self.assertTrue(valid)
        self.assertEqual(error, '')

    @patch('apps.users.sms.get_redis')
    def test_phone_key_missing_fails(self, mock_get_redis):
        """
        用例标题: 手机号 key 不存在时校验失败
        前置: mock Redis 返回验证码，但手机号 key 返回 None
        步骤: 调用 validate_verify_code('13800138000', 'token', '123456')
        预期结果: 返回 (False, '手机号与验证码不匹配')
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = lambda key: {
            'sms:verify_code:token': b'123456',
            'sms:phone:token': None,
        }.get(key)

        valid, error = validate_verify_code('13800138000', 'token', '123456')

        self.assertFalse(valid)
        self.assertEqual(error, '手机号与验证码不匹配')
