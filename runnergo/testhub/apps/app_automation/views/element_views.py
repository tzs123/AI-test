# -*- coding: utf-8 -*-
"""APP元素管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse
from django.conf import settings
from pathlib import Path
from .test_case_views import AppPagination
import hashlib
import re
import logging

from ..models import AppElement, AppElementFolder
from ..access import accessible_project_assets
from ..serializers import AppElementFolderSerializer, AppElementSerializer
from ..utils.element_references import (
    find_test_case_references,
    group_references_by_element,
)
from ..utils.image_helpers import get_element_image_url

logger = logging.getLogger(__name__)


class AppElementFolderViewSet(viewsets.ModelViewSet):
    """元素文件夹管理。删除文件夹时元素由外键规则自动移到未分组。"""

    queryset = AppElementFolder.objects.all()
    serializer_class = AppElementFolderSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        element_filter = Q(elements__is_active=True)
        project_id = str(self.request.query_params.get('project') or '').strip()
        if project_id.isdigit():
            element_filter &= Q(elements__project_id=int(project_id))
        return (
            super()
            .get_queryset()
            .annotate(element_count=Count('elements', filter=element_filter, distinct=True))
            .order_by('sort_order', 'name', 'id')
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        folder_element_names = instance.elements.filter(
            is_active=True,
        ).values_list('name', flat=True)
        conflicts = list(
            AppElement.objects.filter(
                folder__isnull=True,
                is_active=True,
                name__in=folder_element_names,
            )
            .values_list('name', flat=True)
            .distinct()
            .order_by('name')
        )
        if conflicts:
            preview = '、'.join(conflicts[:5])
            suffix = ' 等' if len(conflicts) > 5 else ''
            return Response(
                {
                    'detail': (
                        f'无法删除文件夹：未分组中已存在同名元素 {preview}{suffix}。'
                        '请先重命名或移动这些元素。'
                    ),
                    'conflicting_names': conflicts,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


class AppElementViewSet(viewsets.ModelViewSet):
    """APP元素管理 ViewSet"""
    queryset = AppElement.objects.filter(is_active=True)
    serializer_class = AppElementSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    # ⚠️ 移除 SearchFilter，使用自定义搜索逻辑
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['element_type', 'is_active', 'project']
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @staticmethod
    def _reference_conflict_response(elements):
        element_list = list(elements)
        references = find_test_case_references(element.id for element in element_list)
        if not references:
            return None
        grouped = group_references_by_element(references)
        conflicts = [
            {
                'element_id': element.id,
                'element_name': element.name,
                'test_cases': grouped.get(element.id, []),
            }
            for element in element_list
            if element.id in grouped
        ]
        return Response(
            {
                'detail': '元素仍被测试用例引用，无法删除或覆盖，请先重新绑定用例。',
                'conflicts': conflicts,
            },
            status=status.HTTP_409_CONFLICT,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        conflict_response = self._reference_conflict_response([instance])
        if conflict_response:
            return conflict_response
        return super().destroy(request, *args, **kwargs)
    
    def perform_destroy(self, instance):
        """
        删除元素时同时删除物理文件
        """
        # 图片元素和语义/OCR 元素都可能保存备用模板图。
        if instance.config:
            image_path = instance.config.get('image_path')
            if image_path:
                try:
                    shared = AppElement.objects.filter(
                        config__image_path=image_path,
                        is_active=True,
                    ).exclude(pk=instance.pk).exists()
                    if shared:
                        logger.info(f"图片仍被其他元素引用，保留文件: {image_path}")
                        image_path = None

                    # 构造完整文件路径
                    template_base = self.get_template_base_path()
                    file_path = template_base / image_path if image_path else None
                    
                    # 删除文件
                    if file_path and file_path.exists():
                        file_path.unlink()
                        logger.info(f"删除图片文件: {file_path}")
                    elif file_path:
                        logger.warning(f"图片文件不存在: {file_path}")
                except Exception as e:
                    logger.error(f"删除图片文件失败: {str(e)}")
                    # 继续删除数据库记录，即使文件删除失败
        
        # 删除数据库记录
        instance.delete()
    
    def get_queryset(self):
        """
        自定义查询集，支持名称和标签搜索
        
        - 名称：模糊匹配（LIKE）
        - 标签：精确匹配 JSONField 数组中的元素
        
        安全修复：添加输入验证，防止 SQL 注入
        """
        queryset = accessible_project_assets(
            super().get_queryset(),
            self.request.user,
            project_lookup='project',
            unscoped_owner_lookup='created_by',
        )

        folder = str(self.request.query_params.get('folder') or '').strip().lower()
        if folder in {'unassigned', 'none', 'null', '0'}:
            queryset = queryset.filter(folder__isnull=True)
        elif folder.isdigit():
            queryset = queryset.filter(folder_id=int(folder))
        
        # 获取搜索关键词
        search = self.request.query_params.get('search', '').strip()
        if search:
            from django.db.models import Q
            from django.db import connection
            import json
            import re
            
            # 安全修复：限制搜索关键词长度和特殊字符
            if len(search) > 100:
                search = search[:100]  # 限制最大长度
            
            # 移除潜在危险的 SQL 特殊字符（额外防护层）
            # 注意：Django 的参数化查询已经提供了保护，这是额外的安全层
            search = re.sub(r"[';\"\\]", '', search)
            
            if connection.vendor == 'mysql':
                # MySQL: 使用 JSON_CONTAINS 查询
                search_json = json.dumps(search)  # "登录" → '"登录"'
                
                # 使用参数化查询，Django 会自动转义参数
                queryset = queryset.extra(
                    where=["name LIKE %s OR JSON_CONTAINS(tags, %s)"],
                    params=[f'%{search}%', search_json]
                )
            elif connection.vendor == 'sqlite':
                # SQLite: JSONField 不支持 contains lookup，使用 JSON1 的 json_each。
                queryset = queryset.extra(
                    where=[
                        '"app_elements"."name" LIKE %s '
                        'OR EXISTS ('
                        'SELECT 1 FROM json_each("app_elements"."tags") AS tag '
                        'WHERE tag.value = %s'
                        ')'
                    ],
                    params=[f'%{search}%', search],
                )
            elif connection.vendor == 'postgresql':
                # PostgreSQL: 使用 JSON contains 查询
                queryset = queryset.filter(
                    Q(name__icontains=search) | 
                    Q(tags__contains=[search])
                )
            else:
                # 其他数据库至少保证名称搜索可用。
                queryset = queryset.filter(name__icontains=search)
        
        return queryset

    @action(detail=False, methods=['post'], url_path='bulk-move')
    def bulk_move(self, request):
        """批量移动元素；目标文件夹存在同名元素时可确认覆盖。"""
        element_ids = request.data.get('element_ids')
        if not isinstance(element_ids, list) or not element_ids:
            return Response(
                {'detail': 'element_ids 必须是非空数组'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(element_ids) > 1000:
            return Response(
                {'detail': '单次最多移动 1000 个元素'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        normalized_ids = []
        for value in element_ids:
            try:
                normalized_ids.append(int(value))
            except (TypeError, ValueError):
                return Response(
                    {'detail': f'无效的元素 ID: {value}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        folder_id = request.data.get('folder')
        overwrite = request.data.get('overwrite', False)
        if isinstance(overwrite, str):
            overwrite = overwrite.strip().lower() in {'1', 'true', 'yes'}
        else:
            overwrite = bool(overwrite)

        target_folder = None
        if folder_id not in (None, '', 0, '0'):
            try:
                target_folder = AppElementFolder.objects.get(id=int(folder_id))
            except (TypeError, ValueError, AppElementFolder.DoesNotExist):
                return Response(
                    {'detail': '目标文件夹不存在'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        target_name = target_folder.name if target_folder else '未分组'
        overwritten_image_paths = []

        with transaction.atomic():
            queryset = self.get_queryset().select_for_update().filter(id__in=normalized_ids)
            elements = list(queryset.only('id', 'name', 'folder_id'))
            moving_ids = [element.id for element in elements]
            moving_names = [element.name for element in elements]

            duplicate_names = sorted({
                name for name in moving_names if moving_names.count(name) > 1
            })
            if duplicate_names:
                preview = '、'.join(duplicate_names[:5])
                suffix = ' 等' if len(duplicate_names) > 5 else ''
                return Response(
                    {
                        'detail': (
                            f'选中的元素中包含同名项 {preview}{suffix}，'
                            '请分批移动同名元素。'
                        ),
                        'conflicting_names': duplicate_names,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            conflicting_targets = list(
                AppElement.objects.select_for_update()
                .filter(
                    folder=target_folder,
                    is_active=True,
                    name__in=moving_names,
                )
                .exclude(id__in=moving_ids)
                .only('id', 'name', 'config')
                .order_by('name', 'id')
            )
            conflicts = sorted({element.name for element in conflicting_targets})
            if conflicts and not overwrite:
                preview = '、'.join(conflicts[:5])
                suffix = ' 等' if len(conflicts) > 5 else ''
                return Response(
                    {
                        'detail': (
                            f'文件夹“{target_name}”中已存在同名元素 {preview}{suffix}，'
                            '是否覆盖？'
                        ),
                        'conflicting_names': conflicts,
                        'requires_overwrite': True,
                        'folder': target_folder.id if target_folder else None,
                        'folder_name': target_name,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            overwritten_count = len(conflicting_targets)
            if conflicting_targets:
                conflict_response = self._reference_conflict_response(conflicting_targets)
                if conflict_response:
                    return conflict_response
                overwritten_image_paths = [
                    element.config.get('image_path')
                    for element in conflicting_targets
                    if element.config and element.config.get('image_path')
                ]
                AppElement.objects.filter(
                    id__in=[element.id for element in conflicting_targets]
                ).delete()

            moved = queryset.update(folder=target_folder)

            if overwritten_image_paths:
                transaction.on_commit(
                    lambda paths=tuple(overwritten_image_paths): self._cleanup_unreferenced_images(paths)
                )

        return Response({
            'moved_count': moved,
            'overwritten_count': overwritten_count,
            'folder': target_folder.id if target_folder else None,
            'folder_name': target_name,
        })

    def _cleanup_unreferenced_images(self, image_paths):
        """覆盖元素后清理已没有任何元素引用的旧模板图片。"""
        template_base = self.get_template_base_path()
        for image_path in set(filter(None, image_paths)):
            if AppElement.objects.filter(
                config__image_path=image_path,
                is_active=True,
            ).exists():
                continue
            file_path = template_base / image_path
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"覆盖元素后删除旧图片文件: {file_path}")
            except Exception as exc:
                logger.error(f"覆盖元素后删除旧图片文件失败: {exc}")
    
    def get_template_base_path(self):
        """
        获取模板基础路径
        参考 Smart AI Test 的实现：图片存放在 app 目录下的 Template 文件夹
        
        返回: apps/app_automation/Template/
        """
        # __file__ = .../views/element_views.py
        # .parent = .../views/
        # .parent.parent = .../app_automation/
        return Path(__file__).resolve().parent.parent / "Template"
    
    @action(detail=False, methods=['post'], url_path='upload')
    def upload_image(self, request):
        """
        上传元素图片
        
        功能：
        1. 接收图片文件上传
        2. 验证文件类型和大小
        3. 计算文件哈希
        4. 检测是否重复
        5. 保存到指定分类目录
        6. 返回图片路径和哈希值
        """
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({
                'code': 400,
                'msg': '未接收到文件',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # 安全修复：验证文件类型
        # Airtest 1.4.3 pins an OpenCV build whose bundled libwebp is vulnerable.
        # Do not persist attacker-supplied WebP templates that Airtest/OpenCV may decode.
        ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}
        ALLOWED_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/bmp'}
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        
        # 验证文件扩展名。设备截图生成的 File 可能没有后缀，这里按 MIME 类型补齐。
        import os as _os
        mime_extension_map = {
            'image/png': '.png',
            'image/jpeg': '.jpg',
            'image/gif': '.gif',
            'image/bmp': '.bmp',
        }
        original_filename = _os.path.basename(file_obj.name or 'template')
        _, ext = _os.path.splitext(original_filename)
        if not ext and getattr(file_obj, 'content_type', None) in mime_extension_map:
            ext = mime_extension_map[file_obj.content_type]
            original_filename = f"{original_filename}{ext}"
        if ext.lower() not in ALLOWED_EXTENSIONS:
            return Response({
                'code': 400,
                'msg': f'不支持的文件类型: {ext}，仅支持图片文件',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # 验证文件大小
        if file_obj.size > MAX_FILE_SIZE:
            return Response({
                'code': 400,
                'msg': f'文件大小超过限制（最大10MB），当前: {file_obj.size // (1024*1024)}MB',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # 验证 MIME 类型
        if hasattr(file_obj, 'content_type') and file_obj.content_type not in ALLOWED_MIME_TYPES:
            return Response({
                'code': 400,
                'msg': f'不支持的文件MIME类型: {file_obj.content_type}',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # 获取参数
        category = request.data.get('category', 'common')
        element_id = request.data.get('element_id')  # 编辑模式时传递，用于排除自身
        folder_id = request.data.get('folder')
        if folder_id in (None, '', 0, '0'):
            folder_id = None
        else:
            try:
                folder_id = int(folder_id)
                AppElementFolder.objects.get(id=folder_id)
            except (TypeError, ValueError, AppElementFolder.DoesNotExist):
                return Response({
                    'code': 400,
                    'msg': '所属文件夹不存在',
                    'success': False,
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # 安全修复：验证 category 参数，防止路径遍历
        if not re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', category):
            return Response({
                'code': 400,
                'msg': '分类名称只能包含字母、数字、下划线、中划线和中文',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # ✅ 业务逻辑内联：计算文件哈希
            file_obj.seek(0)
            hasher = hashlib.md5()
            for chunk in file_obj.chunks():
                hasher.update(chunk)
            file_hash = hasher.hexdigest()
            file_obj.seek(0)
            
            # ✅ 业务逻辑内联：检查是否重复（排除当前元素）
            query = AppElement.objects.filter(
                config__file_hash=file_hash,
                is_active=True,
                folder_id=folder_id,
            )
            if element_id:
                query = query.exclude(id=element_id)
            
            existing = query.first()
            
            if existing:
                return Response({
                    'code': 400,
                    'msg': '图片已存在',
                    'success': False,
                    'detail': f'该图片的哈希值为 {file_hash}，已被其他元素使用',
                    'suggestion': '建议使用当前文件夹内的现有元素、选择其他文件夹或上传不同的图片',
                    'data': {
                        'existing_element': {
                            'id': existing.id,
                            'name': existing.name,
                            'image_path': existing.config.get('image_path')
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # ✅ 业务逻辑内联：保存图片到 Template 目录
            template_base = self.get_template_base_path()
            category_path = template_base / category
            category_path.mkdir(parents=True, exist_ok=True)
            
            # 安全修复：使用安全的文件名，防止路径遍历
            # 移除文件名中的特殊字符
            safe_filename = re.sub(r'[^\w\.\-]', '_', original_filename)
            if not safe_filename or safe_filename in ('.', '..'):
                safe_filename = f"template{ext.lower()}"
            file_path = category_path / safe_filename

            if file_path.exists():
                with open(file_path, 'rb') as existing_file:
                    existing_hash = hashlib.md5(existing_file.read()).hexdigest()
                if existing_hash != file_hash:
                    stem, suffix = _os.path.splitext(safe_filename)
                    safe_filename = f"{stem}_{file_hash[:8]}{suffix or ext.lower()}"
                    file_path = category_path / safe_filename
            
            # 保存文件
            with open(file_path, 'wb+') as destination:
                for chunk in file_obj.chunks():
                    destination.write(chunk)
            
            # 构建相对路径（直接返回 category/filename.png）
            relative_path = f"{category}/{safe_filename}"
            
            logger.info(f"用户 {request.user.username} 上传图片: {relative_path}, 哈希: {file_hash}")
            
            return Response({
                'code': 0,
                'msg': '上传成功',
                'success': True,
                'data': {
                    'image_path': relative_path,
                    'file_hash': file_hash,
                    'url': get_element_image_url(relative_path)
                }
            })
        
        except Exception as e:
            logger.error(f"上传图片失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '上传失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['get'], url_path='image-categories')
    def image_categories(self, request):
        """
        获取图片分类列表
        
        返回所有可用的图片分类目录
        """
        try:
            # ✅ 业务逻辑内联：从 Template 目录获取分类列表
            template_base = self.get_template_base_path()
            
            if not template_base.exists():
                return Response({
                    'code': 0,
                    'msg': '获取成功',
                    'success': True,
                    'data': []
                })
            
            categories = []
            for item in template_base.iterdir():
                if item.is_dir():
                    # 计算目录下的图片数量
                    image_count = sum(1 for f in item.iterdir() if f.is_file() and f.suffix.lower() in ['.png', '.jpg', '.jpeg'])
                    
                    categories.append({
                        'name': item.name,
                        'count': image_count,
                        'path': str(item.relative_to(template_base))
                    })
            
            # 按名称排序
            categories.sort(key=lambda x: x['name'])
            
            return Response({
                'code': 0,
                'msg': '获取成功',
                'success': True,
                'data': categories
            })
        except Exception as e:
            logger.error(f"获取分类列表失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '操作失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'], url_path='image-categories/create')
    def create_image_category(self, request):
        """
        创建新的图片分类
        
        参数：
        - name: 分类名称（只能包含字母、数字、下划线、中划线）
        """
        category_name = request.data.get('name', '').strip()
        
        if not category_name:
            return Response({
                'code': 400,
                'msg': '分类名称不能为空',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # ✅ 业务逻辑内联：验证分类名称
        if not re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', category_name):
            return Response({
                'code': 400,
                'msg': '分类名称只能包含字母、数字、下划线、中划线和中文',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # ✅ 业务逻辑内联：在 Template 目录创建分类
            template_base = self.get_template_base_path()
            category_path = template_base / category_name
            
            if category_path.exists():
                return Response({
                    'code': 400,
                    'msg': '分类已存在',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
            
            category_path.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"用户 {request.user.username} 创建图片分类: {category_name}")
            
            return Response({
                'code': 0,
                'msg': '创建成功',
                'success': True,
                'data': {
                    'name': category_name
                }
            })
        except Exception as e:
            logger.error(f"创建分类失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '操作失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['delete'], url_path='image-categories/(?P<name>[^/.]+)')
    def delete_image_category(self, request, name=None):
        """
        删除图片分类（仅删除空目录）
        
        参数：
        - name: 分类名称
        """
        if not name:
            return Response({
                'code': 400,
                'msg': '分类名称不能为空',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # ✅ 业务逻辑内联：从 Template 目录删除分类
            template_base = self.get_template_base_path()
            category_path = template_base / name
            
            if not category_path.exists():
                return Response({
                    'code': 404,
                    'msg': '分类不存在',
                    'success': False
                }, status=status.HTTP_404_NOT_FOUND)
            
            # 检查是否为空目录
            if any(category_path.iterdir()):
                return Response({
                    'code': 400,
                    'msg': '分类不为空，无法删除。请先删除分类下的所有图片。',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 删除空目录
            category_path.rmdir()
            
            logger.info(f"用户 {request.user.username} 删除图片分类: {name}")
            
            return Response({
                'code': 0,
                'msg': '删除成功',
                'success': True
            })
        except Exception as e:
            logger.error(f"删除分类失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '操作失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['get'], url_path='preview')
    def preview(self, request, pk=None):
        """
        获取元素图片预览
        
        返回图片文件（用于前端显示）
        """
        element = self.get_object()
        
        if not (element.config or {}).get('image_path'):
            return Response({
                'code': 400,
                'msg': '该元素没有备用图片',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # 获取图片路径
        image_path = element.config.get('image_path')
        
        if not image_path:
            return Response({
                'code': 404,
                'msg': '图片路径不存在',
                'success': False
            }, status=status.HTTP_404_NOT_FOUND)
        
        # ✅ 业务逻辑内联：从 Template 目录构造完整文件路径
        template_base = self.get_template_base_path()
        file_path = template_base / image_path
        
        if not file_path.exists():
            return Response({
                'code': 404,
                'msg': '图片文件不存在',
                'success': False
            }, status=status.HTTP_404_NOT_FOUND)
        
        try:
            return FileResponse(open(file_path, 'rb'), content_type='image/png')
        except Exception as e:
            logger.error(f"读取图片失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '读取图片失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'], url_path='crop-image')
    def crop_image(self, request):
        """
        裁剪图片并保存
        
        参数：
        - image_data: Base64 图片数据
        - x, y, width, height: 裁剪区域坐标
        - element_name: 元素名称
        - category: 图片分类
        - element_type: 元素类型（image/pos/region）
        
        返回：
        - 裁剪后的图片路径
        - 文件哈希
        - 坐标信息
        """
        try:
            from PIL import Image
            import io
            import base64
            import time
            
            # 获取参数
            image_data = request.data.get('image_data', '')
            x = int(request.data.get('x', 0))
            y = int(request.data.get('y', 0))
            width = int(request.data.get('width', 100))
            height = int(request.data.get('height', 100))
            element_name = request.data.get('element_name', 'captured_element')
            category = request.data.get('category', 'common')
            element_type = request.data.get('element_type', 'image')  # image/pos/region
            folder_id = request.data.get('folder')
            if folder_id in (None, '', 0, '0'):
                folder_id = None
            else:
                try:
                    folder_id = int(folder_id)
                    AppElementFolder.objects.get(id=folder_id)
                except (TypeError, ValueError, AppElementFolder.DoesNotExist):
                    return Response({
                        'code': 400,
                        'msg': '所属文件夹不存在',
                        'success': False,
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            # 安全修复：验证 category 参数，防止路径遍历
            if not re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', category):
                return Response({
                    'code': 400,
                    'msg': '分类名称只能包含字母、数字、下划线、中划线和中文',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 安全修复：验证 element_type 参数
            if element_type not in ('image', 'pos', 'region'):
                element_type = 'image'
            
            # 安全修复：限制图片数据大小（防止内存耗尽攻击）
            MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB base64
            if len(image_data) > MAX_IMAGE_SIZE:
                return Response({
                    'code': 400,
                    'msg': '图片数据过大，请上传较小的图片',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 解码 Base64 图片
            if image_data.startswith('data:image'):
                image_data = image_data.split(',')[1]
            
            image_bytes = base64.b64decode(image_data)
            image = Image.open(io.BytesIO(image_bytes))
            
            # 裁剪图片
            cropped = image.crop((x, y, x + width, y + height))
            
            # 保存到临时缓冲区
            buffer = io.BytesIO()
            cropped.save(buffer, format='PNG')
            buffer.seek(0)
            
            # 计算哈希
            file_hash = hashlib.md5(buffer.getvalue()).hexdigest()
            buffer.seek(0)
            
            # 检查重复
            existing = AppElement.objects.filter(
                config__file_hash=file_hash,
                is_active=True,
                folder_id=folder_id,
            ).first()
            
            if existing:
                return Response({
                    'code': 400,
                    'msg': '该图片已存在',
                    'success': False,
                    'data': {
                        'existing_element': {
                            'id': existing.id,
                            'name': existing.name,
                            'image_path': existing.config.get('image_path')
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 保存文件（统一使用 Template 目录）
            base_path = self.get_template_base_path()
            category_path = base_path / category
            category_path.mkdir(parents=True, exist_ok=True)
            
            # 安全修复：使用安全的文件名
            safe_element_name = re.sub(r'[^\w\.\-]', '_', element_name)
            filename = f"{safe_element_name}_{int(time.time())}.png"
            file_path = category_path / filename
            
            with open(file_path, 'wb') as f:
                f.write(buffer.getvalue())
            
            # 构建相对路径
            relative_path = f"{category}/{filename}"
            
            logger.info(f"用户 {request.user.username} 裁剪图片: {relative_path}, 哈希: {file_hash}")
            
            return Response({
                'code': 0,
                'msg': '裁剪成功',
                'success': True,
                'data': {
                    'image_path': relative_path,
                    'file_hash': file_hash,
                    'url': get_element_image_url(relative_path),
                    'coordinates': {
                        'x': x,
                        'y': y,
                        'width': width,
                        'height': height
                    },
                    'element_type': element_type
                }
            })
            
        except Exception as e:
            logger.error(f"裁剪图片失败: {str(e)}")
            return Response({
                'code': 500,
                'msg': '裁剪失败，请稍后重试',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
