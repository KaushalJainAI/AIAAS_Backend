from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Credential, CredentialType
from .serializers import CredentialSerializer, CredentialTypeSerializer

class CredentialTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ReadOnly ViewSet for Credential Types.
    Allows frontend to fetch available integrations and their schemas.
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
        return Credential.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        """Override to return wrapped response matching frontend expectations."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'credentials': serializer.data})

    def perform_create(self, serializer):
        # Automatically assign the creator
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        """
        Optional endpoint to trigger credential verification logic.
        For now, just returns a mock success if credential exists.
        Future: Integrate with specific service verification logic.
        """
        credential = self.get_object()
        # Mock verification logic
        credential.is_verified = True
        credential.save()
        return Response({'valid': True, 'message': 'Credential verified successfully'})
