from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, F
from django.utils import timezone

from .models import AnalyticsEvent, RegistrationStats

MODULE_LABELS = {
    'home': '首页',
    'auth': '认证',
    'ai_generation': 'AI用例生成',
    'app_automation': 'APP自动化测试',
    'assistant': 'AI评测师',
    'data_factory': '数据工厂',
    'configuration': '配置中心',
    'other': '其他',
    'unknown': '未识别',
}

HOME_CARD_LABELS = {
    'ai': 'AI用例生成',
    'app': 'APP自动化测试',
    'assistant': 'AI评测师',
    'config': '配置中心',
    'data': '数据工厂',
}

PAGE_LABELS = {
    '/home': '首页',
    '/login': '登录页',
    '/register': '注册页',
    '/ai-generation/assistant': 'AI评测师',
    '/ai-generation/requirement-analysis': '需求分析',
    '/app-automation/dashboard': 'APP自动化仪表盘',
    '/data-factory': '数据工厂',
    '/configuration/ai-model': 'AI模型配置',
}

PAGE_PREFIX_LABELS = [
    ('/ai-generation/requirement-analysis', '需求分析'),
    ('/ai-generation/projects', 'AI项目管理'),
    ('/ai-generation/testcases', '测试用例管理'),
    ('/ai-generation/testsuites', '测试套件管理'),
    ('/ai-generation/executions', '测试执行'),
    ('/ai-generation/reports', '测试报告'),
    ('/ai-generation/reviews', '用例评审'),
    ('/app-automation/', 'APP自动化测试'),
    ('/configuration/', '配置中心'),
]

REGISTRATION_FIELD_LABELS = [
    ('username', '用户名'),
    ('phone', '手机号'),
    ('email', '邮箱'),
    ('first_name', '名'),
    ('last_name', '姓'),
    ('department', '部门'),
    ('position', '职位'),
]


def get_day_bounds(target_date):
    start_at = timezone.make_aware(datetime.combine(target_date, time.min))
    end_at = timezone.make_aware(datetime.combine(target_date + timedelta(days=1), time.min))
    return start_at, end_at


def count_for_day(queryset, target_date):
    start_at, end_at = get_day_bounds(target_date)
    return queryset.filter(created_at__gte=start_at, created_at__lt=end_at).count()


def count_since_day(queryset, start_date):
    start_at, _ = get_day_bounds(start_date)
    return queryset.filter(created_at__gte=start_at).count()


def build_daily_trend(queryset, days=7):
    start_date = timezone.localdate() - timedelta(days=days - 1)
    start_at, _ = get_day_bounds(start_date)
    counts_by_day = {}

    for created_at in queryset.filter(created_at__gte=start_at).values_list('created_at', flat=True):
        local_day = timezone.localtime(created_at).date()
        counts_by_day[local_day] = counts_by_day.get(local_day, 0) + 1

    return [
        {
            'day': start_date + timedelta(days=offset),
            'total': counts_by_day.get(start_date + timedelta(days=offset), 0),
        }
        for offset in range(days)
    ]


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(admin.ModelAdmin):
    change_list_template = 'admin/analytics/analyticsevent/change_list.html'
    list_display = ('event_name', 'event_type', 'module', 'user', 'success', 'created_at')
    list_filter = ('event_type', 'module', 'success', 'created_at')
    search_fields = ('event_name', 'page_path', 'route_name', 'session_id', 'user__username')
    readonly_fields = (
        'event_name',
        'event_type',
        'module',
        'page_path',
        'route_name',
        'referrer_path',
        'target_path',
        'success',
        'duration_ms',
        'session_id',
        'metadata',
        'ip_address',
        'user_agent',
        'user',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

    @staticmethod
    def get_module_label(module_code):
        return MODULE_LABELS.get(module_code or 'unknown', module_code or '未识别')

    @staticmethod
    def get_home_card_label(card_code):
        return HOME_CARD_LABELS.get(card_code or '', card_code or '未识别')

    @staticmethod
    def get_page_label(page_path):
        normalized_path = (page_path or '').split('?', 1)[0]
        if normalized_path in PAGE_LABELS:
            return PAGE_LABELS[normalized_path]

        for prefix, label in PAGE_PREFIX_LABELS:
            if normalized_path.startswith(prefix):
                return label

        return normalized_path or '未识别'

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context=extra_context)
        if not hasattr(response, 'context_data') or 'cl' not in response.context_data:
            return response

        queryset = response.context_data['cl'].queryset
        today = timezone.localdate()
        seven_days_ago = today - timedelta(days=6)
        page_view_queryset = queryset.filter(event_name='page_view')
        home_card_click_queryset = queryset.filter(
            event_name='module_card_click',
            page_path='/home',
        )
        login_success_queryset = queryset.filter(event_name='login_success')

        summary = queryset.aggregate(
            total_events=Count('id'),
            active_users=Count('user', distinct=True),
            avg_duration=Avg('duration_ms'),
        )

        response.context_data['analytics_summary'] = {
            'total_events': summary['total_events'] or 0,
            'today_events': count_for_day(queryset, today),
            'seven_day_events': count_since_day(queryset, seven_days_ago),
            'page_view_events': queryset.filter(event_name='page_view').count(),
            'home_card_click_events': home_card_click_queryset.count(),
            'login_success_events': login_success_queryset.count(),
            'success_events': queryset.filter(success=True).count(),
            'failed_events': queryset.filter(success=False).count(),
            'active_users': summary['active_users'] or 0,
            'avg_duration': round(summary['avg_duration'] or 0, 2),
        }
        module_ranking = list(
            page_view_queryset.exclude(module='')
            .values('module')
            .annotate(total=Count('id'))
            .order_by('-total', 'module')[:10]
        )
        response.context_data['analytics_module_ranking'] = [
            {
                **item,
                'module_label': self.get_module_label(item['module']),
            }
            for item in module_ranking
        ]

        page_ranking = list(
            page_view_queryset.exclude(page_path='')
            .values('page_path')
            .annotate(total=Count('id'))
            .order_by('-total', 'page_path')[:10]
        )
        response.context_data['analytics_page_ranking'] = [
            {
                **item,
                'page_label': self.get_page_label(item['page_path']),
            }
            for item in page_ranking
        ]

        home_card_ranking = list(
            home_card_click_queryset.exclude(metadata__card_type='')
            .annotate(card_type=F('metadata__card_type'))
            .values('card_type')
            .annotate(total=Count('id'))
            .order_by('-total', 'card_type')[:10]
        )
        response.context_data['analytics_home_card_ranking'] = [
            {
                **item,
                'card_label': self.get_home_card_label(item['card_type']),
            }
            for item in home_card_ranking
        ]
        response.context_data['analytics_daily_trend'] = build_daily_trend(queryset)
        return response


