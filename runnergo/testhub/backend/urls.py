from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from apps.requirement_analysis.views import (
    KnowledgeGraphView,
    KnowledgeNodeViewSet,
    KnowledgeRelationViewSet,
)
from apps.data_factory.views import TestDataGenerateAPIView
from apps.agent.views import (
    BrowserAgentRunAPIView,
    DataAgentGenerateFromAPIAPIView,
    SecurityAgentScanAPIView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', lambda request: JsonResponse({'status': 'ok'}), name='health'),
    
    path('api/auth/', include('apps.users.urls')),
    path('api/projects/', include('apps.projects.urls')),
    path('api/testcases/', include('apps.testcases.urls')),
    path('api/testsuites/', include('apps.testsuites.urls')),
    path('api/executions/', include('apps.executions.urls')),
    path('api/reports/', include('apps.reports.urls')),
    path('api/reviews/', include('apps.reviews.urls')),
    path('api/versions/', include('apps.versions.urls')),
    path('api/assistant/', include('apps.assistant.urls')),
    path('api/users/', include('apps.users.urls')),
    path('api/requirement-analysis/', include('apps.requirement_analysis.urls')),
    path('api/agent/', include('apps.agent.urls')),
    path('api/e2e/', include('apps.agent.e2e_urls')),
    path('api/data-agent/generate-from-api', DataAgentGenerateFromAPIAPIView.as_view(), name='data-agent-generate-from-api'),
    path('api/browser-agent/run', BrowserAgentRunAPIView.as_view(), name='browser-agent-run'),
    path('api/security-agent/scan', SecurityAgentScanAPIView.as_view(), name='security-agent-scan'),
    path('api/test-data/generate', TestDataGenerateAPIView.as_view(), name='test-data-generate'),
    path('api/knowledge/graph/', KnowledgeGraphView.as_view(), name='knowledge-graph'),
    path('api/knowledge/nodes/', KnowledgeNodeViewSet.as_view({'get': 'list', 'post': 'create'}), name='knowledge-node-list'),
    path('api/knowledge/nodes/batch-delete/', KnowledgeNodeViewSet.as_view({'post': 'batch_delete'}), name='knowledge-node-batch-delete'),
    path('api/knowledge/nodes/<int:pk>/', KnowledgeNodeViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update', 'delete': 'destroy'}), name='knowledge-node-detail'),
    path('api/knowledge/relations/', KnowledgeRelationViewSet.as_view({'get': 'list', 'post': 'create'}), name='knowledge-relation-list'),
    path('api/knowledge/relations/<int:pk>/', KnowledgeRelationViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update', 'delete': 'destroy'}), name='knowledge-relation-detail'),
    path('api/app-automation/', include('apps.app_automation.urls')),  # APP自动化测试
    path('api/sql-console/', include('apps.sql_console.urls')),  # SQL 控制台（多条 SQL 执行）
    path('api/core/', include('apps.core.urls')),
    path('api/data-factory/', include('apps.data_factory.urls')),
    path('api/test-data/', include('apps.data_factory.urls')),
    path('api/test-assets/', include('apps.test_assets.urls')),
    path('api/performance-rule/', include('apps.performance_rule.urls')),
]

if settings.ANALYTICS_ENABLED:
    urlpatterns.append(path('api/analytics/', include('apps.analytics.urls')))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_FILES_URL, document_root=settings.STATIC_FILES_ROOT)

if settings.EXPOSE_API_DOCS:
    urlpatterns += [
        path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
        path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
        path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    ]
