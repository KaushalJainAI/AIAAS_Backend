from django.urls import path
from . import views

app_name = 'templates'

urlpatterns = [
    path('', views.template_list, name='list'),
    path('<int:pk>/', views.template_detail, name='detail'),
    path('search/', views.template_search, name='search'),
    path('publish/<int:workflow_id>/', views.create_from_workflow, name='publish'),
    
    # Community & Discovery
    path('<int:pk>/rate/', views.rate_template, name='rate'),
    path('<int:pk>/ratings/', views.template_ratings, name='ratings'),
    path('<int:pk>/bookmark/', views.bookmark_template, name='bookmark'),
    path('<int:pk>/comments/', views.template_comments, name='comments'),
    path('<int:pk>/similar/', views.similar_templates, name='similar'),
]
