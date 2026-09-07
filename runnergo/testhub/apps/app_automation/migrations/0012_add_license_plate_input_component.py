from django.db import migrations, models


LICENSE_PLATE_INPUT_COMPONENT = {
    'type': 'license_plate_input',
    'name': '车牌输入',
    'category': 'business',
    'description': '自动打开车牌键盘，按省份、字母和数字顺序输入车牌，并关闭键盘',
    'schema': {
        'required': ['plate', 'selector_type', 'selector'],
        'properties': {
            'plate': {'type': 'string'},
            'selector_type': {'type': 'string'},
            'selector': {'type': 'string'},
            'image_scope': {'type': 'string'},
            'image_threshold': {'type': 'number'},
            'timeout': {'type': 'number'},
            'keyboard_region': {'type': 'array'},
            'key_strategies': {'type': 'array'},
            'key_timeout': {'type': 'number'},
            'key_interval': {'type': 'number'},
            'ocr_min_confidence': {'type': 'number'},
            'coordinate_fallback': {'type': 'boolean'},
            'key_positions': {'type': 'object'},
            'close_keyboard': {'type': 'boolean'},
            'confirm_texts': {'type': 'array'},
            'save_as': {'type': 'string'},
        },
    },
    'default_config': {
        'plate': '浙A12345',
        'selector_type': 'pos',
        'selector': '',
        'image_scope': 'common',
        'image_threshold': 0.85,
        'timeout': 5,
        'keyboard_region': [0.0, 0.70, 1.0, 0.98],
        'key_strategies': ['semantic', 'ocr', 'coordinate'],
        'key_timeout': 3,
        'key_interval': 0.15,
        'ocr_min_confidence': 0.35,
        'coordinate_fallback': True,
        'key_positions': {},
        'close_keyboard': True,
        'confirm_texts': ['确认', '完成', '确定'],
        'save_as': 'license_plate_input_result',
    },
    'enabled': True,
    'sort_order': 5,
}


def add_license_plate_input_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    defaults = dict(LICENSE_PLATE_INPUT_COMPONENT)
    component_type = defaults.pop('type')
    AppComponent.objects.update_or_create(type=component_type, defaults=defaults)


def remove_license_plate_input_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type=LICENSE_PLATE_INPUT_COMPONENT['type']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0011_add_short_video_business_components'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appelement',
            name='element_type',
            field=models.CharField(
                choices=[
                    ('appium', '智能元素'),
                    ('ocr', 'OCR元素'),
                    ('image', '图片元素'),
                    ('pos', '坐标元素'),
                    ('region', '区域元素'),
                ],
                max_length=10,
                verbose_name='元素类型',
            ),
        ),
        migrations.RunPython(
            add_license_plate_input_component,
            remove_license_plate_input_component,
        ),
    ]
