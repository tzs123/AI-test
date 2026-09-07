from django.urls import path

from .e2e_views import (
    E2EAnalyzeAPIView,
    E2ECreateAPIView,
    E2EEvidenceAPIView,
    E2EListAPIView,
    E2EReportAPIView,
    E2EReplanAPIView,
    E2EResultAPIView,
    E2ERunAPIView,
    E2EStopAPIView,
)


urlpatterns = [
    path('create', E2ECreateAPIView.as_view(), name='e2e-create'),
    path('tasks', E2EListAPIView.as_view(), name='e2e-list'),
    path('run/<int:task_id>', E2ERunAPIView.as_view(), name='e2e-run'),
    path('replan/<int:task_id>', E2EReplanAPIView.as_view(), name='e2e-replan'),
    path('stop/<int:task_id>', E2EStopAPIView.as_view(), name='e2e-stop'),
    path('result/<int:task_id>', E2EResultAPIView.as_view(), name='e2e-result'),
    path('report/<int:task_id>', E2EReportAPIView.as_view(), name='e2e-report'),
    path(
        'evidence/<int:task_id>/<int:log_id>/screenshot',
        E2EEvidenceAPIView.as_view(),
        {'kind': 'screenshot'},
        name='e2e-screenshot',
    ),
    path(
        'evidence/<int:task_id>/<int:log_id>/video',
        E2EEvidenceAPIView.as_view(),
        {'kind': 'video'},
        name='e2e-video',
    ),
    path('analyze/<int:task_id>', E2EAnalyzeAPIView.as_view(), name='e2e-analyze'),
]
