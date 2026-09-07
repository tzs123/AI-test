from django.db import migrations


TARGET = {'type': 'object'}
TARGET_LIST = {'type': 'array'}


def component(component_type, name, category, description, properties,
              defaults, required=None, sort_order=0):
    return {
        'type': component_type,
        'name': name,
        'category': category,
        'description': description,
        'schema': {
            'required': list(required or []),
            'properties': properties,
        },
        'default_config': defaults,
        'enabled': True,
        'sort_order': sort_order,
    }


COMMON_RESULT_PROPERTIES = {
    'timeout': {'type': 'number'},
    'action_interval': {'type': 'number'},
    'save_as': {'type': 'string'},
    'scope': {'type': 'string', 'enum': ['local', 'global']},
}


COMPONENTS = [
    component(
        'login', '登录', 'page_interaction',
        '支持密码、验证码和生物识别登录，并可等待成功元素或页面',
        {
            'mode': {'type': 'string', 'enum': ['password', 'verification_code', 'biometric']},
            'account': {'type': 'string'},
            'account_kind': {'type': 'string', 'enum': ['phone', 'text']},
            'password': {'type': 'string'},
            'verification_code': {'type': 'string'},
            'code_wait': {'type': 'number'},
            'account_target': TARGET,
            'password_target': TARGET,
            'get_code_target': TARGET,
            'verification_code_target': TARGET,
            'biometric_target': TARGET,
            'submit_target': TARGET,
            'success_target': TARGET,
            'success_page': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'mode': 'password', 'account': '', 'account_kind': 'phone',
            'password': '', 'verification_code': '', 'code_wait': 0,
            'account_target': {}, 'password_target': {}, 'get_code_target': {},
            'verification_code_target': {}, 'biometric_target': {},
            'submit_target': {}, 'success_target': {}, 'success_page': {},
            'timeout': 8, 'action_interval': 0.2,
            'save_as': 'login_result', 'scope': 'local',
        },
        required=['mode'], sort_order=10,
    ),
    component(
        'fill_form', '填写表单', 'page_interaction',
        '批量填写手机号、验证码、姓名、证件、地址或选择类字段',
        {
            'fields': {'type': 'array'},
            'field_interval': {'type': 'number'},
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'fields': [], 'field_interval': 0.2, 'timeout': 5,
            'action_interval': 0.2, 'save_as': 'fill_form_result', 'scope': 'local',
        },
        required=['fields'], sort_order=20,
    ),
    component(
        'close_popup', '关闭弹窗', 'page_interaction',
        '按优先级探测关闭按钮、确认按钮或遮罩目标，命中首个后关闭',
        {
            'targets': TARGET_LIST,
            'probe_timeout': {'type': 'number'},
            'required': {'type': 'boolean'},
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'targets': [], 'probe_timeout': 0.8, 'required': False,
            'timeout': 1, 'action_interval': 0.1,
            'save_as': 'close_popup_result', 'scope': 'local',
        },
        required=['targets'], sort_order=30,
    ),
    component(
        'scroll_list', '滚动列表', 'page_interaction',
        '按方向、距离和次数滚动列表，适用于分页和无限加载',
        {
            'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right']},
            'count': {'type': 'number'},
            'distance': {'type': 'number'},
            'duration': {'type': 'number'},
            'interval': {'type': 'number'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
        {
            'direction': 'up', 'count': 3, 'distance': 0.55,
            'duration': 0.5, 'interval': 0.5,
            'save_as': 'scroll_list_result', 'scope': 'local',
        },
        sort_order=40,
    ),
    component(
        'search_product', '搜索商品', 'ecommerce',
        '输入商品关键词、触发搜索并等待结果区域',
        {
            'keyword': {'type': 'string'},
            'search_target': TARGET,
            'search_button_target': TARGET,
            'result_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'keyword': '', 'search_target': {}, 'search_button_target': {},
            'result_target': {}, 'timeout': 8, 'action_interval': 0.2,
            'save_as': 'search_product_result', 'scope': 'local',
        },
        required=['keyword', 'search_target'], sort_order=10,
    ),
    component(
        'browse_product', '浏览商品', 'ecommerce',
        '打开指定商品或滚动商品列表，并按配置停留',
        {
            'product_id': {'type': 'string'},
            'product_target': TARGET,
            'scroll_count': {'type': 'number'},
            'direction': {'type': 'string', 'enum': ['up', 'down']},
            'distance': {'type': 'number'},
            'duration': {'type': 'number'},
            'interval': {'type': 'number'},
            'dwell_time': {'type': 'number'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
        {
            'product_id': '', 'product_target': {}, 'scroll_count': 2,
            'direction': 'up', 'distance': 0.55, 'duration': 0.5,
            'interval': 0.5, 'dwell_time': 3,
            'save_as': 'browse_product_result', 'scope': 'local',
        },
        sort_order=20,
    ),
    component(
        'open_product_detail', '打开商品详情', 'ecommerce',
        '点击商品并探测详情页标识',
        {
            'product_target': TARGET,
            'detail_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'product_target': {}, 'detail_target': {}, 'timeout': 8,
            'action_interval': 0.2, 'save_as': 'product_detail_result', 'scope': 'local',
        },
        required=['product_target'], sort_order=30,
    ),
    component(
        'add_cart', '加入购物车', 'ecommerce',
        '选择规格、点击加入购物车并探测成功提示',
        {
            'spec_targets': TARGET_LIST,
            'add_cart_target': TARGET,
            'confirmation_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'spec_targets': [], 'add_cart_target': {}, 'confirmation_target': {},
            'timeout': 8, 'action_interval': 0.2,
            'save_as': 'add_cart_result', 'scope': 'local',
        },
        required=['add_cart_target'], sort_order=40,
    ),
    component(
        'create_order', '创建订单', 'ecommerce',
        '选择商品和规格、填写地址并提交订单，不自动执行真实支付',
        {
            'product_target': TARGET,
            'spec_targets': TARGET_LIST,
            'buy_target': TARGET,
            'address_fields': {'type': 'array'},
            'submit_order_target': TARGET,
            'order_success_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'product_target': {}, 'spec_targets': [], 'buy_target': {},
            'address_fields': [], 'submit_order_target': {}, 'order_success_target': {},
            'timeout': 10, 'action_interval': 0.2,
            'save_as': 'create_order_result', 'scope': 'local',
        },
        required=['buy_target', 'submit_order_target'], sort_order=50,
    ),
    component(
        'like_video', '点赞视频', 'short_video', '智能定位并点赞当前视频',
        {'target': TARGET, **COMMON_RESULT_PROPERTIES},
        {'target': {}, 'timeout': 5, 'action_interval': 0.2, 'save_as': '', 'scope': 'local'},
        required=['target'], sort_order=50,
    ),
    component(
        'favorite_video', '收藏视频', 'short_video', '智能定位并收藏当前视频',
        {'target': TARGET, **COMMON_RESULT_PROPERTIES},
        {'target': {}, 'timeout': 5, 'action_interval': 0.2, 'save_as': '', 'scope': 'local'},
        required=['target'], sort_order=60,
    ),
    component(
        'comment_video', '评论视频', 'short_video', '打开评论区、输入评论并发布',
        {
            'comment': {'type': 'string'},
            'comment_entry_target': TARGET,
            'comment_input_target': TARGET,
            'submit_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'comment': '', 'comment_entry_target': {}, 'comment_input_target': {},
            'submit_target': {}, 'timeout': 5, 'action_interval': 0.2,
            'save_as': '', 'scope': 'local',
        },
        required=['comment', 'comment_input_target', 'submit_target'], sort_order=70,
    ),
    component(
        'follow_user', '关注用户', 'short_video', '智能定位并关注当前作者',
        {'target': TARGET, **COMMON_RESULT_PROPERTIES},
        {'target': {}, 'timeout': 5, 'action_interval': 0.2, 'save_as': '', 'scope': 'local'},
        required=['target'], sort_order=80,
    ),
    component(
        'live_room', '直播间', 'short_video', '进入直播间、停留、点赞并可发送弹幕',
        {
            'room_target': TARGET,
            'dwell_time': {'type': 'number'},
            'like_count': {'type': 'number'},
            'like_target': TARGET,
            'like_interval': {'type': 'number'},
            'danmaku': {'type': 'string'},
            'danmaku_input_target': TARGET,
            'danmaku_submit_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'room_target': {}, 'dwell_time': 5, 'like_count': 0,
            'like_target': {}, 'like_interval': 0.15, 'danmaku': '',
            'danmaku_input_target': {}, 'danmaku_submit_target': {},
            'timeout': 5, 'action_interval': 0.2,
            'save_as': 'live_room_result', 'scope': 'local',
        },
        sort_order=90,
    ),
]


def add_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for definition in COMPONENTS:
        defaults = dict(definition)
        component_type = defaults.pop('type')
        AppComponent.objects.update_or_create(type=component_type, defaults=defaults)

    swipe_component = AppComponent.objects.filter(type='swipe').first()
    if swipe_component:
        schema = dict(swipe_component.schema or {})
        properties = dict(schema.get('properties') or {})
        properties['distance'] = {'type': 'number'}
        schema['properties'] = properties
        defaults = dict(swipe_component.default_config or {})
        defaults.setdefault('distance', 0.55)
        swipe_component.schema = schema
        swipe_component.default_config = defaults
        swipe_component.save(update_fields=['schema', 'default_config'])


def remove_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type__in=[item['type'] for item in COMPONENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0014_alter_apppackage_options'),
    ]

    operations = [
        migrations.RunPython(add_components, remove_components),
    ]
