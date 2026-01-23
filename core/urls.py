"""
URL Configuration for Core App

Authentication and user management endpoints.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    UserRegistrationView,
    CustomTokenObtainPairView,
    GoogleLoginView,
    UserProfileView,
    ChangePasswordView,
    APIKeyViewSet,
    APIKeyRotateView,
    UsageTrackingView,
)


# Router for viewsets
router = DefaultRouter()
router.register(r'keys', APIKeyViewSet, basename='api-keys')


urlpatterns = [
    # Authentication
    path('auth/register/', UserRegistrationView.as_view(), name='register'),
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='login'),
    path('auth/google/', GoogleLoginView.as_view(), name='google_login'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/profile/', UserProfileView.as_view(), name='profile'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change-password'),
    
    # API Keys
    path('', include(router.urls)),
    path('keys/<int:pk>/rotate/', APIKeyRotateView.as_view(), name='api-key-rotate'),
    
    # Usage
    path('usage/', UsageTrackingView.as_view(), name='usage'),
]
