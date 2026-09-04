#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    # `manage.py test` gets the test settings unless the caller asked for
    # something else. Without this the suite runs against settings.local,
    # which means the real Redis and the real throttle rates: tests then fail
    # with 429s that have nothing to do with what they were asserting, and
    # leak throttle state into each other depending on execution order.
    if 'test' in sys.argv and not any(a.startswith('--settings') for a in sys.argv):
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings.test')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings.local')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
