from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DatabaseDiagnosticsView,
    ExporterTargetView,
    PerformanceResultViewSet,
    PerformanceRuleViewSet,
    PrometheusReloadView,
    TestedDatabaseConfigView,
)


router = DefaultRouter()
router.register(r'rules', PerformanceRuleViewSet, basename='performance-rules')
router.register(r'results', PerformanceResultViewSet, basename='performance-results')

urlpatterns = [
    path('', include(router.urls)),
    path('exporters/', ExporterTargetView.as_view(), name='performance-exporters'),
    path('exporters/reload/', PrometheusReloadView.as_view(), name='performance-exporters-reload'),
    path('database/config/', TestedDatabaseConfigView.as_view(), name='performance-database-config'),
    path('database/diagnostics/', DatabaseDiagnosticsView.as_view(), name='performance-database-diagnostics'),
]
