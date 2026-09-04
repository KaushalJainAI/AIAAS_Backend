from rest_framework import serializers
from .models import Credential, CredentialType, CredentialAuditLog

class CredentialAuditLogSerializer(serializers.ModelSerializer):
    credential_name = serializers.SerializerMethodField()
    credential_type_name = serializers.SerializerMethodField()

    # A deleted credential's FK is nulled (SET_NULL), so the snapshot taken at
    # event time is what keeps deletion history meaningful.
    def get_credential_name(self, obj):
        if obj.credential_id:
            return obj.credential.name
        return obj.snapshot.get('name') if obj.snapshot else None

    def get_credential_type_name(self, obj):
        if obj.credential_id:
            return obj.credential.credential_type.name
        return obj.snapshot.get('credential_type') if obj.snapshot else None

    class Meta:
        model = CredentialAuditLog
        fields = [
            'id', 'credential', 'credential_name', 'credential_type_name',
            'action', 'workflow_id', 'timestamp', 'ip_address', 'user_agent'
        ]
        read_only_fields = ['id', 'credential', 'credential_name', 'credential_type_name', 'action', 'workflow_id', 'timestamp', 'ip_address', 'user_agent']

class CredentialTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CredentialType
        fields = ['id', 'name', 'slug', 'service_identifier', 'description', 'icon', 'auth_method', 'fields_schema', 'oauth_config']


class CredentialTypeRelatedField(serializers.PrimaryKeyRelatedField):
    """Accept credential type by integer primary key or slug (active types only)."""

    def get_queryset(self):
        return CredentialType.objects.filter(is_active=True)

    def to_internal_value(self, data):
        queryset = self.get_queryset()
        if isinstance(data, str) and not data.isdigit():
            try:
                return queryset.get(slug=data)
            except CredentialType.DoesNotExist:
                self.fail('does_not_exist', pk_value=data)
        return super().to_internal_value(data)


