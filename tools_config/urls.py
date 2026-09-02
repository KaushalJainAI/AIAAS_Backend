from django.urls import path
from .views import tool_catalogue, tool_usage

app_name = 'tools_config'

urlpatterns = [
    path('', tool_catalogue, name='tool_catalogue'),
    path('usage/', tool_usage, name='tool_usage'),
]
