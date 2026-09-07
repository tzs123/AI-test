# -*- coding: utf-8 -*-
"""从同目录下的 ui-component-pack.yaml 加载组件包"""
import os
import yaml
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.app_automation.models import AppComponent


class Command(BaseCommand):
    help = '从 ui-component-pack.yaml 加载组件包到数据库'

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='覆盖已存在的组件'
        )
        parser.add_argument(
            '--prune',
            action='store_true',
            help='删除数据库中未包含在 YAML 组件包里的组件（谨慎使用）'
        )

    def handle(self, *args, **options):
        # YAML 文件路径（与本脚本同目录）
        current_dir = os.path.dirname(os.path.abspath(__file__))
        yaml_path = os.path.join(current_dir, 'ui-component-pack.yaml')
        
        if not os.path.exists(yaml_path):
            self.stdout.write(self.style.ERROR(f'❌ 文件不存在: {yaml_path}'))
            return
        
        self.stdout.write(f'📖 读取组件包: {yaml_path}')
        
        # 读取 YAML
        with open(yaml_path, 'r', encoding='utf-8') as f:
            manifest = yaml.safe_load(f)
        
        if not manifest or 'components' not in manifest:
            self.stdout.write(self.style.ERROR('❌ 组件包格式错误'))
            return
        
        components = manifest['components']
        overwrite = options['overwrite']
        prune = options['prune']
        
        self.stdout.write(self.style.SUCCESS(f'📦 组件包信息:'))
        self.stdout.write(f'   名称: {manifest.get("name", "N/A")}')
        self.stdout.write(f'   版本: {manifest.get("version", "N/A")}')
        self.stdout.write(f'   作者: {manifest.get("author", "N/A")}')
        self.stdout.write(f'   组件数: {len(components)}')
        self.stdout.write('')
        
        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []
        
        deleted_count = 0
        
        with transaction.atomic():
            # 收集 YAML 中定义的所有 type
            yaml_types = set()
            
            for item in components:
                component_type = item.get('type')
                if not component_type:
                    errors.append(f'组件缺少 type: {item.get("name", "unknown")}')
                    continue
                
                yaml_types.add(component_type)
                
                defaults = {
                    'name': item.get('name') or component_type,
                    'category': item.get('category', ''),
                    'description': item.get('description', ''),
                    'schema': item.get('schema') or {},
                    'default_config': item.get('default_config') or {},
                    'enabled': item.get('enabled', True),
                    'sort_order': item.get('sort_order', 0),
                }
                
                # 检查是否已存在
                existing = AppComponent.objects.filter(type=component_type).first()
                
                if existing and not overwrite:
                    skipped_count += 1
                    self.stdout.write(f'⏭️  跳过组件: {defaults["name"]} ({component_type}) - 已存在')
                    continue
                
                obj, created = AppComponent.objects.update_or_create(
                    type=component_type,
                    defaults=defaults
                )
                
                if created:
                    created_count += 1
                    self.stdout.write(self.style.SUCCESS(f'✅ 创建组件: {obj.name} ({obj.type})'))
                else:
                    updated_count += 1
                    self.stdout.write(self.style.WARNING(f'♻️  更新组件: {obj.name} ({obj.type})'))
            
            # 组件库支持由迁移、行业插件和导入包共同扩展。只有显式 --prune
            # 才删除 YAML 未声明的组件，避免覆盖加载基础包时误删扩展组件。
            if prune and yaml_types:
                stale = AppComponent.objects.exclude(type__in=yaml_types)
                for obj in stale:
                    self.stdout.write(self.style.ERROR(f'🗑️  删除组件: {obj.name} ({obj.type}) - YAML 中已移除'))
                deleted_count = stale.count()
                stale.delete()
        
        # 统计
        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write(self.style.SUCCESS('🎉 导入完成！'))
        self.stdout.write(f'   新建: {created_count} 个')
        self.stdout.write(f'   更新: {updated_count} 个')
        self.stdout.write(f'   跳过: {skipped_count} 个')
        if deleted_count:
            self.stdout.write(f'   清理: {deleted_count} 个')
        self.stdout.write(f'   总计: {AppComponent.objects.filter(enabled=True).count()} 个启用的组件')
        
        if errors:
            self.stdout.write(self.style.ERROR(f'   错误: {len(errors)} 个'))
            for error in errors:
                self.stdout.write(self.style.ERROR(f'      - {error}'))
        
        self.stdout.write('=' * 60)
        self.stdout.write('')
        
        # 提示信息
        if skipped_count > 0 and not overwrite:
            self.stdout.write(self.style.WARNING(
                f'💡 提示: 有 {skipped_count} 个组件已存在被跳过。'
            ))
            self.stdout.write(self.style.WARNING(
                '   如需覆盖，请使用: python manage.py load_component_pack --overwrite'
            ))
            self.stdout.write('')
        
        self.stdout.write(self.style.SUCCESS('📋 下一步:'))
        self.stdout.write('   1. 启动前端: cd frontend && npm run dev')
        self.stdout.write('   2. 访问场景编排器: http://localhost:3000/app-automation/test-cases/scene-builder')
        self.stdout.write('   3. 在左侧组件面板查看所有组件')
