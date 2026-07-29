from rest_framework import serializers

from .models import TuningJob


class TuningJobSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='dataset.name', read_only=True)
    dataset_rows = serializers.IntegerField(source='dataset.row_count', read_only=True)
    accuracy_delta = serializers.FloatField(read_only=True)
    cost_saving_pct = serializers.FloatField(read_only=True)
    progress_pct = serializers.IntegerField(read_only=True)

    class Meta:
        model = TuningJob
        fields = [
            'id', 'name', 'base_model', 'dataset', 'dataset_name', 'dataset_rows',
            'status', 'epochs_total', 'epochs_done', 'progress_pct',
            'accuracy', 'baseline_accuracy', 'accuracy_delta',
            'cost_per_1k_paise', 'baseline_cost_per_1k_paise', 'cost_saving_pct',
            'tuned_model_id', 'error_message',
            'created_at', 'updated_at', 'completed_at',
        ]
        read_only_fields = [
            'id', 'status', 'epochs_done', 'progress_pct', 'accuracy',
            'baseline_accuracy', 'accuracy_delta', 'cost_per_1k_paise',
            'baseline_cost_per_1k_paise', 'cost_saving_pct', 'tuned_model_id',
            'error_message', 'created_at', 'updated_at', 'completed_at',
        ]

    def validate_dataset(self, value):
        if value.user_id != self.context['request'].user.id:
            raise serializers.ValidationError('No such dataset.')
        return value

    def validate(self, attrs):
        # Tuning on a handful of rows produces a model that is confidently wrong,
        # which is worse than not tuning at all. Refuse rather than waste the run.
        dataset = attrs.get('dataset') or getattr(self.instance, 'dataset', None)
        if dataset and dataset.row_count < 10:
            raise serializers.ValidationError(
                {'dataset': f'{dataset.name} has {dataset.row_count} rows. '
                            'Tuning needs at least 10 to produce anything meaningful.'}
            )
        return attrs
