from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import EvalCaseViewSet, EvalRunViewSet, EvalSuiteViewSet

router = DefaultRouter()
router.register(r'suites', EvalSuiteViewSet, basename='eval-suite')
router.register(r'cases', EvalCaseViewSet, basename='eval-case')
router.register(r'runs', EvalRunViewSet, basename='eval-run')

urlpatterns = [path('', include(router.urls))]
