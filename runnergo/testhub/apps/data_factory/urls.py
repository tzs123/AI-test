from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DataFactoryViewSet, TestDataGenerateAPIView

router = DefaultRouter()
router.register(r'', DataFactoryViewSet, basename='data-factory')

urlpatterns = [
    path('generate/', TestDataGenerateAPIView.as_view(), name='test-data-generate'),
    path('', include(router.urls)),
]
