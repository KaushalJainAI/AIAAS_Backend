"""
Permission Classes for Workflow Backend

Custom permissions for object-level and tier-based access control.
"""
from rest_framework import permissions


class IsOwnerOrAdmin(permissions.BasePermission):
    """
    Object-level permission: user owns the resource OR is staff/admin.
    """
    
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user or request.user.is_staff


class HasCredits(permissions.BasePermission):
    """
    Check if user has remaining credits.
    Used for endpoints that consume credits.
    """
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        try:
            return request.user.profile.has_credits
        except AttributeError:
            return False
