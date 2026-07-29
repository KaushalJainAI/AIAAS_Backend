from rest_framework import serializers

from .models import EvalCase, EvalCaseResult, EvalRun, EvalSuite


class EvalCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvalCase
        fields = ['id', 'key', 'description', 'inputs', 'expected', 'weight', 'created_at']
        read_only_fields = ['id', 'created_at']


class EvalCaseResultSerializer(serializers.ModelSerializer):
    key = serializers.CharField(source='case.key', read_only=True)
    description = serializers.CharField(source='case.description', read_only=True)
    expected = serializers.JSONField(source='case.expected', read_only=True)

    class Meta:
        model = EvalCaseResult
        fields = ['id', 'key', 'description', 'expected', 'got', 'passed', 'reason', 'duration_ms']
        read_only_fields = fields


class EvalRunSerializer(serializers.ModelSerializer):
    score = serializers.FloatField(read_only=True)
    delta = serializers.SerializerMethodField()
    regressions = serializers.SerializerMethodField()
    suite_name = serializers.CharField(source='suite.name', read_only=True)

    class Meta:
        model = EvalRun
        fields = [
            'id', 'suite', 'suite_name', 'provider', 'model', 'status',
            'total_cases', 'passed_cases', 'score', 'delta', 'regressions',
            'error_message', 'started_at', 'completed_at', 'created_at',
        ]
        read_only_fields = [
            'id', 'status', 'total_cases', 'passed_cases', 'score', 'delta',
            'regressions', 'error_message', 'started_at', 'completed_at', 'created_at',
        ]

    def get_delta(self, obj):
        """Change against the previous run, or None when there is nothing to
        compare — which is different from 'no change' and shown differently."""
        prev = obj.previous()
        return None if prev is None else round(obj.score - prev.score, 1)

    def get_regressions(self, obj):
        return obj.regressions()


class EvalSuiteSerializer(serializers.ModelSerializer):
    case_count = serializers.IntegerField(source='cases_total', read_only=True)
    latest = serializers.SerializerMethodField()
    agent_name = serializers.CharField(source='agent.name', read_only=True, default=None)
    dataset_name = serializers.CharField(source='dataset.name', read_only=True, default=None)

    class Meta:
        model = EvalSuite
        fields = [
            'id', 'name', 'description', 'agent', 'agent_name', 'dataset', 'dataset_name',
            'case_count', 'latest', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_latest(self, obj):
        run = obj.latest_run
        if not run:
            return None
        prev = run.previous()
        return {
            'id': run.id,
            'status': run.status,
            'score': run.score,
            'delta': None if prev is None else round(run.score - prev.score, 1),
            'model': run.model,
            'regressions': len(run.regressions()),
            'created_at': run.created_at,
        }

    def validate_agent(self, value):
        # The suite would otherwise point at an agent the caller cannot see.
        if value and value.user_id != self.context['request'].user.id:
            raise serializers.ValidationError('No such agent.')
        return value

    def validate_dataset(self, value):
        if value and value.user_id != self.context['request'].user.id:
            raise serializers.ValidationError('No such dataset.')
        return value
