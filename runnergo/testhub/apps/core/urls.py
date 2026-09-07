"""
Core 应用路由
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    RuntimeDataSetViewSet,
    RuntimeDataTemplateViewSet,
    RuntimeExecutionSnapshotViewSet,
    BulkTestDataJobViewSet,
    TestDataAssetLeaseViewSet,
    TestDataDecisionLogViewSet,
    TestDataAssetRequirementViewSet,
    TestDataAssetViewSet,
)

router = DefaultRouter()
router.register(r'runtime-data-templates', RuntimeDataTemplateViewSet, basename='runtime-data-template')
router.register(r'runtime-data-sets', RuntimeDataSetViewSet, basename='runtime-data-set')
router.register(r'runtime-execution-snapshots', RuntimeExecutionSnapshotViewSet, basename='runtime-execution-snapshot')
router.register(r'bulk-test-data-jobs', BulkTestDataJobViewSet, basename='bulk-test-data-job')
router.register(r'test-data-assets', TestDataAssetViewSet, basename='test-data-asset')
router.register(r'test-data-asset-requirements', TestDataAssetRequirementViewSet, basename='test-data-asset-requirement')
router.register(r'test-data-asset-leases', TestDataAssetLeaseViewSet, basename='test-data-asset-lease')
router.register(r'test-data-decision-logs', TestDataDecisionLogViewSet, basename='test-data-decision-log')

urlpatterns = [
    path('', include(router.urls)),
]
