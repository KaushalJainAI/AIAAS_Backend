from rest_framework import serializers
from .models import Credential, CredentialType

class CredentialTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CredentialType
        fields = ['id', 'name', 'slug', 'description', 'icon', 'auth_method', 'fields_schema', 'oauth_config']


class CredentialSerializer(serializers.ModelSerializer):
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
            
            value = full_data.get(key, '')
            
            fields_response.append({
                'key': key,
                'label': field_def.get('label', key),
                'type': field_def.get('type', 'text'),
                'value': value
            })
            
        ret['fields'] = fields_response
        return ret

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
             self._save_credential_data(instance, raw_data, save=False)
        
        instance.save()
        return instance

    def _save_credential_data(self, credential, data, save=True):
        """
        Splits data into public and encrypted based on type schema
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
        
        for key, value in data.items():
            if key in public_keys:
                public_payload[key] = value
            else:
                encrypted_payload[key] = value
                
        credential.public_metadata = public_payload
        credential.set_credential_data(encrypted_payload)
        
        if save:
            credential.save()
