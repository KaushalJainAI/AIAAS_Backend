from rest_framework import serializers

from .models import ExtractedRow, ExtractionSchema

ALLOWED_FIELD_TYPES = {'string', 'number', 'date', 'currency', 'boolean'}


class ExtractedRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractedRow
        fields = [
            'id', 'document_name', 'document', 'data', 'field_confidence',
            'confidence', 'status', 'reviewed_at', 'created_at',
        ]
        read_only_fields = ['id', 'status', 'reviewed_at', 'created_at']


class ExtractionSchemaSerializer(serializers.ModelSerializer):
    field_count = serializers.IntegerField(read_only=True)
    row_count = serializers.IntegerField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExtractionSchema
        fields = [
            'id', 'name', 'description', 'fields', 'field_count',
            'source_kind', 'source_ref', 'confidence_threshold',
            'row_count', 'review_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_confidence_threshold(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError('Confidence is a fraction between 0 and 1.')
        return value

    def validate_fields(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError('A schema needs at least one field.')

        names = []
        for i, field in enumerate(value):
            if not isinstance(field, dict):
                raise serializers.ValidationError(f'fields[{i}] must be an object.')
            name = (field.get('name') or '').strip()
            if not name:
                raise serializers.ValidationError(f'fields[{i}] needs a name.')
            ftype = field.get('type', 'string')
            if ftype not in ALLOWED_FIELD_TYPES:
                raise serializers.ValidationError(
                    f"fields[{i}].type '{ftype}' is not one of {', '.join(sorted(ALLOWED_FIELD_TYPES))}."
                )
            names.append(name)

        # Duplicate names would silently overwrite each other in the row's data
        # dict, losing a column with no error anywhere.
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise serializers.ValidationError(f"Duplicate field names: {', '.join(sorted(dupes))}.")
        return value
