from django.urls import path
from . import views

urlpatterns = [
    # 测试用例相关
    path('', views.TestCaseListCreateView.as_view(), name='testcase-list'),
    path('import/template/', views.TestCaseImportTemplateDownloadView.as_view(), name='testcase-import-template'),
    path('import-records/', views.TestCaseImportRecordListCreateView.as_view(), name='testcase-import-record-list'),
    path('import-records/<int:pk>/', views.TestCaseImportRecordDetailView.as_view(), name='testcase-import-record-detail'),
    path('import-records/<int:pk>/failure-report/', views.TestCaseImportFailureReportDownloadView.as_view(), name='testcase-import-record-failure-report'),
    path('<int:pk>/', views.TestCaseDetailView.as_view(), name='testcase-detail'),
]
