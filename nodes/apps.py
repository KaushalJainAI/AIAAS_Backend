from django.apps import AppConfig


class NodesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'nodes'
    
    def ready(self):
        """
        Initialize node registry on app startup.
        
        Note: All node registration is handled by get_registry() with lazy loading.
        This avoids duplicate registration and potential race conditions.
        """
        from .handlers.registry import get_registry
        
        # Trigger lazy registration of all nodes
        get_registry()


