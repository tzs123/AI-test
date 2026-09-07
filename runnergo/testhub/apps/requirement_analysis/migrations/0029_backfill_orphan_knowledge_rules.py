from django.db import migrations


def backfill_orphan_knowledge_rules(apps, schema_editor):
    """Make legacy rule-shaped graph nodes available to the rule search API.

    Older knowledge imports created ``KnowledgeNode(type='rule')`` rows
    directly.  The requirement-analysis search endpoint works with
    ``BusinessRuleKnowledge`` rows, so those nodes became invisible as soon as
    a knowledge file/project filter was applied.  Preserve the existing graph
    and attach each orphan node to a matching, or newly-created, rule.
    """
    User = apps.get_model('users', 'User')
    Project = apps.get_model('projects', 'Project')
    BusinessRuleKnowledge = apps.get_model(
        'requirement_analysis', 'BusinessRuleKnowledge'
    )
    KnowledgeNode = apps.get_model('requirement_analysis', 'KnowledgeNode')
    KnowledgeRelation = apps.get_model('requirement_analysis', 'KnowledgeRelation')

    fallback_user_id = (
        User.objects.filter(is_superuser=True)
        .order_by('id')
        .values_list('id', flat=True)
        .first()
    )
    if fallback_user_id is None:
        fallback_user_id = User.objects.order_by('id').values_list('id', flat=True).first()
    if fallback_user_id is None:
        # The FK is required, but an empty installation has nothing to repair.
        return

    for node in KnowledgeNode.objects.filter(
        type='rule',
        rule__isnull=True,
    ).order_by('id'):
        # A legacy knowledge-file root was sometimes saved as ``rule`` while
        # retaining its outgoing ``contains`` edges.  It is a container, not a
        # searchable rule; normalize it before creating a duplicate rule row.
        has_children = KnowledgeRelation.objects.filter(
            source_id=node.id,
            relation_type='contains',
        ).exists()
        has_parent = KnowledgeRelation.objects.filter(
            target_id=node.id,
            relation_type='contains',
        ).exists()
        if has_children and not has_parent:
            node.type = 'domain'
            node.save(update_fields=['type'])
            continue

        title = str(node.name or '').strip() or '未命名业务规则'
        content = str(node.content or '').strip() or title
        project_id = node.project_id

        created_by_id = fallback_user_id
        if project_id:
            owner_id = (
                Project.objects.filter(pk=project_id)
                .values_list('owner_id', flat=True)
                .first()
            )
            if owner_id:
                created_by_id = owner_id

        # Reuse an unbound equivalent rule where possible, but never attach
        # one rule to multiple nodes (the relation is one-to-one).
        rule = None
        candidates = BusinessRuleKnowledge.objects.filter(
            project_id=project_id,
            title=title[:200],
            content=content,
        ).exclude(status='deprecated').order_by('id')
        for candidate in candidates:
            if not KnowledgeNode.objects.filter(rule_id=candidate.id).exists():
                rule = candidate
                break

        if rule is None:
            rule = BusinessRuleKnowledge.objects.create(
                title=title[:200],
                content=content,
                project_id=project_id,
                status='approved',
                rule_type='business',
                risk_level='medium',
                created_by_id=created_by_id,
                reviewed_by_id=created_by_id,
            )

        node.rule_id = rule.id
        node.save(update_fields=['rule'])


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0028_testskill_execution_config'),
    ]

    operations = [
        migrations.RunPython(backfill_orphan_knowledge_rules, migrations.RunPython.noop),
    ]
