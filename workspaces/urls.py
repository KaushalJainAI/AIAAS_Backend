from django.urls import path

from . import views

urlpatterns = [
    path('hooks/<str:secret>/', views.job_finished, name='workspace-job-finished'),
]
