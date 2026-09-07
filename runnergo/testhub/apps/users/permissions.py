from rest_framework.permissions import BasePermission, SAFE_METHODS
from .local_mode import local_trusted_mode


class UserManagementPermission(BasePermission):
    """平台用户管理：管理员管理全部用户，普通用户只能查看自己。"""

    message = '仅平台管理员可以管理用户，普通用户只能查看自己的账户。'

    def has_permission(self, request, view):
        return local_trusted_mode() or bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if local_trusted_mode() or request.user.is_staff or request.user.is_superuser:
            return True
        return request.method in SAFE_METHODS and obj.pk == request.user.pk
