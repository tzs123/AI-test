# -*- coding: utf-8 -*-
"""APP UI组件管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.http import HttpResponse
from django.db import transaction
import logging
import json
import os
import re
import yaml
from datetime import datetime
from .test_case_views import AppPagination

from ..models import AppComponent, AppCustomComponent, AppComponentPackage
from ..serializers import (
    AppComponentSerializer,
    AppCustomComponentSerializer,
    AppComponentPackageSerializer
)
from ..utils.recorded_steps import repair_recorded_input_steps

logger = logging.getLogger(__name__)


class AppComponentViewSet(viewsets.ModelViewSet):
    """UI组件定义视图"""
    queryset = AppComponent.objects.all()
    serializer_class = AppComponentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination

    def get_queryset(self):
        queryset = AppComponent.objects.all()
        enabled = self.request.query_params.get('enabled')
        if enabled is not None:
            if enabled in ('1', 'true', 'True'):
                queryset = queryset.filter(enabled=True)
            elif enabled in ('0', 'false', 'False'):
                queryset = queryset.filter(enabled=False)
        return queryset.order_by('sort_order', '-updated_at')

    def list(self, request, *args, **kwargs):
        """获取组件列表"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        })

    def create(self, request, *args, **kwargs):
        """创建组件"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """更新组件"""
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=kwargs.pop('partial', False))
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response({
            'success': True,
            'data': serializer.data
        })

    def destroy(self, request, *args, **kwargs):
        """删除组件"""
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response({
            'success': True,
            'message': '删除成功'
        })


class AppCustomComponentViewSet(viewsets.ModelViewSet):
    """自定义组件视图"""
    queryset = AppCustomComponent.objects.all()
    serializer_class = AppCustomComponentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination

    def get_queryset(self):
        queryset = AppCustomComponent.objects.all()
        enabled = self.request.query_params.get('enabled')
        if enabled is not None:
            if enabled in ('1', 'true', 'True'):
                queryset = queryset.filter(enabled=True)
            elif enabled in ('0', 'false', 'False'):
                queryset = queryset.filter(enabled=False)
        return queryset.order_by('sort_order', '-updated_at')

    def list(self, request, *args, **kwargs):
        """获取自定义组件列表"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        })

    @staticmethod
    def _recording_package_ids_for_type(component_type):
        package_ids = []
        for package in AppComponentPackage.objects.all().only('id', 'manifest').iterator():
            manifest = package.manifest if isinstance(package.manifest, dict) else {}
            if (
                manifest.get('kind') == 'runnergo_action_recording'
                and str(manifest.get('type') or '').strip() == component_type
            ):
                package_ids.append(package.id)
        return package_ids

    def create(self, request, *args, **kwargs):
        """创建自定义组件"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """更新自定义组件"""
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=kwargs.pop('partial', False))
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response({
            'success': True,
            'data': serializer.data
        })

    def destroy(self, request, *args, **kwargs):
        """删除自定义组件"""
        instance = self.get_object()
        component_type = instance.type
        with transaction.atomic():
            package_ids = self._recording_package_ids_for_type(component_type)
            if package_ids:
                AppComponentPackage.objects.filter(id__in=package_ids).delete()
            self.perform_destroy(instance)
        return Response({
            'success': True,
            'message': '删除成功'
        })


class AppComponentPackageViewSet(viewsets.ModelViewSet):
    """组件包视图集（用于导入/导出组件定义）"""
    queryset = AppComponentPackage.objects.all()
    serializer_class = AppComponentPackageSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination

    def _parse_manifest(self, request) -> dict:
        """解析组件包清单"""
        if 'file' in request.FILES:
            upload = request.FILES['file']
            raw = upload.read()
            try:
                content = raw.decode('utf-8-sig')
            except Exception:
                content = raw.decode('utf-8')
            filename = upload.name.lower()
            if filename.endswith('.json'):
                return json.loads(content)
            return yaml.safe_load(content)

        manifest = request.data.get('manifest')
        if isinstance(manifest, dict):
            return manifest
        if isinstance(manifest, str) and manifest.strip():
            try:
                return json.loads(manifest)
            except Exception:
                return yaml.safe_load(manifest)
        raise ValueError("请上传组件包文件或提供manifest")

    @staticmethod
    def _recording_component_identity(request, manifest=None):
        upload = request.FILES.get('file')
        filename = getattr(upload, 'name', '') or ''
        file_stem = os.path.splitext(os.path.basename(filename))[0]
        manifest = manifest if isinstance(manifest, dict) else {}
        name = str(
            request.data.get('name') or manifest.get('name') or file_stem or '录制操作组件'
        ).strip()[:100]
        raw_type = str(request.data.get('type') or manifest.get('type') or file_stem or name).lower()
        slug = re.sub(r'[^a-z0-9_]+', '_', raw_type).strip('_') or 'actions'
        if not slug.startswith('recorded_'):
            slug = f'recorded_{slug}'
        return name, slug[:50]

    @staticmethod
    def _normalize_recorded_steps(steps):
        if not isinstance(steps, list) or not steps:
            raise ValueError('录制步骤文件中没有可导入的 steps')
        normalized_steps = []
        errors = []
        for index, item in enumerate(steps, 1):
            if not isinstance(item, dict) or not item.get('type'):
                errors.append(f'第 {index} 个步骤缺少 type')
                continue
            config = item.get('config') if isinstance(item.get('config'), dict) else {}
            normalized_steps.append({
                'type': str(item.get('type')),
                'name': item.get('name') or f"步骤 {index}",
                'config': config,
            })
        if errors:
            raise ValueError('录制步骤格式错误：' + '；'.join(errors))
        repair_result = repair_recorded_input_steps(normalized_steps, reject_unfixed=True)
        if repair_result.problems:
            messages = '；'.join(
                item.get('message') or item.get('reason') or '输入步骤缺少稳定定位'
                for item in repair_result.problems
            )
            raise ValueError('录制步骤存在不可回放的输入定位：' + messages)
        return repair_result.steps

    @staticmethod
    def _upsert_recorded_custom_component(component_name, component_type, normalized_steps, *, overwrite=True):
        defaults = {
            'name': component_name,
            'description': '从设备管理操作录制文件导入',
            'schema': {},
            'default_config': {},
            'steps': normalized_steps,
            'enabled': True,
            'sort_order': 0,
        }
        custom_component, created = AppCustomComponent.objects.get_or_create(
            type=component_type,
            defaults=defaults,
        )
        updated = False
        skipped = False
        if not created:
            if overwrite:
                for key, value in defaults.items():
                    setattr(custom_component, key, value)
                custom_component.save()
                updated = True
            else:
                skipped = True
        return custom_component, created, updated, skipped

    def _repair_recorded_step_packages(self):
        repaired = 0
        for package in AppComponentPackage.objects.all().order_by('id'):
            manifest = package.manifest if isinstance(package.manifest, dict) else {}
            if manifest.get('kind') != 'runnergo_action_recording' or not isinstance(manifest.get('steps'), list):
                continue
            component_type = str(manifest.get('type') or '').strip()
            component_name = str(manifest.get('name') or package.name or component_type or '录制操作组件').strip()[:100]
            if not component_type or AppCustomComponent.objects.filter(type=component_type).exists():
                continue
            try:
                normalized_steps = self._normalize_recorded_steps(manifest.get('steps'))
            except ValueError as exc:
                logger.warning('修复录制组件包 %s 失败: %s', package.id, exc)
                continue
            self._upsert_recorded_custom_component(
                component_name,
                component_type,
                normalized_steps,
                overwrite=False,
            )
            repaired += 1
        return repaired

    def list(self, request, *args, **kwargs):
        """获取组件包列表，并修复旧版本只保存包记录但未生成自定义组件的数据。"""
        repaired = self._repair_recorded_step_packages()
        response = super().list(request, *args, **kwargs)
        if isinstance(response.data, dict):
            response.data['repaired_custom_components'] = repaired
        return response

    def _import_recorded_steps(self, request, steps, manifest=None):
        try:
            normalized_steps = self._normalize_recorded_steps(steps)
        except ValueError as exc:
            message = str(exc)
            return Response({
                'success': False,
                'message': message,
                'msg': message,
            }, status=status.HTTP_400_BAD_REQUEST)

        component_name, component_type = self._recording_component_identity(request, manifest)
        overwrite = str(request.data.get('overwrite', '1')).lower() in ('1', 'true', 'yes')

        with transaction.atomic():
            custom_component, created, updated, skipped = self._upsert_recorded_custom_component(
                component_name,
                component_type,
                normalized_steps,
                overwrite=overwrite,
            )

            stored_manifest = {
                'kind': 'runnergo_action_recording',
                'version': (manifest or {}).get('version', 1) if isinstance(manifest, dict) else 1,
                'name': component_name,
                'type': component_type,
                'device_id': (manifest or {}).get('device_id', '') if isinstance(manifest, dict) else '',
                'screen': (manifest or {}).get('screen', {}) if isinstance(manifest, dict) else {},
                'steps': normalized_steps,
            }
            package = AppComponentPackage.objects.create(
                name=component_name,
                version=str(stored_manifest['version']),
                description='设备操作录制导入',
                source='upload',
                manifest=stored_manifest,
                created_by=request.user if request.user.is_authenticated else None,
            )

        message = (
            f'录制步骤已导入为自定义组件“{component_name}”'
            if not skipped else
            f'自定义组件“{component_name}”已存在，未覆盖'
        )
        return Response({
            'success': True,
            'message': message,
            'msg': message,
            'data': {
                'kind': 'recorded_steps',
                'package_id': package.id,
                'custom_component_id': custom_component.id,
                'custom_component_name': custom_component.name,
                'created': 1 if created else 0,
                'updated': 1 if updated else 0,
                'skipped': 1 if skipped else 0,
                'step_count': len(normalized_steps),
            },
        }, status=status.HTTP_201_CREATED)

    def create(self, request, *args, **kwargs):
        """导入组件包"""
        try:
            manifest = self._parse_manifest(request)
        except Exception as error:
            return Response({
                'success': False,
                'message': f'解析组件包失败: {error}'
            }, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(manifest, list):
            is_recorded_steps = bool(manifest) and all(
                isinstance(item, dict) and item.get('type') for item in manifest
            )
            if is_recorded_steps:
                return self._import_recorded_steps(request, manifest)
            message = '组件包格式错误：manifest 必须为对象'
            return Response({
                'success': False,
                'message': message,
                'msg': message,
            }, status=status.HTTP_400_BAD_REQUEST)

        if not isinstance(manifest, dict):
            message = '组件包格式错误：manifest 必须为对象'
            return Response({
                'success': False,
                'message': message,
                'msg': message,
            }, status=status.HTTP_400_BAD_REQUEST)

        if not manifest.get('components') and isinstance(manifest.get('steps'), list):
            return self._import_recorded_steps(request, manifest.get('steps'), manifest)

        components = manifest.get('components') or []
        if not isinstance(components, list) or not components:
            message = '组件包缺少 components 列表；请使用“导出组件包”生成的文件'
            return Response({
                'success': False,
                'message': message,
                'msg': message,
            }, status=status.HTTP_400_BAD_REQUEST)

        overwrite = str(request.data.get('overwrite', '1')).lower() in ('1', 'true', 'yes')
        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []

        with transaction.atomic():
            for item in components:
                if not isinstance(item, dict):
                    errors.append('组件定义格式错误')
                    continue
                component_type = item.get('type')
                if not component_type:
                    errors.append('组件缺少 type')
                    continue
                defaults = {
                    'name': item.get('name') or component_type,
                    'category': item.get('category', ''),
                    'description': item.get('description', ''),
                    'schema': item.get('schema') or {},
                    'default_config': item.get('default_config') or {},
                    'enabled': item.get('enabled', True),
                    'sort_order': item.get('sort_order', 0),
                }
                obj, created = AppComponent.objects.get_or_create(type=component_type, defaults=defaults)
                if created:
                    created_count += 1
                    continue
                if not overwrite:
                    skipped_count += 1
                    continue
                for key, value in defaults.items():
                    setattr(obj, key, value)
                obj.save()
                updated_count += 1

            if errors:
                return Response({
                    'success': False,
                    'message': '组件包包含错误: ' + ', '.join(errors),
                    'data': errors
                }, status=status.HTTP_400_BAD_REQUEST)

            package = AppComponentPackage.objects.create(
                name=manifest.get('name') or request.data.get('name') or 'component-package',
                version=manifest.get('version') or request.data.get('version', ''),
                description=manifest.get('description') or request.data.get('description', ''),
                author=manifest.get('author') or request.data.get('author', ''),
                source=request.data.get('source', 'upload'),
                manifest=manifest,
                created_by=request.user if request.user.is_authenticated else None
            )

        return Response({
            'success': True,
            'message': '组件包已安装',
            'data': {
                'package_id': package.id,
                'created': created_count,
                'updated': updated_count,
                'skipped': skipped_count
            }
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """导出组件包"""
        # 注意: 不能用 'format' 作参数名，它是 DRF DefaultRouter 的保留字
        export_format = str(request.query_params.get('export_format', 'yaml')).lower()
        include_disabled = str(request.query_params.get('include_disabled', '0')).lower() in ('1', 'true', 'yes')
        name = request.query_params.get('name', 'ui-component-pack')
        version = request.query_params.get('version', '') or datetime.now().strftime('%Y.%m.%d')
        author = request.query_params.get('author', '')
        description = request.query_params.get('description', '导出的组件包')

        queryset = AppComponent.objects.all()
        if not include_disabled:
            queryset = queryset.filter(enabled=True)

        components = []
        for item in queryset.order_by('sort_order', 'type'):
            components.append({
                'type': item.type,
                'name': item.name,
                'category': item.category,
                'description': item.description,
                'schema': item.schema or {},
                'default_config': item.default_config or {},
                'enabled': item.enabled,
                'sort_order': item.sort_order,
            })

        manifest = {
            'name': name,
            'version': version,
            'description': description,
            'author': author,
            'components': components,
        }

        if export_format == 'json':
            content = json.dumps(manifest, ensure_ascii=False, indent=2)
            content_type = 'application/json'
            filename = f'{name}.json'
        else:
            content = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
            content_type = 'application/x-yaml'
            filename = f'{name}.yaml'

        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
