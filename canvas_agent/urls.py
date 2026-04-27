from django.urls import path
from . import views

urlpatterns = [
    path('command/', views.process_command, name='canvas_agent_command'),
    path('node-types/', views.get_node_types, name='canvas_agent_node_types'),
]
