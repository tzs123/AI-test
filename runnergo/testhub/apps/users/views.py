from rest_framework import generics, status, permissions
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.response import Response
from django.contrib.auth import logout
from django.views.decorators.csrf import csrf_exempt
from .models import User
from .serializers import UserSerializer
from .permissions import UserManagementPermission
from .local_mode import local_trusted_mode

# JWT 相关导入
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

import os
import requests


# RunnerGo 的权限服务使用角色名称描述平台权限，而 TestHub 的全局配置
# 接口使用 Django 的 is_staff/is_superuser 作为统一的管理员权限判断。
# SSO 用户不能只创建成普通 Django 用户，否则登录成功后会看不到配置中心，
# 且直接访问配置接口也会被拒绝。
_SUPER_ADMIN_ROLE_NAMES = {
    '超管',
    '超级管理员',
    '系统超级管理员',
    'super admin',
    'superadmin',
    'super_admin',
    'root',
}
_ADMIN_ROLE_NAMES = {
    '管理员',
    '企业管理员',
    '团队管理员',
    'admin',
    'administrator',
}


def _runnergo_admin_flags(data):
    """将 RunnerGo 角色映射为 TestHub 的平台管理员标记。"""
    user_info = data.get('user_info') or {}
    role_names = [user_info.get('role_name')]
    role_names.extend(
        role.get('role_name')
        for role in (data.get('team_list') or [])
        if isinstance(role, dict)
    )

    normalized_roles = {
        str(role).strip().lower()
        for role in role_names
        if role
    }
    is_superuser = bool(
        normalized_roles & {role.lower() for role in _SUPER_ADMIN_ROLE_NAMES}
    )
    is_staff = is_superuser or bool(
        normalized_roles & {role.lower() for role in _ADMIN_ROLE_NAMES}
    )
    return is_staff, is_superuser


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_user(request):
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def runnergo_sso_view(request):
    """使用 RunnerGo 登录态换取 TestHub JWT。"""
    runnergo_token = (
        request.COOKIES.get('token') or
        request.headers.get('authorization') or
        request.headers.get('Authorization') or
        ''
    ).strip()
    if runnergo_token.lower().startswith('bearer '):
        runnergo_token = runnergo_token[7:].strip()

    if not runnergo_token:
        return Response({'error': '未检测到 RunnerGo 登录态'}, status=status.HTTP_401_UNAUTHORIZED)

    permission_url = os.getenv(
        'RUNNERGO_PERMISSION_USER_URL',
        'http://permission:30000/permission/api/v1/user/get'
    )
    try:
        permission_response = requests.get(
            permission_url,
            headers={'authorization': runnergo_token},
            timeout=5
        )
        permission_response.raise_for_status()
        permission_data = permission_response.json()
    except requests.RequestException as exc:
        return Response({
            'error': 'RunnerGo 登录态校验失败',
            'detail': str(exc)
        }, status=status.HTTP_401_UNAUTHORIZED)
    except ValueError:
        return Response({'error': 'RunnerGo 登录态校验返回无效'}, status=status.HTTP_401_UNAUTHORIZED)

    if permission_data.get('code') != 0:
        return Response({
            'error': permission_data.get('et') or permission_data.get('em') or 'RunnerGo 登录态无效'
        }, status=status.HTTP_401_UNAUTHORIZED)

    data = permission_data.get('data') or {}
    user_info = data.get('user_info') or {}
    user_related = data.get('user_related') or {}
    runnergo_is_staff, runnergo_is_superuser = _runnergo_admin_flags(data)
    username = (
        user_info.get('account') or
        user_info.get('nickname') or
        user_info.get('user_id') or
        'runnergo'
    )
    email = user_info.get('email') or f'{username}@runnergo.local'

    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
            'first_name': user_info.get('nickname') or username,
            'is_active': True,
            'is_staff': runnergo_is_staff,
            'is_superuser': runnergo_is_superuser,
        }
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    else:
        fields_to_update = []
        if email and user.email != email:
            user.email = email
            fields_to_update.append('email')
        nickname = user_info.get('nickname') or username
        if nickname and user.first_name != nickname:
            user.first_name = nickname
            fields_to_update.append('first_name')
        if not user.is_active:
            user.is_active = True
            fields_to_update.append('is_active')
        # 只提升由 RunnerGo 权限服务确认的管理员，不因一次异常/缺字段的
        # 权限响应撤销已有本地管理员权限。
        if runnergo_is_staff and not user.is_staff:
            user.is_staff = True
            fields_to_update.append('is_staff')
        if runnergo_is_superuser and not user.is_superuser:
            user.is_superuser = True
            fields_to_update.append('is_superuser')
        if fields_to_update:
            user.save(update_fields=fields_to_update)

    refresh = RefreshToken.for_user(user)
    return Response({
        'user': UserSerializer(user).data,
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'runnergo': {
            'team_id': user_related.get('setting_team_id'),
            'company_id': user_related.get('company_id'),
            'company_name': user_related.get('company_name'),
        },
        'message': 'RunnerGo SSO 登录成功',
    })


@api_view(['POST'])
@authentication_classes([JWTAuthentication, TokenAuthentication])
@csrf_exempt
def logout_view(request):
    """用户退出登录，将refresh token加入黑名单"""
    if request.user.is_authenticated:
        # 将 refresh token 加入黑名单
        refresh_token = request.data.get('refresh')
        if refresh_token:
            try:
                from rest_framework_simplejwt.tokens import RefreshToken as JWTRefreshToken
                JWTRefreshToken(refresh_token).blacklist()
            except Exception:
                pass

        logout(request)

    return Response({'message': '退出成功'})

@api_view(['GET'])
def profile_view(request):
    if not request.user.is_authenticated:
        return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
    
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

class UserListView(generics.ListCreateAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserSerializer
    permission_classes = [UserManagementPermission]

    def check_permissions(self, request):
        super().check_permissions(request)
        if not (local_trusted_mode() or request.user.is_staff or request.user.is_superuser):
            self.permission_denied(request, message=UserManagementPermission.message)

class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserSerializer
    permission_classes = [UserManagementPermission]

    def get_queryset(self):
        queryset = super().get_queryset()
        if local_trusted_mode() or self.request.user.is_staff or self.request.user.is_superuser:
            return queryset
        return queryset.filter(pk=self.request.user.pk)
