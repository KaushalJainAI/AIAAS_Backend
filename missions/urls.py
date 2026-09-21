from django.urls import path

from chat.commands import views as command_views

urlpatterns = [
    path('', command_views.mission_list_create, name='mission_list_create'),
    path('<int:mission_id>/', command_views.mission_detail, name='mission_detail'),
    path('<int:mission_id>/<str:verb>/', command_views.mission_action, name='mission_action'),
]
