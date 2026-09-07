from django.db import migrations


SELECTOR_TYPES = ['id', 'xpath', 'text', 'ocr', 'image', 'pos', 'region']


def selector_properties():
    return {
        'element_id': {'type': 'number'},
        'selector_type': {'type': 'string', 'enum': SELECTOR_TYPES},
        'selector': {'type': 'string'},
        'image_scope': {'type': 'string'},
        'image_threshold': {'type': 'number'},
        'semantic_min_score': {'type': 'number'},
        'ocr_min_confidence': {'type': 'number'},
        'timeout': {'type': 'number'},
        'context_preference': {'type': 'string', 'enum': ['auto', 'native', 'webview']},
        'webview_context': {'type': 'string'},
        'webview_css': {'type': 'string'},
        'webview_xpath': {'type': 'string'},
        'webview_text': {'type': 'string'},
    }


def selector_defaults(selector_type='text'):
    return {
        'selector_type': selector_type,
        'selector': '',
        'image_scope': 'common',
        'image_threshold': 0.85,
        'semantic_min_score': 36,
        'ocr_min_confidence': 0.5,
        'timeout': 5,
        'context_preference': 'auto',
        'webview_context': '',
        'webview_css': '',
        'webview_xpath': '',
        'webview_text': '',
    }


def component(component_type, name, category, description, properties=None,
              defaults=None, required=None, sort_order=0):
    return {
        'type': component_type,
        'name': name,
        'category': category,
        'description': description,
        'schema': {
            'required': list(required or []),
            'properties': dict(properties or {}),
        },
        'default_config': dict(defaults or {}),
        'enabled': True,
        'sort_order': sort_order,
    }


APP_FIELD_PROPERTIES = {
    'package_name': {'type': 'string'},
    'activity': {'type': 'string'},
    'wait_after': {'type': 'number'},
}


