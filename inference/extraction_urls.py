from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .extraction_views import ExtractedRowViewSet, ExtractionSchemaViewSet

router = DefaultRouter()
router.register(r'schemas', ExtractionSchemaViewSet, basename='extraction-schema')
router.register(r'rows', ExtractedRowViewSet, basename='extraction-row')

urlpatterns = [path('', include(router.urls))]