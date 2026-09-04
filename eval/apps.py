from django.apps import AppConfig


class EvalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'eval'
    verbose_name = 'Evaluation'

    # Tables are `eval_*`. Deliberately singular: a previous `evals` app was
    # deleted 2026-08-17 (docs/API.md §18) and dev databases predating that
    # still carry inert `evals_*` tables plus an `evals.0001_initial` row in
    # `django_migrations`. Naming this one `eval` means a fresh `migrate` on
    # such a database cannot collide with the corpse of the old one.
    label = 'eval'
