from django.urls import path
from . import views

urlpatterns = [
    path('context/', views.process_context, name='process_context'),
    path('action/', views.trigger_action, name='trigger_action'),
    path('commands/', views.trigger_action, name='process_command'),
]
