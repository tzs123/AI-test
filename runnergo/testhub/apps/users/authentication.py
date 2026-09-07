from threading import RLock

from rest_framework.authentication import BaseAuthentication

from .local_mode import local_trusted_mode
from .models import User


_local_principal_lock = RLock()


def get_local_principal():
    """Return the local principal and attach it to every project as an admin member.

    This removes user/role friction while keeping every existing project and module
    queryset scoped by an explicit project relationship.
    """
    from apps.projects.models import Project, ProjectMember

    # Authentication runs for every API request. Keep the steady-state path
    # read-only so concurrent requests do not contend for SQLite's global
    # writer lock. The process lock only serializes the rare bootstrap/sync
    # path when the local principal or a new project membership is missing.
    with _local_principal_lock:
        user, _ = User.objects.get_or_create(
            username='local-runnergo',
            defaults={
                'email': 'local-runnergo@localhost',
                'first_name': 'Local RunnerGo',
                'is_active': True,
                'is_staff': True,
                'is_superuser': False,
            },
        )
        changed = []
        if not user.is_active:
            user.is_active = True
            changed.append('is_active')
        if not user.is_staff:
            user.is_staff = True
            changed.append('is_staff')
        if user.has_usable_password():
            user.set_unusable_password()
            changed.append('password')
        if changed:
            user.save(update_fields=changed)

        project_ids = list(Project.objects.values_list('id', flat=True))
        existing = set(
            ProjectMember.objects.filter(
                user=user,
                project_id__in=project_ids,
            ).values_list('project_id', flat=True)
        )
        missing = [project_id for project_id in project_ids if project_id not in existing]
        if missing:
            ProjectMember.objects.bulk_create(
                [
                    ProjectMember(project_id=project_id, user=user, role='admin')
                    for project_id in missing
                ],
                ignore_conflicts=True,
            )

        non_admin_memberships = ProjectMember.objects.filter(user=user).exclude(role='admin')
        if non_admin_memberships.exists():
            non_admin_memberships.update(role='admin')
        return user


class LocalTrustedAuthentication(BaseAuthentication):
    def authenticate(self, request):
        if not local_trusted_mode():
            return None
        return get_local_principal(), None
