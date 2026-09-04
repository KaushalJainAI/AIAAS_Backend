from django.apps import AppConfig


class ToolsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tools_config'
    verbose_name = 'Tool Library'
    label = 'tools_config'

    def ready(self):
        from . import signals  # noqa: F401  - connects cache invalidation
