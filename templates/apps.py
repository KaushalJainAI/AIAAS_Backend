from django.apps import AppConfig

class TemplatesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'templates'

    def ready(self):
        import templates.signals
