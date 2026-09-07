from django.db import migrations


def repair_knowledge_file_roots(apps, schema_editor):
    KnowledgeNode = apps.get_model('requirement_analysis', 'KnowledgeNode')
    KnowledgeRelation = apps.get_model('requirement_analysis', 'KnowledgeRelation')

    # Repair databases where a root was changed after the first backfill. A
    # root has outgoing ``contains`` edges and no containing parent of its own.
    candidates = KnowledgeNode.objects.filter(
        type='rule',
        rule__isnull=True,
        outgoing_relations__relation_type='contains',
    ).distinct()
    for node in candidates:
        if KnowledgeRelation.objects.filter(
            target_id=node.id,
            relation_type='contains',
        ).exists():
            continue
        node.type = 'domain'
        node.save(update_fields=['type'])


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0029_backfill_orphan_knowledge_rules'),
    ]

    operations = [
        migrations.RunPython(repair_knowledge_file_roots, migrations.RunPython.noop),
    ]