@admin.register(RegistrationStats)
class RegistrationStatsAdmin(admin.ModelAdmin):
    change_list_template = 'admin/users/registrationstatsproxy/change_list.html'
    list_display = ['username', 'phone', 'email', 'department', 'position', 'created_at', 'is_active']
    list_display_links = None
    list_filter = ['is_active', 'department', 'position']
    search_fields = ['username', 'phone', 'email', 'first_name', 'last_name', 'department', 'position']
    ordering = ['-created_at']
    list_per_page = 20

    def _is_allowed(self, request):
        if not settings.REGISTRATION_STATS_ENABLED:
            return False

        visible_usernames = set(settings.REGISTRATION_STATS_VISIBLE_USERNAMES or [])
        if visible_usernames:
            return request.user.is_authenticated and request.user.username in visible_usernames
        return request.user.is_authenticated and request.user.is_superuser

    def get_queryset(self, request):
        return super().get_queryset(request).order_by('-created_at')

    def has_module_permission(self, request):
        return self._is_allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_model_perms(self, request):
        if not self._is_allowed(request):
            return {}
        return {'view': True}

    @staticmethod
    def _filled_count(queryset, field_name):
        return queryset.exclude(**{f'{field_name}__isnull': True}).exclude(**{field_name: ''}).count()

    def changelist_view(self, request, extra_context=None):
        if not self._is_allowed(request):
            raise PermissionDenied

        response = super().changelist_view(request, extra_context=extra_context)
        if not hasattr(response, 'context_data') or 'cl' not in response.context_data:
            return response

        queryset = response.context_data['cl'].queryset
        today = timezone.localdate()
        seven_days_ago = today - timedelta(days=6)
        total_users = queryset.count()

        filled_stats = []
        for field_name, label in REGISTRATION_FIELD_LABELS:
            filled_count = self._filled_count(queryset, field_name)
            fill_rate = round((filled_count / total_users * 100), 2) if total_users else 0
            filled_stats.append({
                'field_name': field_name,
                'label': label,
                'filled_count': filled_count,
                'fill_rate': fill_rate,
            })

        response.context_data['registration_summary'] = {
            'today_registrations': count_for_day(queryset, today),
            'seven_day_registrations': count_since_day(queryset, seven_days_ago),
            'total_registrations': total_users,
            'phone_fill_rate': next(item['fill_rate'] for item in filled_stats if item['field_name'] == 'phone'),
            'department_fill_rate': next(item['fill_rate'] for item in filled_stats if item['field_name'] == 'department'),
            'position_fill_rate': next(item['fill_rate'] for item in filled_stats if item['field_name'] == 'position'),
            'email_fill_rate': next(item['fill_rate'] for item in filled_stats if item['field_name'] == 'email'),
        }
        response.context_data['registration_field_stats'] = filled_stats
        response.context_data['registration_department_ranking'] = list(
            queryset.exclude(department__isnull=True).exclude(department='')
            .values('department')
            .annotate(total=Count('id'))
            .order_by('-total', 'department')[:10]
        )
        response.context_data['registration_position_ranking'] = list(
            queryset.exclude(position__isnull=True).exclude(position='')
            .values('position')
            .annotate(total=Count('id'))
            .order_by('-total', 'position')[:10]
        )
        response.context_data['registration_daily_trend'] = build_daily_trend(queryset)
        response.context_data['recent_registrations'] = list(
            queryset.values(
                'username',
                'phone',
                'email',
                'first_name',
                'last_name',
                'department',
                'position',
                'created_at',
            )[:10]
        )
        return response
