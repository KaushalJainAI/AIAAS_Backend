"""
Authentication Views for Workflow Backend

Following NGU backend patterns with rate limiting and JWT.
"""
from rest_framework import generics, status, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import UserProfile, APIKey, UsageTracking
from .serializers import (
    UserSerializer,
    UserProfileSerializer,
    UserRegistrationSerializer,
    CustomTokenObtainPairSerializer,
    ChangePasswordSerializer,
    APIKeySerializer,
    APIKeyCreateSerializer,
    UsageTrackingSerializer,
)
from .permissions import IsOwner


# ==================== CUSTOM THROTTLES ====================

class LoginRateThrottle(AnonRateThrottle):
    """Throttle for login attempts - prevents brute force attacks"""
    scope = 'login'


class RegisterRateThrottle(AnonRateThrottle):
    """Throttle for registration - prevents mass account creation"""
    scope = 'register'


# ==================== Auth Views ====================

class UserRegistrationView(generics.CreateAPIView):
    """
    Register a new user.
    
    Rate limited: 3 attempts per minute
    Creates user and associated UserProfile automatically.
    Returns JWT tokens for immediate auth.
    """
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_classes = [RegisterRateThrottle]
    
    def create(self, request, *args, **kwargs):
        from rest_framework_simplejwt.tokens import RefreshToken
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Generate tokens for immediate login
        refresh = RefreshToken.for_user(user)
        
        # Get profile
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.objects.create(user=user)
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': user.id,
                'email': user.email,
                'name': f"{user.first_name} {user.last_name}".strip() or user.username,
                'tier': profile.tier,
                'credits': profile.credits_remaining,
                'createdAt': user.date_joined.isoformat(),
            },
            'message': 'User registered successfully.'
        }, status=status.HTTP_201_CREATED)


class GoogleLoginView(APIView):
    """
    Exchange Google OAuth2 code for JWT tokens.
    Creates user if they don't exist.
    """
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        code = request.data.get('code')
        if not code:
             return Response({'error': 'Code is required'}, status=status.HTTP_400_BAD_REQUEST)
             
        from credentials.oauth import GoogleOAuthProvider
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from rest_framework_simplejwt.tokens import RefreshToken
        
        # 1. Exchange Code
        # We need to make sure we use the LOGIN callback URI, not the default if they differ.
        # But in settings we set GOOGLE_OAUTH_REDIRECT_URI.
        # Frontend MUST use the same redirect_uri to initiate flow.
        redirect_uri = request.data.get('redirect_uri', settings.GOOGLE_OAUTH_REDIRECT_URI)
        
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        
        try:
            token_data = provider.exchange_code(code)
        except Exception as e:
             return Response({'error': f'Token exchange failed: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
             
        if 'error' in token_data:
             return Response({'error': token_data.get('error_description', 'Unknown OAuth error')}, status=status.HTTP_400_BAD_REQUEST)
             
        access_token = token_data.get('access_token')
        
        # 2. Get User Info
        try:
            user_info = provider.get_user_info(access_token)
        except Exception as e:
            return Response({'error': 'Failed to fetch user info'}, status=status.HTTP_400_BAD_REQUEST)
            
        email = user_info.get('email')
        if not email:
            return Response({'error': 'No email found in Google account'}, status=status.HTTP_400_BAD_REQUEST)
            
        # 3. Find or Create User
        User = get_user_model()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Create new user
            username = email.split('@')[0]
            # Ensure unique username
            base_username = username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{counter}"
                counter += 1
                
            user = User.objects.create_user(
                username=username,
                email=email,
                first_name=user_info.get('given_name', ''),
                last_name=user_info.get('family_name', '')
            )
            # Create profile
            UserProfile.objects.create(user=user)
            
        # 4. Generate JWT
        refresh = RefreshToken.for_user(user)
        try:
             profile = user.profile
        except UserProfile.DoesNotExist:
             profile = UserProfile.objects.create(user=user)

        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': user.id,
                'email': user.email,
                'name': f"{user.first_name} {user.last_name}".strip() or user.username,
                'tier': profile.tier,
                'credits': profile.credits_remaining,
                'createdAt': user.date_joined.isoformat(),
            }
        })


class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Custom JWT token view with additional user data.
    
    Rate limited: 5 attempts per minute to prevent brute force.
    Returns access token, refresh token, and user tier.
    """
    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [LoginRateThrottle]


class UserProfileView(generics.RetrieveUpdateAPIView):
    """
    Get and update current user's profile.
    
    GET: Returns user profile with tier, limits, and credits
    PATCH: Update basic profile info
    """
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_object(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        return profile


class ChangePasswordView(APIView):
    """Change current user's password"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        
        return Response(
            {'detail': 'Password updated successfully'},
            status=status.HTTP_200_OK
        )


# ==================== API Key Views ====================

class APIKeyViewSet(viewsets.ModelViewSet):
    """
    Manage user's API keys.
    
    LIST: Get all user's API keys (key value hidden)
    CREATE: Generate a new API key (key shown once)
    DELETE: Revoke an API key
    """
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return APIKey.objects.filter(user=self.request.user)
    
    def get_serializer_class(self):
        if self.action == 'create':
            return APIKeyCreateSerializer
        return APIKeySerializer
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        
        return Response({
            'api_key': serializer.instance.key,  # Show full key only on creation
            'message': 'API key created. Save this key - it will not be shown again.',
            'data': APIKeySerializer(serializer.instance).data
        }, status=status.HTTP_201_CREATED)


class APIKeyRotateView(APIView):
    """Rotate (regenerate) an existing API key"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, pk):
        try:
            api_key = APIKey.objects.get(pk=pk, user=request.user)
        except APIKey.DoesNotExist:
            return Response(
                {'detail': 'API key not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Generate new key
        old_prefix = api_key.key_prefix
        api_key.key = APIKey.generate_key()
        api_key.key_prefix = api_key.key[:8]
        api_key.save()
        
        return Response({
            'new_key': api_key.key,
            'old_prefix': old_prefix,
            'message': 'API key rotated. Save this key - it will not be shown again.'
        }, status=status.HTTP_200_OK)


# ==================== Usage Views ====================

class UsageTrackingView(generics.ListAPIView):
    """
    Get usage metrics for current user.
    
    Returns daily usage records sorted by date descending.
    """
    serializer_class = UsageTrackingSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return UsageTracking.objects.filter(user=self.request.user)[:30]  # Last 30 days
