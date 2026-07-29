from django.apps import AppConfig

class TemplatesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'templates'

    def ready(self):
        # Imported for the @receiver side effects, not for a name.
        # See orchestrator/apps.py.
        import templates.signals  # noqa: F401
