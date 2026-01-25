from django.urls import path
from . import views

app_name = 'templates'

urlpatterns = [
    path('', views.template_list, name='list'),
    path('<int:pk>/', views.template_detail, name='detail'),
    path('search/', views.template_search, name='search'),
    path('publish/<int:workflow_id>/', views.create_from_workflow, name='publish'),
]
