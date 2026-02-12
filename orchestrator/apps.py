from django.apps import AppConfig


class OrchestratorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orchestrator'

    def ready(self):
        """
        Re-register triggers for all active workflows on startup.
        Checks for local dev environment or specific system checks.
        """
        import sys
        import os

        # Avoid running during management commands like migrate or makemigrations
        if any(cmd in sys.argv for cmd in ['migrate', 'makemigrations', 'collectstatic', 'test']):
            return

        # Avoid running in the main process if we're in a reloader process
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('DJANGO_SETTINGS_MODULE'):
            return

        try:
            from orchestrator.models import Workflow
            from executor.trigger_manager import get_trigger_manager
            
            mgr = get_trigger_manager()
            # Note: Using .all() here might be heavy if there are thousands of workflows.
            # But for initial project scale, it is fine.
            active_workflows = Workflow.objects.filter(status='active')
            
            for workflow in active_workflows:
                mgr.register_triggers(workflow)
        except Exception:
            # We don't want startup to fail because of trigger registration issues
            pass
