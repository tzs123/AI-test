from django.db import migrations


def normalize_business_rule_topology(apps, schema_editor):
    BusinessRuleKnowledge = apps.get_model('requirement_analysis', 'BusinessRuleKnowledge')
    KnowledgeNode = apps.get_model('requirement_analysis', 'KnowledgeNode')
    KnowledgeRelation = apps.get_model('requirement_analysis', 'KnowledgeRelation')

    for rule in BusinessRuleKnowledge.objects.exclude(status='deprecated').iterator():
        root_name = (rule.title or rule.business_domain or rule.module or '未分组业务').strip()
        root_node = KnowledgeNode.objects.filter(
            project_id=rule.project_id,
            type='domain',
            name=root_name,
            rule_id__isnull=True,
        ).first()
        if not root_node:
            root_node = KnowledgeNode.objects.create(
                name=root_name,
                type='domain',
                content=f'{root_name}业务知识一级节点',
                project_id=rule.project_id,
            )

        rule_name = (rule.content or rule.title or '').strip()
        if len(rule_name) > 36:
            rule_name = f'{rule_name[:36]}...'
        if not rule_name:
            rule_name = root_name

        node, _created = KnowledgeNode.objects.update_or_create(
            rule_id=rule.id,
            defaults={
                'name': rule_name,
                'type': 'rule',
                'content': rule.content,
                'project_id': rule.project_id,
            },
        )
        KnowledgeRelation.objects.get_or_create(
            source_id=root_node.id,
            target_id=node.id,
            relation_type='contains',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0019_knowledge_graph_models'),
    ]

    operations = [
        migrations.RunPython(normalize_business_rule_topology, migrations.RunPython.noop),
    ]
