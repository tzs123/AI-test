"""
埋点统计视图测试
覆盖 AnalyticsEventIngestView 及 get_client_ip 辅助函数
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User

# analytics app 默认未启用，需要通过 override_settings 动态添加
# 由于 Django 不支持运行时修改 INSTALLED_APPS，这里直接导入模型
# 测试运行时需确保 ANALYTICS_ENABLED=True 或 REGISTRATION_STATS_ENABLED=True
try:
    from apps.analytics.models import AnalyticsEvent
    ANALYTICS_AVAILABLE = True
except RuntimeError:
    ANALYTICS_AVAILABLE = False


EVENTS_URL = '/api/analytics/events/'


class GetClientIpTests(TestCase):
    """get_client_ip 辅助函数测试"""

    def test_x_forwarded_for_single_ip(self):
        """
        用例标题：X-Forwarded-For 单个 IP 时应正确提取
        前置：构造包含单个 IP 的 X-Forwarded-For 请求
        步骤：调用 get_client_ip 并传入该请求
        预期结果：返回 X-Forwarded-For 中的 IP 地址
        """
        from apps.analytics.views import get_client_ip

        request = type('Request', (), {
            'META': {'HTTP_X_FORWARDED_FOR': '203.0.113.50', 'REMOTE_ADDR': '10.0.0.1'}
        })()
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    def test_x_forwarded_for_multiple_ips(self):
        """
        用例标题：X-Forwarded-For 多个 IP 时应取第一个
        前置：构造包含多个 IP 的 X-Forwarded-For 请求
        步骤：调用 get_client_ip 并传入该请求
        预期结果：返回逗号分隔的第一个 IP 地址
        """
        from apps.analytics.views import get_client_ip

        request = type('Request', (), {
            'META': {'HTTP_X_FORWARDED_FOR': '203.0.113.50, 70.41.3.18, 150.172.238.13', 'REMOTE_ADDR': '10.0.0.1'}
        })()
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    def test_x_forwarded_for_with_spaces(self):
        """
        用例标题：X-Forwarded-For 含空格时应正确去除
        前置：构造 X-Forwarded-For 值含前后空格的请求
        步骤：调用 get_client_ip 并传入该请求
        预期结果：返回去除空格后的 IP 地址
        """
        from apps.analytics.views import get_client_ip

        request = type('Request', (), {
            'META': {'HTTP_X_FORWARDED_FOR': '  203.0.113.50  ', 'REMOTE_ADDR': '10.0.0.1'}
        })()
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    def test_no_x_forwarded_for_falls_back_to_remote_addr(self):
        """
        用例标题：无 X-Forwarded-For 时应回退到 REMOTE_ADDR
        前置：构造不含 X-Forwarded-For 头的请求
        步骤：调用 get_client_ip 并传入该请求
        预期结果：返回 REMOTE_ADDR 的值
        """
        from apps.analytics.views import get_client_ip

        request = type('Request', (), {
            'META': {'REMOTE_ADDR': '10.0.0.1'}
        })()
        self.assertEqual(get_client_ip(request), '10.0.0.1')

    def test_empty_x_forwarded_for_falls_back_to_remote_addr(self):
        """
        用例标题：X-Forwarded-For 为空字符串时应回退到 REMOTE_ADDR
        前置：构造 X-Forwarded-For 为空字符串的请求
        步骤：调用 get_client_ip 并传入该请求
        预期结果：返回 REMOTE_ADDR 的值
        """
        from apps.analytics.views import get_client_ip

        request = type('Request', (), {
            'META': {'HTTP_X_FORWARDED_FOR': '', 'REMOTE_ADDR': '10.0.0.1'}
        })()
        self.assertEqual(get_client_ip(request), '10.0.0.1')


class AnalyticsEventIngestViewPositiveTests(TestCase):
    """正向正常流程测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='analytics_user', password='testpass123')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def test_post_single_event_dict(self):
        """
        用例标题：单个事件 dict 格式上报应成功
        前置：已认证用户，准备单个事件 dict 数据
        步骤：POST /api/analytics/events/ 发送单个事件 dict
        预期结果：返回 201，created 为 1，数据库新增 1 条记录
        """
        payload = {'event_name': 'page_view', 'module': 'home'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['created'], 1)
        self.assertEqual(AnalyticsEvent.objects.count(), 1)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.event_name, 'page_view')
        self.assertEqual(event.module, 'home')

    def test_post_events_list(self):
        """
        用例标题：事件列表格式上报应成功
        前置：已认证用户，准备事件列表数据
        步骤：POST /api/analytics/events/ 发送事件列表
        预期结果：返回 201，created 为列表长度，数据库新增对应条数
        """
        payload = [
            {'event_name': 'page_view', 'module': 'home'},
            {'event_name': 'button_click', 'module': 'dashboard'},
        ]
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['created'], 2)
        self.assertEqual(AnalyticsEvent.objects.count(), 2)

    def test_post_events_field_with_list(self):
        """
        用例标题：events 字段包含列表格式上报应成功
        前置：已认证用户，准备 {"events": [...]} 格式数据
        步骤：POST /api/analytics/events/ 发送 events 字段包裹的列表
        预期结果：返回 201，created 为列表长度，数据库新增对应条数
        """
        payload = {
            'events': [
                {'event_name': 'api_call', 'module': 'backend'},
                {'event_name': 'api_error', 'module': 'backend'},
            ]
        }
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['created'], 2)
        self.assertEqual(AnalyticsEvent.objects.count(), 2)

    def test_post_event_with_all_optional_fields(self):
        """
        用例标题：包含所有可选字段的事件上报应成功
        前置：已认证用户，准备含全部字段的事件数据
        步骤：POST /api/analytics/events/ 发送完整字段事件
        预期结果：返回 201，数据库记录各字段值正确
        """
        payload = {
            'event_name': 'page_load',
            'event_type': 'performance',
            'module': 'report',
            'page_path': '/report/detail',
            'route_name': 'ReportDetail',
            'referrer_path': '/report/list',
            'target_path': '/report/detail/123',
            'success': True,
            'duration_ms': 350,
            'session_id': 'sess_abc123',
            'metadata': {'browser': 'Chrome', 'version': '120'},
        }
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.event_type, 'performance')
        self.assertEqual(event.page_path, '/report/detail')
        self.assertEqual(event.route_name, 'ReportDetail')
        self.assertEqual(event.referrer_path, '/report/list')
        self.assertEqual(event.target_path, '/report/detail/123')
        self.assertTrue(event.success)
        self.assertEqual(event.duration_ms, 350)
        self.assertEqual(event.session_id, 'sess_abc123')
        self.assertEqual(event.metadata, {'browser': 'Chrome', 'version': '120'})

    def test_post_event_defaults_applied(self):
        """
        用例标题：仅传必填字段时可选字段应使用默认值
        前置：已认证用户，仅传 event_name
        步骤：POST /api/analytics/events/ 发送仅含必填字段的事件
        预期结果：返回 201，数据库记录中可选字段为默认值
        """
        payload = {'event_name': 'simple_event'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.event_type, 'custom')
        self.assertEqual(event.module, '')
        self.assertEqual(event.page_path, '')
        self.assertIsNone(event.success)
        self.assertIsNone(event.duration_ms)
        self.assertEqual(event.session_id, '')
        self.assertEqual(event.metadata, {})

    def test_post_event_user_linked(self):
        """
        用例标题：已认证用户上报事件应关联用户
        前置：已认证用户
        步骤：POST /api/analytics/events/ 上报事件
        预期结果：数据库记录的 user 字段为当前认证用户
        """
        payload = {'event_name': 'user_action'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.user, self.user)

    def test_post_event_ip_address_captured(self):
        """
        用例标题：上报事件应记录客户端 IP 地址
        前置：已认证用户，请求携带 X-Forwarded-For 头
        步骤：POST /api/analytics/events/ 并设置 X-Forwarded-For 头
        预期结果：数据库记录的 ip_address 为 X-Forwarded-For 中的 IP
        """
        payload = {'event_name': 'ip_test'}
        response = self.client.post(
            EVENTS_URL, payload, format='json',
            HTTP_X_FORWARDED_FOR='198.51.100.23',
        )
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.ip_address, '198.51.100.23')

    def test_post_event_user_agent_captured(self):
        """
        用例标题：上报事件应记录 User-Agent
        前置：已认证用户，请求携带 User-Agent 头
        步骤：POST /api/analytics/events/ 并设置 User-Agent 头
        预期结果：数据库记录的 user_agent 为请求中的 User-Agent 值
        """
        payload = {'event_name': 'ua_test'}
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        response = self.client.post(EVENTS_URL, payload, format='json', HTTP_USER_AGENT=ua)
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.user_agent, ua)


