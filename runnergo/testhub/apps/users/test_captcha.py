"""
图形验证码模块测试
覆盖 generate_captcha_code / create_captcha_image / create_captcha / validate_captcha
"""
import string
import base64
from unittest.mock import patch, MagicMock

from django.test import TestCase

from apps.users.captcha import (
    generate_captcha_code,
    create_captcha_image,
    create_captcha,
    validate_captcha,
)

# 易混淆字符集合
CONFUSABLE_CHARS = set('O0I1L')


class TestGenerateCaptchaCode(TestCase):
    """generate_captcha_code 测试"""

    # ==================== 正向正常流程 ====================

    def test_默认长度生成4位验证码(self):
        """
        用例标题: 默认长度生成4位验证码
        前置: 无
        步骤: 调用 generate_captcha_code() 不传参
        预期结果: 返回字符串长度为 4
        """
        code = generate_captcha_code()
        self.assertEqual(len(code), 4)

    def test_自定义长度生成验证码(self):
        """
        用例标题: 自定义长度生成验证码
        前置: 无
        步骤: 调用 generate_captcha_code(length=6)
        预期结果: 返回字符串长度为 6
        """
        code = generate_captcha_code(length=6)
        self.assertEqual(len(code), 6)

    def test_验证码不含易混淆字符(self):
        """
        用例标题: 验证码不含易混淆字符
        前置: 无
        步骤: 多次调用 generate_captcha_code，检查结果中是否包含 O/0/I/1/L
        预期结果: 所有生成的验证码均不含易混淆字符
        """
        for _ in range(200):
            code = generate_captcha_code()
            for char in code:
                self.assertNotIn(char, CONFUSABLE_CHARS)

    def test_验证码字符来自合法字符集(self):
        """
        用例标题: 验证码字符来自合法字符集
        前置: 无
        步骤: 构造合法字符集（大写字母+数字去掉易混淆字符），多次生成验证码，检查每个字符是否在合法字符集中
        预期结果: 所有字符均在合法字符集中
        """
        valid_chars = string.ascii_uppercase + string.digits
        valid_chars = valid_chars.replace('O', '').replace('0', '').replace('I', '').replace('1', '').replace('L', '')
        valid_set = set(valid_chars)
        for _ in range(200):
            code = generate_captcha_code()
            for char in code:
                self.assertIn(char, valid_set)

    def test_多次生成验证码具有随机性(self):
        """
        用例标题: 多次生成验证码具有随机性
        前置: 无
        步骤: 连续生成 50 个验证码，检查是否至少存在两个不同的值
        预期结果: 不应全部相同（概率极低）
        """
        codes = {generate_captcha_code() for _ in range(50)}
        self.assertGreater(len(codes), 1)

    # ==================== 边界极值 ====================

    def test_最小长度1生成验证码(self):
        """
        用例标题: 最小长度1生成验证码
        前置: 无
        步骤: 调用 generate_captcha_code(length=1)
        预期结果: 返回字符串长度为 1，且不含易混淆字符
        """
        code = generate_captcha_code(length=1)
        self.assertEqual(len(code), 1)
        self.assertNotIn(code, CONFUSABLE_CHARS)

    def test_较大长度生成验证码(self):
        """
        用例标题: 较大长度生成验证码
        前置: 无
        步骤: 调用 generate_captcha_code(length=20)
        预期结果: 返回字符串长度为 20，且不含易混淆字符
        """
        code = generate_captcha_code(length=20)
        self.assertEqual(len(code), 20)
        for char in code:
            self.assertNotIn(char, CONFUSABLE_CHARS)


class TestCreateCaptchaImage(TestCase):
    """create_captcha_image 测试"""

    # ==================== 正向正常流程 ====================

    def test_返回base64格式图片(self):
        """
        用例标题: 返回 base64 格式图片
        前置: 无
        步骤: 调用 create_captcha_image('ABCD')
        预期结果: 返回值以 'data:image/png;base64,' 开头
        """
        result = create_captcha_image('ABCD')
        self.assertTrue(result.startswith('data:image/png;base64,'))

    def test_base64部分可正常解码(self):
        """
        用例标题: base64 部分可正常解码
        前置: 无
        步骤: 调用 create_captcha_image('ABCD')，提取 base64 部分并解码
        预期结果: base64 解码成功，得到非空字节数据
        """
        result = create_captcha_image('ABCD')
        b64_part = result.split(',', 1)[1]
        decoded = base64.b64decode(b64_part)
        self.assertGreater(len(decoded), 0)

    def test_不同验证码生成不同图片(self):
        """
        用例标题: 不同验证码生成不同图片
        前置: 无
        步骤: 分别对 'ABCD' 和 'EFGH' 调用 create_captcha_image
        预期结果: 两次返回的图片数据不同
        """
        img1 = create_captcha_image('ABCD')
        img2 = create_captcha_image('EFGH')
        self.assertNotEqual(img1, img2)


