from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    RequirementDocumentViewSet,
    PrototypeUnderstandingAssetViewSet,
    RequirementAnalysisViewSet,
    BusinessRequirementViewSet,
    GeneratedTestCaseViewSet,
    AnalysisTaskViewSet,
    AIModelConfigViewSet,
    TestSkillViewSet,
    GenerationConfigViewSet,
    BusinessRuleKnowledgeViewSet,
    BusinessWorkflowViewSet,
    KnowledgeGraphView,
    KnowledgeNodeViewSet,
    KnowledgeRelationViewSet,
    TestCaseGenerationTaskViewSet,
    ConfigStatusViewSet,
    upload_and_analyze,
    analyze_text
)

# 创建DRF路由器
router = DefaultRouter()
router.register(r'documents', RequirementDocumentViewSet, basename='requirementdocument')
router.register(r'prototype-understanding', PrototypeUnderstandingAssetViewSet, basename='prototypeunderstanding')
router.register(r'analyses', RequirementAnalysisViewSet, basename='requirementanalysis')
router.register(r'requirements', BusinessRequirementViewSet, basename='businessrequirement')
router.register(r'test-cases', GeneratedTestCaseViewSet, basename='generatedtestcase')
router.register(r'tasks', AnalysisTaskViewSet, basename='analysistask')
router.register(r'ai-models', AIModelConfigViewSet, basename='aimodelconfig')
router.register(r'skills', TestSkillViewSet, basename='testskill')
router.register(r'generation-config', GenerationConfigViewSet, basename='generationconfig')
router.register(r'business-rules', BusinessRuleKnowledgeViewSet, basename='businessruleknowledge')
router.register(r'workflows', BusinessWorkflowViewSet, basename='businessworkflow')
router.register(r'knowledge/nodes', KnowledgeNodeViewSet, basename='knowledgenode')
router.register(r'knowledge/relations', KnowledgeRelationViewSet, basename='knowledgerelation')
router.register(r'testcase-generation', TestCaseGenerationTaskViewSet, basename='testcasegenerationtask')
router.register(r'config', ConfigStatusViewSet, basename='configstatus')

app_name = 'requirement_analysis'

urlpatterns = [
    # 显式注册 export_excel 路由（必须在 DRF router 之前，
    # 否则 GET /business-rules/export_excel/ 会被 detail 路由的 <pk> 匹配）
    path(
        'business-rules/export_excel/',
        BusinessRuleKnowledgeViewSet.as_view({'get': 'export_excel'}),
        name='businessruleknowledge-export-excel',
    ),

    # DRF路由
    path('', include(router.urls)),
    path('knowledge/graph/', KnowledgeGraphView.as_view(), name='knowledge-graph'),

    # 特殊API端点
    path('upload-and-analyze/', upload_and_analyze, name='upload-and-analyze'),
    path('analyze-text/', analyze_text, name='analyze-text'),
]
