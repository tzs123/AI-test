from django.db import migrations


def merge_duplicate_domain_nodes(apps, schema_editor):
    KnowledgeNode = apps.get_model('requirement_analysis', 'KnowledgeNode')
    KnowledgeRelation = apps.get_model('requirement_analysis', 'KnowledgeRelation')

    groups = {}
    for node in KnowledgeNode.objects.filter(
        type='domain',
        rule_id__isnull=True,
    ).order_by('project_id', 'name', 'id'):
        key = (node.project_id, (node.name or '').strip())
        if not key[1]:
            continue
        groups.setdefault(key, []).append(node)

    for nodes in groups.values():
        if len(nodes) < 2:
            continue

        keeper = nodes[0]
        duplicate_ids = [node.id for node in nodes[1:]]
        for duplicate_id in duplicate_ids:
            for relation in KnowledgeRelation.objects.filter(source_id=duplicate_id):
                if relation.target_id != keeper.id:
                    KnowledgeRelation.objects.get_or_create(
                        source_id=keeper.id,
                        target_id=relation.target_id,
                        relation_type=relation.relation_type,
                    )
                relation.delete()

            for relation in KnowledgeRelation.objects.filter(target_id=duplicate_id):
                if relation.source_id != keeper.id:
                    KnowledgeRelation.objects.get_or_create(
                        source_id=relation.source_id,
                        target_id=keeper.id,
                        relation_type=relation.relation_type,
                    )
                relation.delete()

        KnowledgeNode.objects.filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0020_normalize_business_rule_topology'),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_domain_nodes, migrations.RunPython.noop),
    ]
