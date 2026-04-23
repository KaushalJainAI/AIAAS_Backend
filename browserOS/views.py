from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from .models import OSWorkspace, OSAppWindow, OSNotification
from .serializers import OSWorkspaceSerializer, OSAppWindowSerializer, OSNotificationSerializer

class OSWorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = OSWorkspaceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return OSWorkspace.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
        
    @action(detail=False, methods=['get'])
    def mine(self, request):
        """Get or create the user's default workspace."""
        workspace, created = OSWorkspace.objects.get_or_create(user=request.user)
        serializer = self.get_serializer(workspace)
        return Response(serializer.data)


class OSAppWindowViewSet(viewsets.ModelViewSet):
    serializer_class = OSAppWindowSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return OSAppWindow.objects.filter(workspace__user=self.request.user)

    def perform_create(self, serializer):
        workspace, _ = OSWorkspace.objects.get_or_create(user=self.request.user)
        serializer.save(workspace=workspace)


class OSNotificationViewSet(viewsets.ModelViewSet):
    serializer_class = OSNotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return OSNotification.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        self.get_queryset().update(is_read=True)
        return Response({"status": "ok"})

