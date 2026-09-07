from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0005_remove_browser_use_ai_role'),
    ]

    operations = [
        migrations.AlterField(
            model_name='requirementdocument',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('pdf', 'PDF文档'),
                    ('docx', 'Word文档'),
                    ('txt', '文本文档'),
                    ('md', 'Markdown文档'),
                    ('image', '需求图片'),
                    ('xmind', 'XMind脑图'),
                ],
                max_length=10,
                verbose_name='文档类型',
            ),
        ),
    ]
