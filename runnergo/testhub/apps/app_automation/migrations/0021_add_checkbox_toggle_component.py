from django.db import migrations


CHECKBOX_TOGGLE_COMPONENT = {
    'type': 'checkbox_toggle',
    'name': '复选框设置',
    'category': 'base_element',
    'description': '按目标状态勾选、取消勾选或切换 Native/H5 复选框，已满足状态时不重复点击',
    'schema': {
        'required': ['selector_type', 'selector'],
        'properties': {
            'element_id': {'type': 'number'},
            'selector_type': {
                'type': 'string',
                'enum': ['id', 'xpath', 'text', 'ocr', 'image', 'pos', 'region'],
            },
            'selector': {'type': 'string'},
            'locator_strategies': {'type': 'array'},
            'image_scope': {'type': 'string'},
            'image_threshold': {'type': 'number'},
            'semantic_min_score': {'type': 'number'},
            'ocr_min_confidence': {'type': 'number'},
            'timeout': {'type': 'number'},
            'context_preference': {
                'type': 'string',
                'enum': ['auto', 'native', 'webview'],
            },
            'webview_context': {'type': 'string'},
            'webview_css': {'type': 'string'},
            'webview_xpath': {'type': 'string'},
            'webview_text': {'type': 'string'},
            'desired_state': {
                'type': 'string',
                'enum': ['checked', 'unchecked', 'toggle'],
            },
            'verify_state': {'type': 'boolean'},
            'strict_verify': {'type': 'boolean'},
            'state_timeout': {'type': 'number'},
            'retry_interval': {'type': 'number'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
    },
    'default_config': {
        'selector_type': 'text',
        'selector': '',
        'locator_strategies': [],
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
        'desired_state': 'checked',
        'verify_state': True,
        'strict_verify': False,
        'state_timeout': 2.0,
        'retry_interval': 0.2,
        'save_as': 'checkbox_toggle_result',
        'scope': 'local',
    },
    'enabled': True,
    'sort_order': 15,
}


def add_checkbox_toggle_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    defaults = dict(CHECKBOX_TOGGLE_COMPONENT)
    component_type = defaults.pop('type')
    AppComponent.objects.update_or_create(type=component_type, defaults=defaults)


def remove_checkbox_toggle_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type=CHECKBOX_TOGGLE_COMPONENT['type']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0020_element_name_unique_per_folder'),
    ]

    operations = [
        migrations.RunPython(
            add_checkbox_toggle_component,
            remove_checkbox_toggle_component,
        ),
    ]
