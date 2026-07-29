from rest_framework import serializers

from .models import Dataset, DatasetRow


class DatasetRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = DatasetRow
        fields = ['id', 'inputs', 'expected', 'split', 'note', 'source_execution', 'created_at']
        read_only_fields = ['id', 'created_at']


class DatasetSerializer(serializers.ModelSerializer):
    # Counted in the queryset, not per row, so listing N datasets stays one query.
    row_count = serializers.IntegerField(source='rows_total', read_only=True)
    split_label = serializers.CharField(read_only=True)
    # Which suites and tuning jobs consume this dataset — the question you
    # actually ask before editing one is "what breaks if I change this?".
    used_by = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            'id', 'name', 'description', 'source',
            'train_pct', 'val_pct', 'test_pct', 'split_label',
            'row_count', 'used_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_used_by(self, obj):
        names = list(obj.eval_suites.values_list('name', flat=True))
        names += list(obj.tuning_jobs.values_list('name', flat=True))
        return names

    def validate(self, attrs):
        # A split that doesn't add up silently mis-sizes every downstream job.
        def pick(key):
            return attrs.get(key, getattr(self.instance, key, 0) if self.instance else 0)

        total = pick('train_pct') + pick('val_pct') + pick('test_pct')
        if total not in (0, 100):
            raise serializers.ValidationError(
                {'train_pct': f'Splits must add up to 100 (or all be 0 for no split); got {total}.'}
            )
        return attrs
