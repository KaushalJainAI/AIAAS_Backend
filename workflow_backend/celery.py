"""
Celery Application Configuration

Sets up Celery for asynchronous task processing.
"""
import os
from celery import Celery

# Must name a concrete settings module. 'workflow_backend.settings' is the
# package, and its __init__ is empty, so defaulting to it hands Django a
# settings object with zero INSTALLED_APPS — which fails silently: the worker
# boots fine and simply registers no tasks. Match what manage.py defaults to.
# In Docker this is already set to .deployment by the Dockerfile, so this
# default only bites someone running the worker by hand.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings.local')

app = Celery('workflow_backend')

# Load task modules from all registered Django apps
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()

# autodiscover_tasks only looks at each app's tasks.py, so anything declared
# elsewhere never reaches the worker: calling .delay() enqueues a message that
# the worker then rejects as NotRegistered. inference/migration_tasks.py holds
# the KB re-index sweep and was in exactly that state.
app.autodiscover_tasks(related_name='migration_tasks')


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery connectivity."""
    print(f'Request: {self.request!r}')
