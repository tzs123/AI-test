from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0008_device_bridge_url'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appelement',
            name='element_type',
            field=models.CharField(
                choices=[
                    ('appium', 'Appium元素'),
                    ('ocr', 'OCR元素'),
                    ('image', '图片元素'),
                    ('pos', '坐标元素'),
                    ('region', '区域元素'),
                ],
                max_length=10,
                verbose_name='元素类型',
            ),
        ),
        migrations.AlterField(
            model_name='appelement',
            name='config',
            field=models.JSONField(
                default=dict,
                help_text='''
        appium类型: {
            "platform": "android",
            "resource_id": "com.demo:id/login",
            "accessibility_id": "登录按钮",
            "text": "登录",
            "xpath": "//*[@resource-id='com.demo:id/login']",
            "bounds": {"x1": 100, "y1": 200, "x2": 300, "y2": 260},
            "locator_strategies": [
                {"type": "resource_id", "enabled": true},
                {"type": "accessibility", "enabled": true},
                {"type": "text", "enabled": true},
                {"type": "ocr", "enabled": true},
                {"type": "image", "enabled": true},
                {"type": "position", "enabled": true}
            ]
        }
        ocr类型: {"ocr_text": "登录", "ocr_languages": ["ch_sim", "en"]}
        image类型: {
            "image_category": "common",
            "image_path": "common/login.png", 
            "file_hash": "abc123...",
            "image_threshold": 0.85, 
            "rgb": false
        }
        pos类型: {"x": 100, "y": 200}
        region类型: {"x1": 100, "y1": 200, "x2": 300, "y2": 400}
        ''',
                verbose_name='元素配置',
            ),
        ),
    ]
