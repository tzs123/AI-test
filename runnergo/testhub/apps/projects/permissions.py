from rest_framework.permissions import BasePermission, SAFE_METHODS

from .models import ProjectMember
from apps.users.local_mode import local_trusted_mode


class ProjectScopedPermission(BasePermission):
    """项目资源的最小权限策略，避免仅凭登录态访问跨项目数据。"""

    message = '无权访问或修改该项目资源。'

    def has_permission(self, request, view):
        return local_trusted_mode() or bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        project = obj if hasattr(obj, 'owner_id') else getattr(obj, 'project', None)
        if project is None:
            return False
        if project.owner_id == request.user.pk:
            return True
        member = ProjectMember.objects.filter(
            project_id=project.pk, user_id=request.user.pk
        ).first()
        if not member:
            return False
        if request.method in SAFE_METHODS:
            return True
        return member.role in {'owner', 'admin'}
