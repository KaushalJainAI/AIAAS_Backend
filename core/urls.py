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
    UserProfileView,
    AvatarUploadView,
    ChangePasswordView,
    PasswordChangeOTPRequestView,
    PasswordChangeOTPVerifyView,
    PasswordResetRequestView,
    PasswordResetVerifyView,
    PasswordResetConfirmView,
    APIKeyViewSet,
    APIKeyRotateView,
    GoogleLoginView,
    UsageTrackingView,
    UsageInsightsView,
)


# Router for viewsets
router = DefaultRouter()
router.register(r'api-keys', APIKeyViewSet, basename='api-keys')


urlpatterns = [
    # Authentication
    path('auth/register/', UserRegistrationView.as_view(), name='register'),
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='login'),
    path('auth/google/', GoogleLoginView.as_view(), name='google_login'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/profile/', UserProfileView.as_view(), name='profile'),
    path('auth/profile/avatar/', AvatarUploadView.as_view(), name='avatar-upload'),
    path('auth/change-password/request-otp/', PasswordChangeOTPRequestView.as_view(), name='change-password-request-otp'),
    path('auth/change-password/verify-otp/', PasswordChangeOTPVerifyView.as_view(), name='change-password-verify-otp'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('auth/password-reset-request/', PasswordResetRequestView.as_view(), name='password-reset-request'),
    path('auth/password-reset-verify/', PasswordResetVerifyView.as_view(), name='password-reset-verify'),
    path('auth/password-reset-confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    
    # API Keys
    path('auth/', include(router.urls)),
    path('auth/api-keys/<int:pk>/rotate/', APIKeyRotateView.as_view(), name='api-key-rotate'),
    
    # Usage
    path('usage/', UsageTrackingView.as_view(), name='usage'),
    path('usage/insights/', UsageInsightsView.as_view(), name='usage-insights'),
]
