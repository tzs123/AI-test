from django.apps import AppConfig


class SqlConsoleConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sql_console'
    verbose_name = 'SQL 控制台'