class AnalyticsEventIngestViewNegativeTests(TestCase):
    """反向异常入参测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='neg_user', password='testpass123')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def test_post_string_payload_returns_400(self):
        """
        用例标题：请求体为字符串应返回 400
        前置：已认证用户，准备字符串请求体
        步骤：POST /api/analytics/events/ 发送字符串
        预期结果：返回 400，detail 为"无效的埋点数据格式"
        """
        response = self.client.post(EVENTS_URL, 'invalid_string', format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '无效的埋点数据格式')

    def test_post_number_payload_returns_400(self):
        """
        用例标题：请求体为数字应返回 400
        前置：已认证用户，准备数字请求体
        步骤：POST /api/analytics/events/ 发送数字
        预期结果：返回 400，detail 为"无效的埋点数据格式"
        """
        response = self.client.post(EVENTS_URL, 12345, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '无效的埋点数据格式')

    def test_post_empty_list_returns_400(self):
        """
        用例标题：空事件列表应返回 400
        前置：已认证用户，准备空列表请求体
        步骤：POST /api/analytics/events/ 发送空列表 []
        预期结果：返回 400，detail 为"埋点数据不能为空"
        """
        response = self.client.post(EVENTS_URL, [], format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '埋点数据不能为空')

    def test_post_events_field_empty_list_returns_400(self):
        """
        用例标题：events 字段为空列表应返回 400
        前置：已认证用户，准备 {"events": []} 格式请求体
        步骤：POST /api/analytics/events/ 发送 events 为空列表
        预期结果：返回 400，detail 为"埋点数据不能为空"
        """
        response = self.client.post(EVENTS_URL, {'events': []}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '埋点数据不能为空')

    def test_post_unauthenticated_returns_401(self):
        """
        用例标题：未认证用户应返回 401
        前置：不携带任何认证信息
        步骤：POST /api/analytics/events/ 发送有效事件数据
        预期结果：返回 401
        """
        client = APIClient()
        payload = {'event_name': 'page_view'}
        response = client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 401)


class AnalyticsEventIngestViewBoundaryTests(TestCase):
    """边界极值测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='boundary_user', password='testpass123')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    @override_settings(ANALYTICS_MAX_BATCH_SIZE=20)
    def test_post_exactly_max_batch_size_succeeds(self):
        """
        用例标题：上报数量恰好等于最大批量限制应成功
        前置：已认证用户，ANALYTICS_MAX_BATCH_SIZE=20，准备 20 条事件
        步骤：POST /api/analytics/events/ 发送 20 条事件
        预期结果：返回 201，created 为 20
        """
        events = [{'event_name': f'event_{i}'} for i in range(20)]
        response = self.client.post(EVENTS_URL, events, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['created'], 20)

    @override_settings(ANALYTICS_MAX_BATCH_SIZE=20)
    def test_post_exceeds_max_batch_size_returns_400(self):
        """
        用例标题：上报数量超过最大批量限制应返回 400
        前置：已认证用户，ANALYTICS_MAX_BATCH_SIZE=20，准备 21 条事件
        步骤：POST /api/analytics/events/ 发送 21 条事件
        预期结果：返回 400，detail 包含批量限制提示
        """
        events = [{'event_name': f'event_{i}'} for i in range(21)]
        response = self.client.post(EVENTS_URL, events, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('单次最多允许上报', response.data['detail'])
        self.assertIn('20', response.data['detail'])

    @override_settings(ANALYTICS_MAX_BATCH_SIZE=1)
    def test_post_single_event_with_batch_size_1_succeeds(self):
        """
        用例标题：批量限制为 1 时上报单条事件应成功
        前置：已认证用户，ANALYTICS_MAX_BATCH_SIZE=1，准备 1 条事件
        步骤：POST /api/analytics/events/ 发送 1 条事件
        预期结果：返回 201，created 为 1
        """
        payload = {'event_name': 'single_event'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['created'], 1)

    @override_settings(ANALYTICS_MAX_BATCH_SIZE=1)
    def test_post_two_events_with_batch_size_1_returns_400(self):
        """
        用例标题：批量限制为 1 时上报 2 条事件应返回 400
        前置：已认证用户，ANALYTICS_MAX_BATCH_SIZE=1，准备 2 条事件
        步骤：POST /api/analytics/events/ 发送 2 条事件
        预期结果：返回 400，detail 包含批量限制提示
        """
        events = [{'event_name': 'event_1'}, {'event_name': 'event_2'}]
        response = self.client.post(EVENTS_URL, events, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('单次最多允许上报', response.data['detail'])

    def test_post_event_name_at_max_length(self):
        """
        用例标题：event_name 达到最大长度 100 应成功
        前置：已认证用户，准备 event_name 为 100 字符的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 201
        """
        name = 'a' * 100
        payload = {'event_name': name}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(len(event.event_name), 100)

    def test_post_duration_ms_zero_accepted(self):
        """
        用例标题：duration_ms 为 0 应被接受
        前置：已认证用户，准备 duration_ms=0 的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 201，数据库记录 duration_ms 为 0
        """
        payload = {'event_name': 'instant_event', 'duration_ms': 0}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(event.duration_ms, 0)

    def test_post_user_agent_truncated_at_512(self):
        """
        用例标题：User-Agent 超过 512 字符应被截断
        前置：已认证用户，准备超长 User-Agent 头
        步骤：POST /api/analytics/events/ 并设置 600 字符的 User-Agent
        预期结果：返回 201，数据库记录的 user_agent 长度不超过 512
        """
        long_ua = 'A' * 600
        payload = {'event_name': 'long_ua_event'}
        response = self.client.post(EVENTS_URL, payload, format='json', HTTP_USER_AGENT=long_ua)
        self.assertEqual(response.status_code, 201)
        event = AnalyticsEvent.objects.first()
        self.assertEqual(len(event.user_agent), 512)


class AnalyticsEventIngestViewIllegalTests(TestCase):
    """非法参数测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='illegal_user', password='testpass123')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def test_post_missing_event_name_returns_400(self):
        """
        用例标题：缺少必填字段 event_name 应返回 400
        前置：已认证用户，准备不含 event_name 的事件数据
        步骤：POST /api/analytics/events/ 发送缺少 event_name 的事件
        预期结果：返回 400
        """
        payload = {'module': 'home', 'event_type': 'custom'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_event_name_exceeds_max_length_returns_400(self):
        """
        用例标题：event_name 超过最大长度 100 应返回 400
        前置：已认证用户，准备 event_name 为 101 字符的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 400
        """
        payload = {'event_name': 'a' * 101}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_event_type_exceeds_max_length_returns_400(self):
        """
        用例标题：event_type 超过最大长度 32 应返回 400
        前置：已认证用户，准备 event_type 为 33 字符的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 400
        """
        payload = {'event_name': 'test', 'event_type': 'b' * 33}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_negative_duration_ms_returns_400(self):
        """
        用例标题：duration_ms 为负数应返回 400
        前置：已认证用户，准备 duration_ms=-1 的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 400
        """
        payload = {'event_name': 'neg_duration', 'duration_ms': -1}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_invalid_event_name_type_returns_400(self):
        """
        用例标题：event_name 为非字符串类型时序列化器应处理
        前置：已认证用户，准备 event_name 为数字的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：DRF CharField 会自动将数字转为字符串，因此返回 201
        """
        payload = {'event_name': 12345}
        response = self.client.post(EVENTS_URL, payload, format='json')
        # DRF CharField 会自动将数字类型转为字符串，所以请求会成功
        self.assertEqual(response.status_code, 201)

    def test_post_invalid_metadata_type_returns_400(self):
        """
        用例标题：metadata 为非 dict 类型应返回 400
        前置：已认证用户，准备 metadata 为字符串的事件
        步骤：POST /api/analytics/events/ 发送该事件
        预期结果：返回 400
        """
        payload = {'event_name': 'bad_meta', 'metadata': 'not_a_dict'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_boolean_payload_returns_400(self):
        """
        用例标题：请求体为布尔值应返回 400
        前置：已认证用户，准备布尔值请求体
        步骤：POST /api/analytics/events/ 发送 true
        预期结果：返回 400，detail 为"无效的埋点数据格式"
        """
        response = self.client.post(EVENTS_URL, True, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '无效的埋点数据格式')

    def test_post_null_payload_returns_400(self):
        """
        用例标题：请求体为 null 应返回 400
        前置：已认证用户，准备 null 请求体
        步骤：POST /api/analytics/events/ 发送 null
        预期结果：返回 400
        """
        response = self.client.post(EVENTS_URL, None, format='json')
        self.assertEqual(response.status_code, 400)

    def test_post_list_with_missing_event_name_returns_400(self):
        """
        用例标题：事件列表中某条缺少 event_name 应返回 400
        前置：已认证用户，准备列表中第二条缺少 event_name
        步骤：POST /api/analytics/events/ 发送该列表
        预期结果：返回 400，数据库无新增记录
        """
        events = [
            {'event_name': 'valid_event'},
            {'module': 'missing_name'},
        ]
        response = self.client.post(EVENTS_URL, events, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)


class AnalyticsEventIngestViewBulkCreateFailureTests(TestCase):
    """bulk_create 异常场景测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='bulk_fail_user', password='testpass123')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    @patch('apps.analytics.views.AnalyticsEvent.objects.bulk_create', side_effect=Exception('DB error'))
    def test_bulk_create_exception_returns_500(self, mock_bulk_create):
        """
        用例标题：bulk_create 抛出异常应返回 500
        前置：已认证用户，mock bulk_create 抛出异常
        步骤：POST /api/analytics/events/ 发送有效事件数据
        预期结果：返回 500，detail 为"写入埋点事件失败"
        """
        payload = {'event_name': 'db_fail_event'}
        response = self.client.post(EVENTS_URL, payload, format='json')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data['detail'], '写入埋点事件失败')
