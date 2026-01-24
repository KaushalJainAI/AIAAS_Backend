from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Credential, CredentialType, CredentialAuditLog
from .serializers import CredentialSerializer, CredentialTypeSerializer, CredentialAuditLogSerializer

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
    def verify(self, request, pk=None):
        """
        Trigger credential verification logic against the external provider.
        """
        credential = self.get_object()
        
        from .verification import CredentialVerifier
        
        # Capture audit context
        audit_context = {
            'user': request.user,
            'ip_address': request.META.get('REMOTE_ADDR'),
            'user_agent': request.META.get('HTTP_USER_AGENT')
        }
        
        is_valid, message = CredentialVerifier.verify(credential, audit_context=audit_context)
        
        credential.is_verified = is_valid
        # Update last_error if failed, clear it if success
        if is_valid:
            credential.last_error = ""
        else:
            credential.last_error = message
            
        credential.save()
        
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
        Generate Google Authorization URL.
        Query Params:
          - redirect_uri: callback URL on frontend
          - scopes: optional list of scopes (defaults to Sheets/Drive if not specified)
        """
        from .oauth import GoogleOAuthProvider
        
        redirect_uri = request.query_params.get('redirect_uri')
        if not redirect_uri:
            return Response({'error': 'redirect_uri query param is required'}, status=400)
            
        # Default scopes for our main integration use cases (Sheets, etc)
        default_scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.readonly'
        ]
        
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        url = provider.get_auth_url(scopes=default_scopes)
        
        return Response({'url': url})

    @action(detail=False, methods=['post'])
    def callback(self, request):
        """
        Exchange code for tokens and create/update Credential.
        Body:
          - code: authorization code
          - redirect_uri: callback URL used
          - name: name for the credential
        """
        from .oauth import GoogleOAuthProvider
        from .models import Credential, CredentialType
        
        code = request.data.get('code')
        redirect_uri = request.data.get('redirect_uri')
        name = request.data.get('name', 'Google Account')
        
        if not code or not redirect_uri:
            return Response({'error': 'code and redirect_uri are required'}, status=400)
            
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        
        try:
            token_data = provider.exchange_code(code)
        except Exception as e:
            return Response({'error': f'Token exchange failed: {str(e)}'}, status=400)
            
        if 'error' in token_data:
             return Response({'error': token_data.get('error_description', 'Unknown OAuth error')}, status=400)
             
        # Get/Create Credential Type
        try:
            cred_type = CredentialType.objects.get(slug='google-oauth2')
        except CredentialType.DoesNotExist:
             return Response({'error': 'Google OAuth2 credential type not found in system'}, status=500)
             
        # Create Credential
        # We store tokens in the encrypted 'access_token'/'refresh_token' fields 
        # OR in the 'data' blob. The model has dedicated fields for tokens.
        
        credential = Credential.objects.create(
            user=request.user,
            credential_type=cred_type,
            name=name,
            # Store tokens in specific fields
            # Note: BinaryField requires bytes
            access_token=token_data.get('access_token', '').encode(),
            refresh_token=token_data.get('refresh_token', '').encode(),
        )
        
        # Also store expiration if available
        expires_in = token_data.get('expires_in')
        if expires_in:
            from django.utils import timezone
            from datetime import timedelta
            credential.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
            
        # Verify immediately (fetch user email/profile as proof)
        try:
            user_info = provider.get_user_info(token_data.get('access_token'))
            credential.public_metadata = {
                'email': user_info.get('email'),
                'picture': user_info.get('picture'),
                'name': user_info.get('name')
            }
            credential.is_verified = True
        except:
            credential.is_verified = False
            
        credential.save()
            
        return Response(CredentialSerializer(credential).data)


class CredentialAuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ReadOnly ViewSet for Credential Audit Logs.
    Users can view logs for their own credentials.
    """
    serializer_class = CredentialAuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return CredentialAuditLog.objects.filter(user=self.request.user)

