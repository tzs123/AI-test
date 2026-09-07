# -*- coding: utf-8 -*-
from django.urls import path, include
from .views.execution_views import serve_report_file, serve_public_report_file
from .views.evidence_views import serve_public_evidence_file
from .views.template_views import serve_public_template_file
from rest_framework.routers import DefaultRouter

from .views import (
    AppProjectViewSet,
    AppConfigViewSet,
    AppDeviceViewSet,
    AppElementFolderViewSet,
    AppElementViewSet,
    AppComponentViewSet,
    AppCustomComponentViewSet,
    AppComponentPackageViewSet,
    AppPackageViewSet,
    AppTestCaseViewSet,
    AppTestSuiteViewSet,
    AppScheduledTaskViewSet,
    AppNotificationLogViewSet,
    AppTestExecutionViewSet,
    AppDashboardViewSet,
)

router = DefaultRouter()

# 注册ViewSets
router.register(r'projects', AppProjectViewSet, basename='app-project')
router.register(r'config', AppConfigViewSet, basename='app-config')
router.register(r'dashboard', AppDashboardViewSet, basename='app-dashboard')
router.register(r'devices', AppDeviceViewSet, basename='app-device')
router.register(r'element-folders', AppElementFolderViewSet, basename='app-element-folder')
router.register(r'elements', AppElementViewSet, basename='app-element')
router.register(r'components', AppComponentViewSet, basename='app-component')
router.register(r'custom-components', AppCustomComponentViewSet, basename='app-custom-component')
router.register(r'component-packages', AppComponentPackageViewSet, basename='app-component-package')
router.register(r'packages', AppPackageViewSet, basename='app-package')
router.register(r'test-cases', AppTestCaseViewSet, basename='app-test-case')
router.register(r'test-suites', AppTestSuiteViewSet, basename='app-test-suite')
router.register(r'scheduled-tasks', AppScheduledTaskViewSet, basename='app-scheduled-task')
router.register(r'notification-logs', AppNotificationLogViewSet, basename='app-notification-log')
router.register(r'executions', AppTestExecutionViewSet, basename='app-execution')

urlpatterns = [
    path('', include(router.urls)),
    path('executions/<int:execution_id>/report/', serve_report_file, name='app-execution-report'),
    path('executions/<int:execution_id>/report/<path:file_path>', serve_report_file, name='app-execution-report-file'),
    path('executions/<int:execution_id>/public-report/<str:token>/', serve_public_report_file, name='app-execution-public-report'),
    path('executions/<int:execution_id>/public-report/<str:token>/<path:file_path>', serve_public_report_file, name='app-execution-public-report-file'),
    path('evidence/<str:token>/', serve_public_evidence_file, name='app-public-evidence'),
    path('templates/<str:token>/', serve_public_template_file, name='app-public-template'),
]
