from django.db import migrations


SELECTOR_TYPES = ['id', 'xpath', 'text', 'ocr', 'image', 'pos', 'region']


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


VISUAL_PROPERTIES = {
    'description': {'type': 'string'},
    'reference_image': {'type': 'string'},
    'image_scope': {'type': 'string'},
    'image_threshold': {'type': 'number'},
    'region': {'type': 'array'},
    'fallback_position': {'type': 'object'},
    'strategy_order': {'type': 'array'},
    'semantic_min_score': {'type': 'number'},
    'ocr_min_confidence': {'type': 'number'},
    'match_mode': {'type': 'string', 'enum': ['contains', 'exact']},
    'timeout': {'type': 'number'},
    'retry_interval': {'type': 'number'},
    'save_as': {'type': 'string'},
    'scope': {'type': 'string', 'enum': ['local', 'global']},
}


VISUAL_DEFAULTS = {
    'description': '',
    'reference_image': '',
    'image_scope': 'common',
    'image_threshold': 0.85,
    'region': [],
    'fallback_position': {},
    'strategy_order': ['semantic', 'ocr', 'image', 'position'],
    'semantic_min_score': 20,
    'ocr_min_confidence': 0.45,
    'match_mode': 'contains',
    'timeout': 5,
    'retry_interval': 0.4,
    'save_as': '',
    'scope': 'local',
}


SELF_HEAL_PROPERTIES = {
    'element_id': {'type': 'number'},
    'selector_type': {'type': 'string', 'enum': SELECTOR_TYPES},
    'selector': {'type': 'string'},
    'image_scope': {'type': 'string'},
    'image_threshold': {'type': 'number'},
    'timeout': {'type': 'number'},
    'self_heal_mode': {'type': 'string', 'enum': ['report', 'persist']},
    'allow_coordinate_persist': {'type': 'boolean'},
    'save_as': {'type': 'string'},
    'scope': {'type': 'string', 'enum': ['local', 'global']},
}


SELF_HEAL_DEFAULTS = {
    'selector_type': 'text',
    'selector': '',
    'image_scope': 'common',
    'image_threshold': 0.85,
    'timeout': 5,
    'self_heal_mode': 'report',
    'allow_coordinate_persist': False,
    'save_as': '',
    'scope': 'local',
}


COMPONENTS = [
    component(
        'record_video', '录屏', 'base_page',
        '录制当前设备画面并保存到本次执行媒体目录',
        {
            'duration': {'type': 'number'},
            'fps': {'type': 'number'},
            'record_mode': {'type': 'string', 'enum': ['ffmpeg', 'yosemite']},
            'orientation': {'type': 'number'},
            'max_size': {'type': 'number'},
            'filename': {'type': 'string'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
        {
            'duration': 10, 'fps': 8, 'record_mode': 'ffmpeg',
            'orientation': 0, 'max_size': 1080, 'filename': '',
            'save_as': 'record_video_result', 'scope': 'local',
        },
        sort_order=70,
    ),
    component(
        'visual_locate', '视觉定位', 'ai',
        '按语义节点、OCR、参考图片和归一化坐标定位目标并输出证据',
        VISUAL_PROPERTIES,
        {**VISUAL_DEFAULTS, 'save_as': 'visual_locate_result'},
        sort_order=50,
    ),
    component(
        'visual_click', '视觉点击', 'ai',
        '通过视觉定位链找到目标后执行点击',
        VISUAL_PROPERTIES,
        {**VISUAL_DEFAULTS, 'save_as': 'visual_click_result'},
        sort_order=60,
    ),
    component(
        'visual_assert', '视觉断言', 'ai',
        '断言描述目标、参考图片或视觉位置存在或不存在',
        {**VISUAL_PROPERTIES, 'expected_exists': {'type': 'boolean'}},
        {**VISUAL_DEFAULTS, 'expected_exists': True, 'save_as': 'visual_assert_result'},
        sort_order=70,
    ),
    component(
        'self_heal_click', '自愈点击', 'ai',
        '主定位失败后自动使用备用策略点击，并按配置记录或持久化新优先级',
        SELF_HEAL_PROPERTIES,
        {**SELF_HEAL_DEFAULTS, 'save_as': 'self_heal_click_result'},
        sort_order=80,
    ),
    component(
        'self_heal_input', '自愈输入', 'ai',
        '主定位失败后自动使用备用策略定位输入框并输入内容',
        {
            **SELF_HEAL_PROPERTIES,
            'value': {'type': 'string'},
            'input_kind': {'type': 'string'},
            'clear_first': {'type': 'boolean'},
            'send_enter': {'type': 'boolean'},
        },
        {
            **SELF_HEAL_DEFAULTS,
            'value': '', 'input_kind': 'auto', 'clear_first': True,
            'send_enter': False, 'save_as': 'self_heal_input_result',
        },
        required=['value'], sort_order=90,
    ),
]


def add_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for definition in COMPONENTS:
        defaults = dict(definition)
        component_type = defaults.pop('type')
        AppComponent.objects.update_or_create(type=component_type, defaults=defaults)


def remove_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type__in=[item['type'] for item in COMPONENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0015_add_composite_business_components'),
    ]

    operations = [
        migrations.RunPython(add_components, remove_components),
    ]
