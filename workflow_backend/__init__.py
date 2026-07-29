"""
Importing the Celery app here is the documented Django integration: it makes
the app exist as soon as the project package loads, so @shared_task in the
individual apps binds to it instead of to whichever app happened to be created
first. Without this, binding depended on some module incidentally importing
workflow_backend.celery — executor/trigger_manager.py was doing exactly that,
with an import that looked unused.
"""
from .celery import app as celery_app

__all__ = ('celery_app',)
