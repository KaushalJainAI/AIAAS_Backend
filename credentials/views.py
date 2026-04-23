from rest_framework import status
from adrf import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.conf import settings as django_settings
from django.core import signing
from urllib.parse import urlparse
from .models import Credential, CredentialType, CredentialAuditLog
from .serializers import (
    CredentialSerializer, 
    CredentialTypeSerializer, 
    CredentialAuditLogSerializer,
    CredentialOAuthInitSerializer,
    CredentialOAuthCallbackSerializer
)
from asgiref.sync import sync_to_async
import logging

logger = logging.getLogger(__name__)

# Allowed OAuth redirect URI origins (add production domain)
ALLOWED_REDIRECT_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:5173',
    'http://127.0.0.1:3000',
    'http://127.0.0.1:5173',
]
# Extend with CORS origins from settings if available
try:
    ALLOWED_REDIRECT_ORIGINS.extend(getattr(django_settings, 'CORS_ALLOWED_ORIGINS', []))
except Exception:
    pass

class CredentialTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Credential Types.
    Allows fetching available types.
    """
    queryset = CredentialType.objects.filter(is_active=True)
    serializer_class = CredentialTypeSerializer
    permission_classes = [IsAuthenticated]

    def list(self, request, *args, **kwargs):
        """Override to return wrapped response matching frontend expectations."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'types': serializer.data})


class CredentialViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing user credentials.
    """
    serializer_class = CredentialSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Users can only see their own credentials
        return Credential.objects.filter(user=self.request.user).select_related('credential_type')

    def list(self, request, *args, **kwargs):
        """Override to return wrapped response matching frontend expectations."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'credentials': serializer.data})

    def perform_create(self, serializer):
        # Automatically assign the creator
        serializer.save(user=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Prevent deletion if credential is used in active workflows.
        """
        instance = self.get_object()
        credential_id = str(instance.id)
        
        # Check active workflows
        # Since we don't have a normalized WorkflowNode model, we inspect the JSON
        from orchestrator.models import Workflow
        
        active_workflows = Workflow.objects.filter(
            user=request.user, 
            status='active'
        )
        
        affected = []
        for wf in active_workflows:
            for node in wf.nodes:
                data = node.get('data', {})
                # check for credential_id usage
                if str(data.get('credential_id')) == credential_id:
                    affected.append(wf.name)
                    break 
        
        if affected:
            return Response(
                {'error': f'Cannot delete credential used in active workflows: {", ".join(affected)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Log deletion audit
        from .models import CredentialAuditLog
        CredentialAuditLog.objects.create(
            credential=instance, # It will be nullified on delete due to SET_NULL but we capture it now? 
            # Actually on_delete=SET_NULL in AuditLog means log stays but credential link is lost.
            # We should probably capture name/type in snapshot if we want to keep history meaningful?
            # Creating audit log BEFORE deletion so it has the link.
            user=request.user,
            action='deleted',
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT')
        )
            
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    async def verify(self, request, pk=None):
        """
        Trigger credential verification logic against the external provider.
        """
        # Wrap ORM in sync_to_async
        credential = await sync_to_async(self.get_object)()
        
        from .verification import CredentialVerifier
        
        # Capture audit context
        audit_context = {
            'user': request.user,
            'ip_address': request.META.get('REMOTE_ADDR'),
            'user_agent': request.META.get('HTTP_USER_AGENT')
        }
        
        # Await the now-async verify method
        is_valid, message = await CredentialVerifier.verify(credential, audit_context=audit_context)
        
        credential.is_verified = is_valid
        # Update last_error if failed, clear it if success
        if is_valid:
            credential.last_error = ""
        else:
            credential.last_error = message
            
        await sync_to_async(credential.save)()
        
        return Response({
            'valid': is_valid, 
            'message': message
        })


class GoogleCredentialOAuthViewSet(viewsets.ViewSet):
    """
    Handle Google OAuth2 flow for creating/updating Credentials.
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def init(self, request):
        """
        Generate Google Authorization URL with CSRF state token.
        """
        from .oauth import GoogleOAuthProvider
        
        serializer = CredentialOAuthInitSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        
        redirect_uri = serializer.validated_data['redirect_uri']
        scopes = serializer.validated_data.get('scopes')
        
        # Validate redirect_uri against allowlist to prevent open redirect
        parsed_redirect = urlparse(redirect_uri)
        redirect_origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
        if redirect_origin not in ALLOWED_REDIRECT_ORIGINS:
            return Response(
                {'error': f'Redirect URI origin is not allowed: {redirect_origin}'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Default scopes for our main integration use cases (Sheets, etc)
        if not scopes:
            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive.readonly'
            ]
        
        # Generate CSRF state token (signed with Django SECRET_KEY, expires in 10 min)
        state = signing.dumps(
            {'user_id': request.user.id, 'redirect_uri': redirect_uri},
            salt='oauth-state'
        )
        
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        url = provider.get_auth_url(scopes=scopes, state=state)
        
        return Response({'url': url})

    @action(detail=False, methods=['post'])
    async def callback(self, request):
        """
        Exchange code for tokens and create/update Credential.
        Validates the CSRF state token to prevent CSRF/open redirect attacks.
        """
        from .oauth import GoogleOAuthProvider
        from .models import Credential, CredentialType
        
        serializer = CredentialOAuthCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        code = serializer.validated_data['code']
        redirect_uri = serializer.validated_data['redirect_uri']
        name = serializer.validated_data['name']
        state = serializer.validated_data.get('state') or request.data.get('state')
        
        # Validate CSRF state token
        if state:
            try:
                state_data = signing.loads(state, salt='oauth-state', max_age=600)  # 10 min expiry
                if state_data.get('user_id') != request.user.id:
                    return Response({'error': 'OAuth state token user mismatch'}, status=400)
                if state_data.get('redirect_uri') != redirect_uri:
                    return Response({'error': 'OAuth state token redirect_uri mismatch'}, status=400)
            except signing.BadSignature:
                return Response({'error': 'Invalid OAuth state token'}, status=400)
            except signing.SignatureExpired:
                return Response({'error': 'OAuth state token expired. Please try again.'}, status=400)
        else:
            logger.warning(f"OAuth callback missing state parameter for user {request.user.id}")
        
        # Validate redirect_uri against allowlist
        parsed_redirect = urlparse(redirect_uri)
        redirect_origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
        if redirect_origin not in ALLOWED_REDIRECT_ORIGINS:
            return Response(
                {'error': f'Redirect URI origin is not allowed'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        
        try:
            # Await async code exchange
            token_data = await provider.exchange_code(code)
        except Exception as e:
            return Response({'error': f'Token exchange failed: {str(e)}'}, status=400)
            
        if 'error' in token_data:
             return Response({'error': token_data.get('error_description', 'Unknown OAuth error')}, status=400)
             
        # Get/Create Credential Type
        try:
            cred_type = await sync_to_async(CredentialType.objects.get)(slug='google-oauth2')
        except CredentialType.DoesNotExist:
             return Response({'error': 'Google OAuth2 credential type not found in system'}, status=500)
             
        # Encrypt tokens before saving
        from cryptography.fernet import Fernet
        fernet = Fernet(Credential._get_encryption_key())
        
        # Create Credential
        credential = await sync_to_async(Credential.objects.create)(
            user=request.user,
            credential_type=cred_type,
            name=name,
            access_token=fernet.encrypt(token_data.get('access_token', '').encode()),
            refresh_token=fernet.encrypt(token_data.get('refresh_token', '').encode()),
        )
        
        # Also store expiration if available
        expires_in = token_data.get('expires_in')
        if expires_in:
            from django.utils import timezone
            from datetime import timedelta
            credential.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
            
        # Verify immediately - use async info fetch
        try:
            # Await async user info fetch
            user_info = await provider.get_user_info(token_data.get('access_token'))
            credential.public_metadata = {
                'email': user_info.get('email'),
                'picture': user_info.get('picture'),
                'name': user_info.get('name')
            }
            credential.is_verified = True
        except Exception:
            credential.is_verified = False
            
        await sync_to_async(credential.save)()
        
        # Use sync_to_async for serializer in async context
        serialized_data = await sync_to_async(lambda: CredentialSerializer(credential).data)()
        return Response(serialized_data)


class CredentialAuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ReadOnly ViewSet for Credential Audit Logs.
    Users can view logs for their own credentials.
    """
    serializer_class = CredentialAuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return CredentialAuditLog.objects.filter(user=self.request.user)

