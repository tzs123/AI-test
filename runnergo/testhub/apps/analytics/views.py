import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AnalyticsEvent
from .serializers import AnalyticsEventCreateSerializer

logger = logging.getLogger(__name__)


def get_client_ip(request):
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class AnalyticsEventIngestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = request.data
        if isinstance(payload, dict) and isinstance(payload.get('events'), list):
            events = payload['events']
        elif isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            events = [payload]
        else:
            return Response({'detail': '无效的埋点数据格式'}, status=status.HTTP_400_BAD_REQUEST)

        if not events:
            return Response({'detail': '埋点数据不能为空'}, status=status.HTTP_400_BAD_REQUEST)

        max_batch_size = max(1, settings.ANALYTICS_MAX_BATCH_SIZE)
        if len(events) > max_batch_size:
            return Response(
                {'detail': f'单次最多允许上报 {max_batch_size} 条埋点事件'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AnalyticsEventCreateSerializer(data=events, many=True)
        serializer.is_valid(raise_exception=True)

        user = request.user if getattr(request.user, 'is_authenticated', False) else None
        ip_address = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:512]

        instances = [
            AnalyticsEvent(
                user=user,
                ip_address=ip_address,
                user_agent=user_agent,
                **item,
            )
            for item in serializer.validated_data
        ]

        try:
            AnalyticsEvent.objects.bulk_create(instances)
        except Exception:
            logger.exception('写入埋点事件失败')
            return Response({'detail': '写入埋点事件失败'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'created': len(instances)}, status=status.HTTP_201_CREATED)
