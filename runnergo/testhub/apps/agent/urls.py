from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgentAssetGenerateAPIView,
    AgentExplorerAPIView,
    AgentFailureAnalyzeAPIView,
    AgentFullCycleAPIView,
    AgentKnowledgeAPIView,
    AgentMemoryAPIView,
    AgentPlanAPIView,
    AgentQualityScoreAPIView,
    AgentRagSearchAPIView,
    AgentSelfHealAPIView,
    AgentTaskViewSet,
    AgentToolsAPIView,
)

router = DefaultRouter()
router.register(r'tasks', AgentTaskViewSet, basename='agent-task')

urlpatterns = [
    path('', include(router.urls)),
    path('plan/', AgentPlanAPIView.as_view(), name='agent-plan'),
    path('v2/plan/', AgentPlanAPIView.as_view(), name='agent-plan-v2'),
    path('tools/', AgentToolsAPIView.as_view(), name='agent-tools'),
    path('full-cycle/', AgentFullCycleAPIView.as_view(), name='agent-full-cycle'),
    path('analyze-failure/', AgentFailureAnalyzeAPIView.as_view(), name='agent-analyze-failure'),
    path('v2/analyze/', AgentFailureAnalyzeAPIView.as_view(), name='agent-analyze-failure-v2'),
    path('v2/full-cycle/', AgentFullCycleAPIView.as_view(), name='agent-full-cycle-v2'),
    path('v2/explorer/', AgentExplorerAPIView.as_view(), name='agent-explorer'),
    path('v2/memory/', AgentMemoryAPIView.as_view(), name='agent-memory'),
    path('v2/knowledge/', AgentKnowledgeAPIView.as_view(), name='agent-knowledge'),
    path('v2/rag/search/', AgentRagSearchAPIView.as_view(), name='agent-rag-search'),
    path('v2/assets/generate/', AgentAssetGenerateAPIView.as_view(), name='agent-assets-generate'),
    path('v2/self-heal/', AgentSelfHealAPIView.as_view(), name='agent-self-heal'),
    path('v2/quality-score/', AgentQualityScoreAPIView.as_view(), name='agent-quality-score'),
]
