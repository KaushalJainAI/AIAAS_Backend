"""
Compatibility shim — re-exports local settings when DJANGO_SETTINGS_MODULE
is not explicitly set. Do not add new config here.
"""
from workflow_backend.settings.local import *  # noqa: F401, F403
