from django.contrib.auth import get_user_model
from django.db import models

User = get_user_model()

class OSWorkspace(models.Model):
    """Represents a user's desktop environment in BrowserOS."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="browser_workspace")
    name = models.CharField(max_length=255, default="My Workspace")
    theme_preferences = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s Workspace"


class OSAppWindow(models.Model):
    """Represents a running micro-app or widget inside the workspace."""
    workspace = models.ForeignKey(OSWorkspace, on_delete=models.CASCADE, related_name="windows")
    app_id = models.CharField(max_length=50, help_text="e.g., 'ai_notes', 'datalab'")
    title = models.CharField(max_length=255)
    
    # Window State
    is_minimized = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    
    # Spatial Data
    position_x = models.IntegerField(default=100)
    position_y = models.IntegerField(default=100)
    width = models.IntegerField(default=800)
    height = models.IntegerField(default=600)
    z_index = models.IntegerField(default=1)
    
    # App-Specific Data Storage
    state_data = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Window: {self.title} ({self.app_id})"


class OSNotification(models.Model):
    """Proactive notifications and system alerts for the user."""
    NOTIFICATION_TYPES = (
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    )
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="browser_notifications")
    title = models.CharField(max_length=255)
    message = models.TextField()
    type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='info')
    is_read = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.type.upper()}] {self.title} (Read: {self.is_read})"
