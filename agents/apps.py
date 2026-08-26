from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agents'

    # The package is `agents`; the *app label* deliberately is not. Every table
    # in this app is named `orchestrator_*`, every `to='orchestrator.workflow'`
    # in six other apps' migrations resolves through this label, and 15
    # migrations depend on `('orchestrator', ...)`. Pinning the label keeps the
    # rename a pure source-code change: nothing to migrate, nothing to rewrite
    # in `django_migrations` or `django_content_type`, and no deploy window.
    label = 'orchestrator'
