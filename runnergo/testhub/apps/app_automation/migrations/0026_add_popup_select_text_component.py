from django.db import migrations


def install_popup_select_text_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.update_or_create(
        type='popup_select_text',
        defaults={
            'name': '弹框选择',
            'category': 'page_interaction',
            'description': '输入弹框文本或省/市/区路径，自动定位滚轮或级联选择器并依次选择',
            'schema': {
                'required': ['value'],
                'properties': {
                    'value': {'type': 'string'},
                    'match_mode': {'type': 'string', 'enum': ['exact', 'contains']},
                    'max_swipes': {'type': 'number'},
                    'popup_region': {'type': 'array'},
                    'interval': {'type': 'number'},
                    'duration': {'type': 'number'},
                    'save_as': {'type': 'string'},
                    'scope': {'type': 'string', 'enum': ['local', 'global']},
                },
            },
            'default_config': {
                'value': '',
                'match_mode': 'exact',
                'max_swipes': 8,
                'popup_region': [0.0, 0.35, 1.0, 0.98],
                'interval': 0.25,
                'duration': 0.35,
                'save_as': 'popup_select_result',
                'scope': 'local',
            },
            'enabled': True,
            'sort_order': 35,
        },
    )


def remove_popup_select_text_component(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(type='popup_select_text').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0025_app_agent_matrix_telemetry'),
    ]

    operations = [
        migrations.RunPython(
            install_popup_select_text_component,
            remove_popup_select_text_component,
        ),
    ]