class TestCreateCaptcha(TestCase):
    """create_captcha 测试"""

    # ==================== 正向正常流程 ====================

    @patch('apps.users.captcha.get_redis')
    def test_返回token和image字段(self, mock_get_redis):
        """
        用例标题: 返回 token 和 image 字段
        前置: mock Redis 客户端
        步骤: 调用 create_captcha()
        预期结果: 返回字典包含 'token' 和 'image' 两个 key
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        result = create_captcha()

        self.assertIn('token', result)
        self.assertIn('image', result)
        self.assertTrue(result['image'].startswith('data:image/png;base64,'))

    @patch('apps.users.captcha.get_redis')
    def test_redis存储验证码使用正确key格式(self, mock_get_redis):
        """
        用例标题: Redis 存储验证码使用正确 key 格式
        前置: mock Redis 客户端
        步骤: 调用 create_captcha()，检查 setex 调用参数
        预期结果: setex 以 'captcha:{token}' 为 key，TTL 为 300
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        result = create_captcha()

        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        key, ttl, code = call_args[0]
        self.assertEqual(key, f"captcha:{result['token']}")
        self.assertEqual(ttl, 300)
        self.assertEqual(len(code), 4)

    @patch('apps.users.captcha.get_redis')
    def test_自定义TTL存储(self, mock_get_redis):
        """
        用例标题: 自定义 TTL 存储
        前置: mock Redis 客户端
        步骤: 调用 create_captcha(ttl=600)，检查 setex 调用的 TTL
        预期结果: setex 的 TTL 参数为 600
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        result = create_captcha(ttl=600)

        call_args = mock_redis.setex.call_args
        _, ttl, _ = call_args[0]
        self.assertEqual(ttl, 600)

    @patch('apps.users.captcha.get_redis')
    def test_token为合法UUID格式(self, mock_get_redis):
        """
        用例标题: token 为合法 UUID 格式
        前置: mock Redis 客户端
        步骤: 调用 create_captcha()，检查 token 格式
        预期结果: token 可被解析为 UUID
        """
        import uuid
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        result = create_captcha()

        # 验证 token 是合法 UUID
        parsed = uuid.UUID(result['token'])
        self.assertEqual(str(parsed), result['token'])


class TestValidateCaptcha(TestCase):
    """validate_captcha 测试"""

    # ==================== 正向正常流程 ====================

    @patch('apps.users.captcha.get_redis')
    def test_正确验证码校验通过(self, mock_get_redis):
        """
        用例标题: 正确验证码校验通过
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', 'ABCD')
        预期结果: 返回 True
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        result = validate_captcha('test-token', 'ABCD')

        self.assertTrue(result)

    @patch('apps.users.captcha.get_redis')
    def test_大小写不敏感验证(self, mock_get_redis):
        """
        用例标题: 大小写不敏感验证
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', 'abcd')
        预期结果: 返回 True（大小写不敏感）
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        result = validate_captcha('test-token', 'abcd')

        self.assertTrue(result)

    @patch('apps.users.captcha.get_redis')
    def test_验证成功后删除Redis中的验证码(self, mock_get_redis):
        """
        用例标题: 验证成功后删除 Redis 中的验证码（一次性使用）
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', 'ABCD')
        预期结果: redis_client.delete 被调用，key 为 'captcha:test-token'
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        validate_captcha('test-token', 'ABCD')

        mock_redis.delete.assert_called_once_with('captcha:test-token')

    # ==================== 反向异常入参 ====================

    @patch('apps.users.captcha.get_redis')
    def test_错误验证码校验失败(self, mock_get_redis):
        """
        用例标题: 错误验证码校验失败
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', 'WXYZ')
        预期结果: 返回 False
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        result = validate_captcha('test-token', 'WXYZ')

        self.assertFalse(result)

    @patch('apps.users.captcha.get_redis')
    def test_错误验证码不删除Redis数据(self, mock_get_redis):
        """
        用例标题: 错误验证码不删除 Redis 数据
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', 'WXYZ')
        预期结果: redis_client.delete 未被调用
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        validate_captcha('test-token', 'WXYZ')

        mock_redis.delete.assert_not_called()

    @patch('apps.users.captcha.get_redis')
    def test_过期token校验失败(self, mock_get_redis):
        """
        用例标题: 过期 token 校验失败
        前置: mock Redis 返回 None（模拟 key 已过期）
        步骤: 调用 validate_captcha('expired-token', 'ABCD')
        预期结果: 返回 False
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = None

        result = validate_captcha('expired-token', 'ABCD')

        self.assertFalse(result)

    # ==================== 非法参数 ====================

    @patch('apps.users.captcha.get_redis')
    def test_空token校验失败(self, mock_get_redis):
        """
        用例标题: 空 token 校验失败
        前置: mock Redis 返回 None
        步骤: 调用 validate_captcha('', 'ABCD')
        预期结果: 返回 False
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = None

        result = validate_captcha('', 'ABCD')

        self.assertFalse(result)

    @patch('apps.users.captcha.get_redis')
    def test_空验证码校验失败(self, mock_get_redis):
        """
        用例标题: 空验证码校验失败
        前置: mock Redis 返回存储的验证码 'ABCD'
        步骤: 调用 validate_captcha('test-token', '')
        预期结果: 返回 False
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = b'ABCD'

        result = validate_captcha('test-token', '')

        self.assertFalse(result)

    @patch('apps.users.captcha.get_redis')
    def test_验证码存储为字符串类型也能通过(self, mock_get_redis):
        """
        用例标题: 验证码存储为字符串类型也能通过
        前置: mock Redis 返回字符串类型的验证码 'ABCD'（非 bytes）
        步骤: 调用 validate_captcha('test-token', 'ABCD')
        预期结果: 返回 True
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.return_value = 'ABCD'

        result = validate_captcha('test-token', 'ABCD')

        self.assertTrue(result)

    # ==================== 边界极值 ====================

    @patch('apps.users.captcha.get_redis')
    def test_一次性使用_第二次验证失败(self, mock_get_redis):
        """
        用例标题: 一次性使用——第二次验证失败
        前置: mock Redis 第一次返回 'ABCD'，第二次返回 None（模拟已被删除）
        步骤: 连续两次调用 validate_captcha('test-token', 'ABCD')
        预期结果: 第一次返回 True，第二次返回 False
        """
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        mock_redis.get.side_effect = [b'ABCD', None]

        result1 = validate_captcha('test-token', 'ABCD')
        result2 = validate_captcha('test-token', 'ABCD')

        self.assertTrue(result1)
        self.assertFalse(result2)
