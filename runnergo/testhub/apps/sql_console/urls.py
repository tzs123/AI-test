from django.urls import path

from . import views

urlpatterns = [
    path('execute', views.execute),
    path('test-connection', views.test_connection),
]
