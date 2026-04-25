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
        import orchestrator.signals  # Register signals

        # Avoid running during management commands like migrate or makemigrations
        if any(cmd in sys.argv for cmd in ['migrate', 'makemigrations', 'collectstatic', 'test', 'showmigrations']):
            return

        # Only run in the reloader's child process (where the actual app runs)
        # to avoid triggering DB warnings and duplicate registrations.
        if os.environ.get('RUN_MAIN') != 'true' and os.environ.get('DJANGO_SETTINGS_MODULE'):
            return

        import threading

        def _register():
            try:
                from orchestrator.models import Workflow
                from executor.trigger_manager import get_trigger_manager

                mgr = get_trigger_manager()
                active_workflows = Workflow.objects.filter(status='active')
                for workflow in active_workflows:
                    mgr.register_triggers(workflow)
            except Exception:
                pass

        threading.Thread(target=_register, daemon=True).start()
