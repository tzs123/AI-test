from django.db import migrations, models
import django.db.models.deletion


def backfill_rule_nodes(apps, schema_editor):
    BusinessRuleKnowledge = apps.get_model('requirement_analysis', 'BusinessRuleKnowledge')
    KnowledgeNode = apps.get_model('requirement_analysis', 'KnowledgeNode')

    for rule in BusinessRuleKnowledge.objects.exclude(status='deprecated').iterator():
        KnowledgeNode.objects.update_or_create(
            rule_id=rule.id,
            defaults={
                'name': rule.title,
                'type': 'rule',
                'content': rule.content,
                'project_id': rule.project_id,
            }
        )


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0018_prototype_understanding_asset'),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeNode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='节点名称')),
                ('type', models.CharField(choices=[('rule', '业务规则'), ('domain', '业务域'), ('entity', '业务实体'), ('attribute', '属性/维度'), ('state', '状态'), ('strategy', '测试策略'), ('other', '其他')], default='rule', max_length=30, verbose_name='节点类型')),
                ('content', models.TextField(blank=True, verbose_name='节点内容/业务规则')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='knowledge_nodes', to='projects.project', verbose_name='所属项目')),
                ('rule', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='knowledge_node', to='requirement_analysis.businessruleknowledge', verbose_name='关联业务规则')),
            ],
            options={
                'verbose_name': '知识节点',
                'verbose_name_plural': '知识节点',
                'db_table': 'knowledge_node',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='KnowledgeRelation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('relation_type', models.CharField(choices=[('upstream', '上游依赖'), ('downstream', '下游影响'), ('contains', '包含'), ('depends_on', '依赖'), ('constrains', '约束'), ('affects', '影响'), ('related', '关联')], default='related', max_length=40, verbose_name='关系类型')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='outgoing_relations', to='requirement_analysis.knowledgenode', verbose_name='源节点')),
                ('target', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='incoming_relations', to='requirement_analysis.knowledgenode', verbose_name='目标节点')),
            ],
            options={
                'verbose_name': '知识关系',
                'verbose_name_plural': '知识关系',
                'db_table': 'knowledge_relation',
                'ordering': ['source_id', 'target_id'],
            },
        ),
        migrations.AddIndex(
            model_name='knowledgenode',
            index=models.Index(fields=['project', 'type'], name='knode_project_type_idx'),
        ),
        migrations.AddIndex(
            model_name='knowledgenode',
            index=models.Index(fields=['name'], name='knowledge_node_name_idx'),
        ),
        migrations.AddConstraint(
            model_name='knowledgerelation',
            constraint=models.CheckConstraint(check=~models.Q(source=models.F('target')), name='knowledge_relation_no_self_loop'),
        ),
        migrations.AddConstraint(
            model_name='knowledgerelation',
            constraint=models.UniqueConstraint(fields=('source', 'target', 'relation_type'), name='unique_knowledge_relation'),
        ),
        migrations.RunPython(backfill_rule_nodes, migrations.RunPython.noop),
    ]