class CredentialSerializer(serializers.ModelSerializer):
    credential_type = CredentialTypeRelatedField(
        queryset=CredentialType.objects.filter(is_active=True),
    )
    credential_type_display = serializers.CharField(source='credential_type.name', read_only=True)
    # The 'data' field is virtual - it's decrypted on read, and encrypted on write via set_credential_data
    data = serializers.DictField(write_only=True, required=False)
    # On read, we might want to return masked data or just the keys? 
    # For now, let's assume we return the full decrypted data for the owner to see/edit, 
    # OR we follow the pattern of only returning metadata and having a separate "get details" if needed.
    # The frontend expects 'fields' which seems to be the configured values.
    # Let's map 'data' -> 'fields' in representation if needed, or just return 'data'.
    # Looking at frontend `Credential` interface:
    # fields: CredentialField[]; where CredentialField has key, label, type, value.
    # The backend stores a simple dict {key: value}.
    # We need to combine the Type schema with the stored Values to produce the full 'fields' list for frontend.
    # Alias is_active as is_valid to match frontend expectations
    is_valid = serializers.BooleanField(source='is_active', read_only=True)

    class Meta:
        model = Credential
        fields = [
            'id', 'name', 'credential_type', 'credential_type_display', 
            'is_valid', 'is_verified', 'last_used_at', 'last_error', 
            'created_at', 'updated_at', 'data'
        ]
        read_only_fields = ['created_at', 'updated_at', 'last_used_at', 'last_error', 'is_verified', 'is_valid']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        
        # 1. Get Decrypted Data
        try:
            decrypted_data = instance.get_credential_data()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to decrypt data for credential {instance.id}: {e}")
            decrypted_data = {}

        # 2. Get Public Data
        public_data = instance.public_metadata or {}
        
        # 3. Merge (Decrypted overrides public if collision, though shouldn't happen)
        full_data = {**public_data, **decrypted_data}

        # The frontend expects 'fields' array with metadata + value.
        cred_type = instance.credential_type
        schema = cred_type.fields_schema # list of dicts defining fields
        
        fields_response = []
        for field_def in schema:
            key = field_def.get('name')
            is_public = field_def.get('public', False)
            field_type = field_def.get('type', 'text')
            
            value = full_data.get(key, '')
            
            # Mask sensitive values if they exist and are not marked as public
            if value and not is_public and field_type in ['password', 'secret', 'token']:
                # Show only last 4 chars for better UX, or just stars
                if len(str(value)) > 8:
                    value = f"********{str(value)[-4:]}"
                else:
                    value = "********"
            
            fields_response.append({
                'key': key,
                'label': field_def.get('label', key),
                'type': field_type,
                'value': value
            })
            
        ret['fields'] = fields_response
        return ret

    def validate(self, attrs):
        # The `data` dict is unconstrained by DRF, so the type's own
        # `fields_schema` has to be enforced here — previously a credential
        # missing required fields saved fine and only failed at run time.
        raw_data = attrs.get('data')
        if raw_data is not None:
            credential_type = attrs.get('credential_type') or (
                self.instance.credential_type if self.instance else None
            )
            if credential_type is not None:
                # Layer incoming values over what is already stored, so an
                # untouched required field doesn't fail a partial update.
                merged = {}
                if self.instance:
                    try:
                        merged.update(self.instance.get_credential_data())
                    except Exception:
                        # Undecryptable existing blob: validate what arrives.
                        pass
                    merged.update(self.instance.public_metadata or {})
                merged.update(raw_data)
                from credentials.manager import get_credential_manager
                errors = get_credential_manager().validate_against_schema(
                    merged, credential_type
                )
                if errors:
                    raise serializers.ValidationError({'data': errors})
        return attrs

    def create(self, validated_data):
        validated_data.pop('data', None)
        raw_data = self.initial_data.get('data', {})
        
        # We need to assign user before we can access instance.credential_type easily 
        # (though validated_data has credential_type id)
        credential = Credential(**validated_data)
        
        # Helper to split data
        self._save_credential_data(credential, raw_data)
        
        return credential

    def update(self, instance, validated_data):
        validated_data.pop('data', None)
        raw_data = self.initial_data.get('data', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        if raw_data is not None:
             # Merge, don't replace: the read serializer masks secrets, so the
             # client can only send back the fields the user actually retyped.
             # A straight replace would wipe every untouched secret.
             self._save_credential_data(instance, raw_data, save=False, merge=True)
             # Reset verification status on sensitive data update
             instance.is_verified = False
             instance.last_error = "" # Set to empty string as field is NOT NULL
        
        instance.save()
        return instance

    def _save_credential_data(self, credential, data, save=True, merge=False):
        """
        Splits data into public and encrypted based on type schema.

        With merge=True the incoming keys are layered over what is already
        stored, so omitting a key leaves it untouched (used on update).
        """
        # Fetch type (if not loaded)
        if not credential.credential_type_id:
             # Should be handled by validation, but safe check
             return
             
        # If credential_type is lazy/id, fetch it. 
        # But 'credential' instance from create() isn't saved yet, 
        # so we rely on what we passed to constructor or have in instance.
        
        # In create(), credential.credential_type is likely accessible b/c we passed an instance to FK field
        # or we passed ID and Django resolves it on access if strictly needed?
        # Let's simple check schema.
        
        schema = credential.credential_type.fields_schema
        
        public_payload = {}
        encrypted_payload = {}
        
        public_keys = {f['name'] for f in schema if f.get('public')}

        if merge:
            public_payload.update(credential.public_metadata or {})
            try:
                encrypted_payload.update(credential.get_credential_data())
            except Exception:
                # Undecryptable existing blob: better to overwrite with what the
                # user just supplied than to fail the whole update.
                pass

        for key, value in data.items():
            if key in public_keys:
                public_payload[key] = value
            else:
                encrypted_payload[key] = value
                
        credential.public_metadata = public_payload
        credential.set_credential_data(encrypted_payload)
        
        if save:
            credential.save()

class CredentialOAuthInitSerializer(serializers.Serializer):
    """Serializer for OAuth initialization parameters."""
    redirect_uri = serializers.URLField(required=True)
    scopes = serializers.ListField(child=serializers.CharField(), required=False)

class CredentialOAuthCallbackSerializer(serializers.Serializer):
    """Serializer for OAuth callback parameters."""
    code = serializers.CharField(required=True)
    redirect_uri = serializers.URLField(required=True)
    name = serializers.CharField(required=False, default='Google Account')
