from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DatasetRowViewSet, DatasetViewSet

router = DefaultRouter()
router.register(r'datasets', DatasetViewSet, basename='dataset')
router.register(r'dataset-rows', DatasetRowViewSet, basename='dataset-row')

urlpatterns = [path('', include(router.urls))]
