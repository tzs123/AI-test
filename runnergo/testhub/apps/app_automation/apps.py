# -*- coding: utf-8 -*-
from django.apps import AppConfig
from django.db.backends.signals import connection_created


class AppAutomationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.app_automation'
    verbose_name = 'APP自动化测试'
    
    def ready(self):
        """应用启动时执行"""
        from .database import configure_sqlite_connection

        connection_created.connect(
            configure_sqlite_connection,
            dispatch_uid='app_automation.configure_sqlite_connection',
        )
        try:
            import apps.app_automation.tasks  # noqa
        except ImportError:
            pass
