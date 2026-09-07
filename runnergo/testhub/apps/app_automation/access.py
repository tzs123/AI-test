"""APP 自动化项目资产的统一可见范围。"""

from django.db.models import Q
from apps.users.local_mode import local_trusted_mode


def is_platform_admin(user):
    return bool(
        user and user.is_authenticated
        and (local_trusted_mode() or user.is_staff or user.is_superuser)
    )


def can_access_app_project(user, project):
    if not user or not user.is_authenticated or project is None:
        return False
    if is_platform_admin(user) or project.owner_id == user.pk:
        return True
    return project.members.filter(pk=user.pk).exists()


def accessible_app_projects(queryset, user):
    if is_platform_admin(user):
        return queryset
    return queryset.filter(Q(owner=user) | Q(members=user)).distinct()


def accessible_project_assets(
    queryset,
    user,
    *,
    project_lookup='project',
    unscoped_owner_lookup=None,
):
    """按 project.owner/members 过滤资产；历史无项目资产仅对创建者可见。"""
    if is_platform_admin(user):
        return queryset
    access_filter = (
        Q(**{f'{project_lookup}__owner': user})
        | Q(**{f'{project_lookup}__members': user})
    )
    if unscoped_owner_lookup:
        access_filter |= (
            Q(**{f'{project_lookup}__isnull': True})
            & Q(**{unscoped_owner_lookup: user})
        )
    return queryset.filter(access_filter).distinct()
