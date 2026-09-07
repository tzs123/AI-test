from rest_framework import serializers
from .models import AssistantSession, AssistantMessage, DifyConfig, ChatMessage, CopilotTask


class DifyConfigSerializer(serializers.ModelSerializer):
    api_key_masked = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DifyConfig
        fields = ['id', 'api_url', 'api_key', 'api_key_masked', 'is_active', 'created_at', 'updated_at']
        extra_kwargs = {
            'api_key': {'write_only': True}  # Don't expose API key in responses
        }

    def get_api_key_masked(self, obj):
        value = obj.get_api_key()
        if not value:
            return ''
        if len(value) <= 7:
            return '*' * len(value)
        return f'{value[:3]}{"*" * (len(value) - 7)}{value[-4:]}'

    def validate_api_url(self, value):
        from apps.core.outbound import validate_outbound_http_url

        try:
            return validate_outbound_http_url(value, resolve=False, label='Dify API 地址')
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'content', 'conversation_id', 'message_id', 'created_at']
        read_only_fields = ['conversation_id', 'message_id', 'created_at']


class AssistantMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssistantMessage
        fields = ['id', 'message_type', 'content', 'created_at']


class AssistantSessionSerializer(serializers.ModelSerializer):
    messages = AssistantMessageSerializer(many=True, read_only=True)
    chat_messages = ChatMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = AssistantSession
        fields = ['id', 'session_id', 'conversation_id', 'title', 'created_at', 'updated_at', 'messages', 'chat_messages']


class AssistantSessionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssistantSession
        fields = ['session_id', 'title']
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class CopilotTaskSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    test_plan_name = serializers.CharField(source='test_plan.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = CopilotTask
        fields = [
            'id', 'task_id', 'objective', 'project', 'project_name', 'status',
            'status_display', 'analysis', 'test_strategy', 'created_assets',
            'test_plan', 'test_plan_name', 'summary', 'error_message',
            'created_by_name', 'created_at', 'updated_at', 'completed_at'
        ]
        read_only_fields = fields
