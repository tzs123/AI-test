from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('requirements', views.TestAssetRequirementViewSet, basename='test-asset-requirements')
router.register('apis', views.ApiAssetViewSet, basename='test-asset-apis')
router.register('pages', views.PageAssetViewSet, basename='test-asset-pages')
router.register('automation-scripts', views.AutomationScriptAssetViewSet, basename='test-asset-automation-scripts')
router.register('defects', views.DefectAssetViewSet, basename='test-asset-defects')
router.register('links', views.TestAssetLinkViewSet, basename='test-asset-links')

urlpatterns = [
    path('', include(router.urls)),
    path('dashboard/', views.dashboard, name='test-asset-dashboard'),
    path('options/', views.options, name='test-asset-options'),
]
