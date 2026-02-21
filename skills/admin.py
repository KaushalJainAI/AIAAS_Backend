from django.contrib import admin
from .models import Skill

@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'author_name', 'category', 'is_shared', 'updated_at')
    list_filter = ('is_shared', 'category', 'created_at')
    search_fields = ('title', 'description', 'content', 'author_name', 'user__username')
    readonly_fields = ('created_at', 'updated_at')
    
    def save_model(self, request, obj, form, change):
        if not obj.user_id:
            obj.user = request.user
        super().save_model(request, obj, form, change)
