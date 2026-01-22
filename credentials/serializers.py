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
        
        # specific handling to getting decrypted data
        try:
            decrypted_data = instance.get_credential_data()
        except Exception:
            decrypted_data = {}

        # The frontend expects 'fields' array with metadata + value.
        # We need the CredentialType schema to build this.
        cred_type = instance.credential_type
        schema = cred_type.fields_schema # list of dicts defining fields
        
        # Merge schema with values
        fields_response = []
        for field_def in schema:
            # field_def expected keys: name, label, type, required...
            key = field_def.get('name')
            
            # Mask sensitive values if not explicitly requested? 
            # For "Edit" view, we usually need the values. 
            # Password fields should probably be masked strictly speaking, but for "Edit" usage 
            # the user often wants to see if it's set or overwrite it.
            # Let's return the value as is.
            value = decrypted_data.get(key, '')
            
            # Construct entry matching frontend CredentialField expectation roughly
            # Frontend CredentialField: { key: string, label: string, type: string, value: string }
            # Backend field_def might use 'name' instead of 'key'.
            fields_response.append({
                'key': key,
                'label': field_def.get('label', key),
                'type': field_def.get('type', 'text'),
                'value': value
            })
            
        ret['fields'] = fields_response
        return ret

    def create(self, validated_data):
        data = validated_data.pop('data', {})
        credential = Credential.objects.create(**validated_data)
        if data:
            credential.set_credential_data(data)
            credential.save()
        return credential

    def update(self, instance, validated_data):
        data = validated_data.pop('data', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        if data is not None:
             instance.set_credential_data(data)
        
        instance.save()
        return instance
