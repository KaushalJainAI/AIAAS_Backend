from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    def ready(self):
        # Arms/cancels HITL reminder ladders. Imported here so the receivers
        # are registered exactly once, after the app registry is populated.
        from . import signals  # noqa: F401
