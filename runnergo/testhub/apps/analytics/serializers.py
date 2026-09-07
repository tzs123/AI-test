from rest_framework import serializers


class AnalyticsEventCreateSerializer(serializers.Serializer):
    event_name = serializers.CharField(max_length=100)
    event_type = serializers.CharField(max_length=32, default='custom', required=False)
    module = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    page_path = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    route_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    referrer_path = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    target_path = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    success = serializers.BooleanField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(required=False, allow_null=True, min_value=0, default=None)
    session_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    metadata = serializers.DictField(required=False, default=dict)
