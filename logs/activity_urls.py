from django.urls import path

from . import activity

app_name = 'activity'

urlpatterns = [
    path('live/', activity.activity_live, name='activity_live'),
    path('recent-files/', activity.activity_recent_files, name='activity_recent_files'),
]
