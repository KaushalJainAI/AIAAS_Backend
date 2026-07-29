from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TuningJobViewSet

router = DefaultRouter()
router.register(r'jobs', TuningJobViewSet, basename='tuning-job')

urlpatterns = [path('', include(router.urls))]
