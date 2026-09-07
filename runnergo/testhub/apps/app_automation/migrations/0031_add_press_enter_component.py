from django.db import migrations


PRESS_ENTER_COMPONENT = {
    'type': 'press_enter',
    'name': '回车键',
    'category': 'base_page',
    'description': '发送系统回车键，常用于搜索、提交输入框或关闭键盘',
    'schema': {
        'required': [],
        'properties': {
            'wait_after': {'type': 'number'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
    },
    'default_config': {
        'wait_after': 0.3,
        'save_as': 'press_enter_result',
        'scope': 'local',
    },
    'enabled': True,
    'sort_order': 65,
}


def add_press_enter_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    defaults = dict(PRESS_ENTER_COMPONENT)
    component_type = defaults.pop('type')
    AppComponent.objects.update_or_create(type=component_type, defaults=defaults)


def remove_press_enter_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type=PRESS_ENTER_COMPONENT['type']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0029_app_test_config_ios_browser_start_url'),
    ]

    operations = [
        migrations.RunPython(
            add_press_enter_component,
            remove_press_enter_component,
        ),
    ]