COMPONENTS = [
    component(
        'launch_app', '启动应用', 'base_page', '启动指定包名的 Android/iOS 应用',
        APP_FIELD_PROPERTIES,
        {'package_name': '', 'activity': '', 'wait_after': 1},
        sort_order=10,
    ),
    component(
        'close_app', '关闭应用', 'base_page', '关闭指定应用',
        {'package_name': {'type': 'string'}}, {'package_name': ''}, sort_order=20,
    ),
    component(
        'restart_app', '重启应用', 'base_page', '关闭应用后按间隔重新启动',
        {**APP_FIELD_PROPERTIES, 'restart_interval': {'type': 'number'}},
        {'package_name': '', 'activity': '', 'restart_interval': 0.8, 'wait_after': 1},
        sort_order=30,
    ),
    component(
        'clear_data', '清理应用数据', 'base_page', '清理应用缓存与本地数据',
        {'package_name': {'type': 'string'}}, {'package_name': ''}, sort_order=40,
    ),
    component('back', '返回', 'base_page', '发送系统返回键',
              {'wait_after': {'type': 'number'}}, {'wait_after': 0.3}, sort_order=50),
    component('home', 'Home键', 'base_page', '发送系统 Home 键',
              {'wait_after': {'type': 'number'}}, {'wait_after': 0.3}, sort_order=60),
    component(
        'wait_page', '等待页面', 'base_assert', '等待 Native/WebView 页面或目标 Activity 出现',
        {
            'page_type': {'type': 'string', 'enum': ['any', 'native', 'webview']},
            'expected_package': {'type': 'string'},
            'expected_activity': {'type': 'string'},
            'match_mode': {'type': 'string', 'enum': ['contains', 'exact', 'regex']},
            'timeout': {'type': 'number'},
            'retry_interval': {'type': 'number'},
        },
        {
            'page_type': 'any', 'expected_package': '', 'expected_activity': '',
            'match_mode': 'contains', 'timeout': 10, 'retry_interval': 0.5,
        },
        sort_order=10,
    ),
    component(
        'wait_element', '等待元素', 'base_assert', '按智能定位链等待元素出现且不触发点击',
        {**selector_properties(), 'save_as': {'type': 'string'}, 'scope': {'type': 'string'}},
        {**selector_defaults(), 'save_as': 'wait_element_result', 'scope': 'local'},
        required=['selector_type', 'selector'], sort_order=20,
    ),
    component(
        'assert_exists', '元素存在断言', 'base_assert', '断言元素存在或不存在',
        {**selector_properties(), 'expected_exists': {'type': 'boolean'}},
        {**selector_defaults(), 'expected_exists': True},
        required=['selector_type', 'selector'], sort_order=30,
    ),
    component(
        'assert_text', '文本断言', 'base_assert', 'OCR 识别区域文本并校验',
        {
            'selector_type': {'type': 'string', 'enum': ['region']},
            'selector': {'type': 'string'},
            'expected': {'type': 'string'},
            'match_mode': {'type': 'string', 'enum': ['contains', 'exact', 'regex']},
            'timeout': {'type': 'number'},
            'retry_interval': {'type': 'number'},
        },
        {
            'selector_type': 'region', 'selector': '', 'expected': '',
            'match_mode': 'contains', 'timeout': 5, 'retry_interval': 0.5,
        },
        required=['selector', 'expected'], sort_order=40,
    ),
    component(
        'assert_image', '图片断言', 'base_assert', '断言目标图片在当前页面中可见',
        {
            'expected': {'type': 'string'},
            'expected_image_scope': {'type': 'string'},
            'image_threshold': {'type': 'number'},
            'timeout': {'type': 'number'},
            'retry_interval': {'type': 'number'},
        },
        {
            'expected': '', 'expected_image_scope': 'common', 'image_threshold': 0.85,
            'timeout': 5, 'retry_interval': 0.5,
        },
        required=['expected'], sort_order=50,
    ),
    component(
        'assert_page', '页面断言', 'base_assert', '校验页面类型、包名或 Activity',
        {
            'page_type': {'type': 'string', 'enum': ['any', 'native', 'webview']},
            'expected_package': {'type': 'string'},
            'expected_activity': {'type': 'string'},
            'match_mode': {'type': 'string', 'enum': ['contains', 'exact', 'regex']},
        },
        {'page_type': 'any', 'expected_package': '', 'expected_activity': '', 'match_mode': 'contains'},
        sort_order=60,
    ),
    component(
        'smart_click', '智能点击', 'ai', 'WebView DOM、ID/XPath/文本、OCR、图片、坐标自动降级点击',
        selector_properties(), selector_defaults(),
        required=['selector_type', 'selector'], sort_order=10,
    ),
    component(
        'smart_input', '智能输入', 'ai', '智能定位输入框并按手机号、验证码、金额、银行卡等类型标准化输入',
        {
            **selector_properties(),
            'value': {'type': 'string'},
            'input_kind': {
                'type': 'string',
                'enum': ['auto', 'text', 'phone', 'password', 'verification_code', 'money', 'bank_card', 'license_plate'],
            },
            'clear_first': {'type': 'boolean'},
            'send_enter': {'type': 'boolean'},
        },
        {**selector_defaults(), 'value': '', 'input_kind': 'auto', 'clear_first': True, 'send_enter': False},
        required=['selector_type', 'selector', 'value'], sort_order=20,
    ),
    component(
        'element_probe', '元素探测', 'ai', '不执行点击，仅验证元素定位链并输出命中策略',
        {**selector_properties(), 'save_as': {'type': 'string'}, 'scope': {'type': 'string'}},
        {**selector_defaults(), 'save_as': 'element_probe_result', 'scope': 'local'},
        required=['selector_type', 'selector'], sort_order=30,
    ),
    component(
        'page_detect', '页面识别', 'ai', '识别当前 Native/WebView 类型、包名和 Activity 并保存结果',
        {'save_as': {'type': 'string'}, 'scope': {'type': 'string'}},
        {'save_as': 'page_detect_result', 'scope': 'local'}, sort_order=40,
    ),
    component('switch_native', '切换 Native', 'h5', '将 Appium 上下文切换到 NATIVE_APP',
              {}, {}, sort_order=10),
    component(
        'switch_webview', '切换 H5', 'h5', '查找并切换到指定或首个 WebView 上下文',
        {'webview_context': {'type': 'string'}, 'timeout': {'type': 'number'}},
        {'webview_context': '', 'timeout': 5}, sort_order=20,
    ),
    component(
        'click_web_element', '点击 H5 元素', 'h5', '通过 CSS、XPath 或文本点击 WebView DOM 元素',
        {
            'webview_css': {'type': 'string'}, 'webview_xpath': {'type': 'string'},
            'webview_text': {'type': 'string'}, 'webview_context': {'type': 'string'},
            'timeout': {'type': 'number'},
        },
        {'webview_css': '', 'webview_xpath': '', 'webview_text': '', 'webview_context': '', 'timeout': 5},
        sort_order=30,
    ),
    component(
        'input_web_text', '输入 H5 文本', 'h5', '通过 WebView DOM 定位输入框并输入文本',
        {
            'webview_css': {'type': 'string'}, 'webview_xpath': {'type': 'string'},
            'webview_text': {'type': 'string'}, 'webview_context': {'type': 'string'},
            'value': {'type': 'string'}, 'clear_first': {'type': 'boolean'},
            'send_enter': {'type': 'boolean'}, 'timeout': {'type': 'number'},
        },
        {
            'webview_css': '', 'webview_xpath': '', 'webview_text': '', 'webview_context': '',
            'value': '', 'clear_first': True, 'send_enter': False, 'timeout': 5,
        },
        required=['value'], sort_order=40,
    ),
    component(
        'execute_js', '执行 JavaScript', 'h5', '在当前 WebView 中执行同步 JavaScript',
        {
            'script': {'type': 'string'}, 'args': {'type': 'array'},
            'webview_context': {'type': 'string'}, 'timeout': {'type': 'number'},
            'save_as': {'type': 'string'}, 'scope': {'type': 'string'},
        },
        {'script': '', 'args': [], 'webview_context': '', 'timeout': 5, 'save_as': '', 'scope': 'local'},
        required=['script'], sort_order=50,
    ),
    component(
        'scroll_web_page', '滚动 H5 页面', 'h5', '按方向和视口比例滚动 WebView 页面',
        {
            'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right']},
            'distance': {'type': 'number'}, 'webview_context': {'type': 'string'},
            'timeout': {'type': 'number'},
        },
        {'direction': 'down', 'distance': 0.8, 'webview_context': '', 'timeout': 5},
        sort_order=60,
    ),
    component(
        'pull_refresh', '下拉刷新', 'base_element', '按屏幕比例执行下拉刷新并等待页面加载',
        {'duration': {'type': 'number'}, 'wait_after': {'type': 'number'}},
        {'duration': 0.6, 'wait_after': 1}, sort_order=80,
    ),
    component(
        'bank_card_input', '银行卡输入', 'finance', '清理空格和分隔符后智能输入银行卡号',
        {**selector_properties(), 'value': {'type': 'string'}, 'clear_first': {'type': 'boolean'}},
        {**selector_defaults(), 'value': '', 'clear_first': True},
        required=['selector_type', 'selector', 'value'], sort_order=10,
    ),
    component(
        'money_input', '金额输入', 'finance', '标准化金额并限制为两位小数后智能输入',
        {**selector_properties(), 'value': {'type': 'string'}, 'clear_first': {'type': 'boolean'}},
        {**selector_defaults(), 'value': '', 'clear_first': True},
        required=['selector_type', 'selector', 'value'], sort_order=20,
    ),
]


