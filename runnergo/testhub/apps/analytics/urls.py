from django.urls import path

from .views import AnalyticsEventIngestView

urlpatterns = [
    path('events/', AnalyticsEventIngestView.as_view(), name='analytics-events'),
]
