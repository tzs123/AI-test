from django.db import migrations


GUARDED_COMPONENT_TYPES = [
    'click', 'input', 'clear_text', 'smart_click', 'smart_input',
    'self_heal_click', 'self_heal_input', 'long_press', 'double_click',
]


def add_guard(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for component in AppComponent.objects.filter(type__in=GUARDED_COMPONENT_TYPES):
        schema = dict(component.schema or {})
        properties = dict(schema.get('properties') or {})
        properties['guard_security_challenge'] = {'type': 'boolean'}
        schema['properties'] = properties
        default_config = dict(component.default_config or {})
        default_config.setdefault('guard_security_challenge', True)
        component.schema = schema
        component.default_config = default_config
        component.save(update_fields=['schema', 'default_config'])


def remove_guard(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for component in AppComponent.objects.filter(type__in=GUARDED_COMPONENT_TYPES):
        schema = dict(component.schema or {})
        properties = dict(schema.get('properties') or {})
        properties.pop('guard_security_challenge', None)
        schema['properties'] = properties
        default_config = dict(component.default_config or {})
        default_config.pop('guard_security_challenge', None)
        component.schema = schema
        component.default_config = default_config
        component.save(update_fields=['schema', 'default_config'])


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0017_complete_platform_component_library'),
    ]

    operations = [
        migrations.RunPython(add_guard, remove_guard),
    ]
