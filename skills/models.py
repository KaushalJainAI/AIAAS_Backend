from django.db import models
from django.conf import settings

class Skill(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='skills'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    content = models.TextField()
    is_shared = models.BooleanField(default=False)
    category = models.CharField(max_length=100, blank=True)
    author_name = models.CharField(max_length=255, blank=True, help_text="Display name for the author")
    embedding = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.author_name and self.user:
            self.author_name = self.user.get_full_name() or self.user.username
        super().save(*args, **kwargs)
