from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0009_aimodelconfig_model_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='generation_mode',
            field=models.CharField(
                choices=[
                    ('quick', '快速生成'),
                    ('deep', '深度测试设计'),
                    ('security', '安全专项测试'),
                ],
                default='quick',
                max_length=20,
                verbose_name='测试生成模式',
            ),
        ),
        migrations.AlterField(
            model_name='requirementdocument',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('pdf', 'PDF文档'),
                    ('doc', 'Word 97-2003文档'),
                    ('docx', 'Word文档'),
                    ('excel', 'Excel文档'),
                    ('txt', '文本文档'),
                    ('md', 'Markdown文档'),
                    ('image', '需求图片'),
                    ('yaml', 'YAML文档'),
                    ('json', 'JSON文档'),
                    ('har', 'HAR文档'),
                    ('xml', 'XML文档'),
                    ('csv', 'CSV文档'),
                    ('sql', 'SQL文档'),
                ],
                max_length=10,
                verbose_name='文档类型',
            ),
        ),
    ]
