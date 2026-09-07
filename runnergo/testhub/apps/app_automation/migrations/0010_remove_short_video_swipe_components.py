from django.db import migrations


REMOVED_COMPONENT_TYPES = {
    'short_video_swipe_up',
    'short_video_swipe_down',
}


def remove_obsolete_steps(value):
    if isinstance(value, list):
        cleaned = []
        for item in value:
            if isinstance(item, dict) and item.get('type') in REMOVED_COMPONENT_TYPES:
                continue
            cleaned.append(remove_obsolete_steps(item))
        return cleaned

    if isinstance(value, dict):
        return {key: remove_obsolete_steps(item) for key, item in value.items()}

    return value


def remove_short_video_swipe_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppCustomComponent = apps.get_model('app_automation', 'AppCustomComponent')
    AppTestCase = apps.get_model('app_automation', 'AppTestCase')

    AppComponent.objects.filter(type__in=REMOVED_COMPONENT_TYPES).delete()

    for component in AppCustomComponent.objects.all().iterator():
        cleaned_steps = remove_obsolete_steps(component.steps)
        if cleaned_steps != component.steps:
            component.steps = cleaned_steps
            component.save(update_fields=['steps'])

    for test_case in AppTestCase.objects.all().iterator():
        cleaned_flow = remove_obsolete_steps(test_case.ui_flow)
        if cleaned_flow != test_case.ui_flow:
            test_case.ui_flow = cleaned_flow
            test_case.save(update_fields=['ui_flow'])


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0009_alter_appelement_element_type'),
    ]

    operations = [
        migrations.RunPython(
            remove_short_video_swipe_components,
            migrations.RunPython.noop,
        ),
    ]
