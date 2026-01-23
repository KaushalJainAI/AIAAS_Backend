from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'types', views.CredentialTypeViewSet, basename='credential-types')
router.register(r'', views.CredentialViewSet, basename='credentials')

router.register(r'oauth/google', views.GoogleCredentialOAuthViewSet, basename='google-credentials')

urlpatterns = [
    path('', include(router.urls)),
]
