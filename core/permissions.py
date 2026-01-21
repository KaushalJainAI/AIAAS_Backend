"""
Permission Classes for Workflow Backend

Custom permissions for object-level and tier-based access control.
"""
from rest_framework import permissions
from .models import APIKey


class IsOwner(permissions.BasePermission):
    """
    Object-level permission: user must own the resource.
    Expects the object to have a 'user' attribute.
    """
    
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user


class IsOwnerOrAdmin(permissions.BasePermission):
    """
    Object-level permission: user owns the resource OR is staff/admin.
    """
    
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user or request.user.is_staff


class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Object-level permission: owner can modify, others can only read.
    """
    
    def has_object_permission(self, request, view, obj):
        # Read permissions allowed for any request
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write permissions only for owner
        return obj.user == request.user


class HasAPIKey(permissions.BasePermission):
    """
    Check if request was authenticated via API key.
    The API key object is set as request.auth by APIKeyAuthentication.
    """
    
    def has_permission(self, request, view):
        return isinstance(request.auth, APIKey)


class TierPermission(permissions.BasePermission):
    """
    Base class for tier-based permissions.
    Subclass and set required_tier attribute.
    """
    required_tier = 'free'
    
    TIER_ORDER = {'free': 0, 'pro': 1, 'enterprise': 2}
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        try:
            user_tier = request.user.profile.tier
        except AttributeError:
            return False
        
        user_level = self.TIER_ORDER.get(user_tier, 0)
        required_level = self.TIER_ORDER.get(self.required_tier, 0)
        
        return user_level >= required_level


class IsProTier(TierPermission):
    """Requires Pro tier or higher"""
    required_tier = 'pro'


class IsEnterpriseTier(TierPermission):
    """Requires Enterprise tier"""
    required_tier = 'enterprise'


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