CATEGORY_UPDATES = {
    'click': 'base_element',
    'input': 'base_element',
    'swipe': 'base_element',
    'long_press': 'base_element',
    'double_click': 'base_element',
    'drag': 'base_element',
    'swipe_to': 'base_element',
    'image_exists_click': 'base_element',
    'image_exists_click_chain': 'base_element',
    'wait': 'base_page',
    'screenshot': 'base_page',
    'assert': 'base_assert',
    'assert_activity': 'base_assert',
    'foreach_assert': 'base_assert',
    'short_video_feed': 'short_video',
    'wait_video_play': 'short_video',
    'watch_video': 'short_video',
    'random_action': 'short_video',
    'license_plate_input': 'industry',
    'set_variable': 'workflow',
    'unset_variable': 'workflow',
    'extract_output': 'workflow',
    'api_request': 'workflow',
    'if': 'workflow',
    'loop': 'workflow',
    'sequence': 'workflow',
    'try': 'workflow',
}


def add_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for component_definition in COMPONENTS:
        defaults = dict(component_definition)
        component_type = defaults.pop('type')
        AppComponent.objects.update_or_create(type=component_type, defaults=defaults)
    for component_type, category in CATEGORY_UPDATES.items():
        AppComponent.objects.filter(type=component_type).update(category=category)


def remove_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type__in=[item['type'] for item in COMPONENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0012_add_license_plate_input_component'),
    ]

    operations = [
        migrations.RunPython(add_components, remove_components),
    ]
