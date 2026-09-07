from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import render
import requests
import uuid
from apps.projects.models import Project
from apps.core.outbound import validate_outbound_http_url

from .copilot_service import execute_copilot_task, run_copilot
from .models import AssistantSession, AssistantMessage, ChatMessage, DifyConfig, CopilotTask
from .serializers import (
    AssistantSessionSerializer, 
    AssistantSessionCreateSerializer,
    AssistantMessageSerializer,
    ChatMessageSerializer,
    CopilotTaskSerializer
)


class AssistantSessionViewSet(viewsets.ModelViewSet):
    """智能助手会话视图集"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return AssistantSessionCreateSerializer
        return AssistantSessionSerializer
    
    def get_queryset(self):
        return AssistantSession.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=True, methods=['post'])
    def add_message(self, request, pk=None):
        """添加消息到会话"""
        session = self.get_object()
        serializer = AssistantMessageSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(session=session)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """获取会话的聊天消息"""
        session = self.get_object()
        messages = session.chat_messages.all()
        serializer = ChatMessageSerializer(messages, many=True)
        return Response(serializer.data)


class ChatViewSet(viewsets.ViewSet):
    """聊天功能ViewSet"""
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['post'])
    def send_message(self, request):
        """发送消息到Dify API"""
        session_id = request.data.get('session_id')
        message = request.data.get('message')
        
        if not session_id or not message:
            return Response(
                {'error': 'session_id和message都是必填项'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 获取会话
        try:
            session = AssistantSession.objects.get(
                session_id=session_id,
                user=request.user
            )
        except AssistantSession.DoesNotExist:
            return Response(
                {'error': '会话不存在'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # 获取Dify配置
        dify_config = DifyConfig.get_active_config()
        if not dify_config:
            return Response(
                {'error': '未配置Dify API，请先在配置中心配置'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 保存用户消息
        user_message = ChatMessage.objects.create(
            session=session,
            role='user',
            content=message,
            conversation_id=session.conversation_id
        )
        
        try:
            # 调用Dify API
            headers = {
                'Authorization': f'Bearer {dify_config.get_api_key()}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'inputs': {},
                'query': message,
                'user': str(request.user.id),
                'response_mode': 'blocking'
            }
            
            # 如果有conversation_id，添加到请求中以保持会话连续性
            if session.conversation_id:
                payload['conversation_id'] = session.conversation_id
            
            # 去除URL末尾的斜杠
            api_url = dify_config.api_url.rstrip('/')
            
            request_url = validate_outbound_http_url(
                f'{api_url}/chat-messages',
                label='Dify API 地址',
            )
            response = requests.post(
                request_url,
                headers=headers,
                json=payload,
                timeout=60,
                allow_redirects=False,
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # 更新会话的conversation_id
                if 'conversation_id' in data and not session.conversation_id:
                    session.conversation_id = data['conversation_id']
                    session.save()
                
                # 保存助手回复
                assistant_message = ChatMessage.objects.create(
                    session=session,
                    role='assistant',
                    content=data.get('answer', ''),
                    conversation_id=data.get('conversation_id'),
                    message_id=data.get('message_id')
                )
                
                return Response({
                    'user_message': ChatMessageSerializer(user_message).data,
                    'assistant_message': ChatMessageSerializer(assistant_message).data,
                    'conversation_id': data.get('conversation_id')
                })
            else:
                return Response({
                    'error': f'Dify API错误: {response.status_code}'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
        except requests.exceptions.Timeout:
            return Response({
                'error': 'API请求超时'
            }, status=status.HTTP_408_REQUEST_TIMEOUT)
        except (requests.exceptions.RequestException, ValueError) as e:
            return Response({
                'error': f'API请求失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CopilotTaskViewSet(viewsets.ReadOnlyModelViewSet):
    """AI 测试 Copilot 编排任务。"""

    serializer_class = CopilotTaskSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CopilotTask.objects.filter(created_by=self.request.user).select_related(
            'project', 'test_plan', 'created_by'
        )

    @action(detail=False, methods=['post'])
    def plan(self, request):
        objective = (request.data.get('objective') or '').strip()
        project_id = request.data.get('project_id')
        if not objective:
            return Response({'error': '请输入测试目标'}, status=status.HTTP_400_BAD_REQUEST)

        if project_id:
            has_access = Project.objects.filter(id=project_id).filter(
                owner=request.user
            ).exists() or Project.objects.filter(id=project_id, members=request.user).exists()
            if not has_access:
                return Response({'error': '项目不存在或无权限访问'}, status=status.HTTP_403_FORBIDDEN)

        try:
            task = run_copilot(objective, request.user, project_id)
            return Response(CopilotTaskSerializer(task).data, status=status.HTTP_201_CREATED)
        except Exception as exc:
            task = CopilotTask.objects.create(
                task_id=f'COPILOT-FAILED-{uuid.uuid4().hex[:8].upper()}',
                objective=objective,
                status='failed',
                error_message=str(exc),
                created_by=request.user,
            )
            return Response(CopilotTaskSerializer(task).data, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        task = self.get_object()
        if task.status != 'completed':
            return Response({'error': 'Copilot 任务未完成，暂不能执行'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            task = execute_copilot_task(task, request.user)
            return Response(CopilotTaskSerializer(task).data)
        except Exception as exc:
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def assistant_view(request):
    """智能助手页面视图 - 用于iframe内嵌"""
    return render(request, 'assistant/assistant.html')
