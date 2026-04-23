from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.conf import settings
from django.contrib.auth import get_user_model
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from .serializers import UserSerializer, CustomTokenObtainPairSerializer
from .models import UserProfile

User = get_user_model()

class GoogleLogin(APIView):
    """
    Google Social Login View (Manual id_token Verification)
    Receives frontend's Google credential, verifies it locally, 
    and returns JWT tokens for the session.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        # Frontend sends the credential as `access_token` or `id_token`
        token = request.data.get('access_token') or request.data.get('id_token')
        
        if not token:
            return Response({'detail': 'Google token is missing'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            # 1. Verify token signature against Google's certificates
            client_id = settings.SOCIALACCOUNT_PROVIDERS['google']['APP']['client_id']
            
            idinfo = id_token.verify_oauth2_token(
                token, 
                google_requests.Request(), 
                client_id
            )
            
            # 2. Extract user info
            email = idinfo.get('email')
            name = idinfo.get('name', '')
            first_name = idinfo.get('given_name', '')
            last_name = idinfo.get('family_name', '')
            
            if not email:
                return Response({'detail': 'Email not provided by Google'}, status=status.HTTP_400_BAD_REQUEST)
                
            # 3. Get or create user
            # AIAAS uses default User model which has username, email, first_name, last_name
            user, created = User.objects.get_or_create(email=email, defaults={
                'username': email.split('@')[0],
                'first_name': first_name,
                'last_name': last_name,
            })
            
            if created:
                user.set_unusable_password()
                user.save()
                # Create profile
                UserProfile.objects.get_or_create(user=user)
            
            # 4. Generate JWT tokens
            refresh = CustomTokenObtainPairSerializer.get_token(user)
            access = refresh.access_token
            
            response = Response({
                'access': str(access),
                'refresh': str(refresh),
                'user': UserSerializer(user).data
            }, status=status.HTTP_200_OK if not created else status.HTTP_201_CREATED)
            
            # 5. Set HttpOnly secure cookies
            response.set_cookie(
                key='access_token',
                value=str(access),
                httponly=True,
                secure=not settings.DEBUG,
                samesite='Lax',
                max_age=3600
            )
            response.set_cookie(
                key='refresh_token',
                value=str(refresh),
                httponly=True,
                secure=not settings.DEBUG,
                samesite='Lax',
                max_age=3600 * 24 * 7
            )
            
            return response
            
        except ValueError as e:
            return Response({'detail': 'Invalid Google token', 'error': str(e)}, status=status.HTTP_401_UNAUTHORIZED)
