"""
Serializers for User Management and Authentication

Following NGU backend patterns with DRF serializers for API views.
"""
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from .models import UserProfile, APIKey, UsageTracking


User = get_user_model()


# ==================== User Serializers ====================

class UserSerializer(serializers.ModelSerializer):
    """Basic user information"""
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'date_joined']
        read_only_fields = ['id', 'date_joined']


class UserProfileSerializer(serializers.ModelSerializer):
    """User profile with tier and limits"""
    user = UserSerializer(read_only=True)
    
    class Meta:
        model = UserProfile
        fields = [
            'user', 'tier', 'compile_limit', 'execute_limit',
            'stream_connections', 'credits_remaining', 'credits_used_total',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'tier', 'compile_limit', 'execute_limit', 'stream_connections',
            'credits_used_total', 'created_at', 'updated_at'
        ]


class UserRegistrationSerializer(serializers.ModelSerializer):
    """User registration with password validation"""
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'password2', 'first_name', 'last_name']
    
    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({
                'password': "Passwords don't match."
            })
        return attrs
    
    def create(self, validated_data):
        validated_data.pop('password2')
        password = validated_data.pop('password')
        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()
        
        # Create user profile automatically
        UserProfile.objects.create(user=user)
        
        return user


# ==================== Auth Serializers ====================

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """JWT token with additional user claims and user data in response"""
    
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        
        # Add custom claims
        token['email'] = user.email
        token['username'] = user.username
        
        # Add tier if profile exists
        if hasattr(user, 'profile') and user.profile:
            token['tier'] = user.profile.tier
        else:
            token['tier'] = 'free'
        
        return token
    
    def validate(self, attrs):
        data = super().validate(attrs)
        
        # Get or create profile for user data
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        
        # Add user data to response for frontend
        data['user'] = {
            'id': self.user.id,
            'email': self.user.email,
            'name': f"{self.user.first_name} {self.user.last_name}".strip() or self.user.username,
            'tier': profile.tier,
            'credits': profile.credits_remaining,
            'createdAt': self.user.date_joined.isoformat(),
        }
        
        return data


class ChangePasswordSerializer(serializers.Serializer):
    """Password change validation"""
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password]
    )
    
    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Old password is incorrect')
        return value


# ==================== API Key Serializers ====================

class APIKeySerializer(serializers.ModelSerializer):
    """API key management - key visible only on creation"""
    key = serializers.CharField(read_only=True)
    
    class Meta:
        model = APIKey
        fields = [
            'id', 'name', 'key', 'key_prefix', 'is_active',
            'expires_at', 'last_used_at', 'created_at'
        ]
        read_only_fields = ['id', 'key', 'key_prefix', 'last_used_at', 'created_at']


class APIKeyCreateSerializer(serializers.ModelSerializer):
    """Create API key - returns full key once"""
    
    class Meta:
        model = APIKey
        fields = ['id', 'name', 'key', 'key_prefix', 'expires_at', 'created_at']
        read_only_fields = ['id', 'key', 'key_prefix', 'created_at']


# ==================== Usage Serializers ====================

class UsageTrackingSerializer(serializers.ModelSerializer):
    """Usage metrics for a day"""
    
    class Meta:
        model = UsageTracking
        fields = [
            'date', 'compile_count', 'execute_count', 'chat_count',
            'tokens_used', 'credits_used', 'estimated_cost'
        ]
        read_only_fields = '__all__'
