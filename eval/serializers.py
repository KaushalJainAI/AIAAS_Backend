"""
Wire contracts for `/api/eval/`.

`ModelSerializer` throughout, unlike `agents/views/agents.py`: there is no flat
camelCase builder shape to translate to here, so a hand-written mapping would be
a second copy of the columns with nothing to gain.

The one rule worth stating: **a grader spec is validated on write**, against the
same `eval.graders.REGISTRY` the runner dispatches through. A case that can be
saved is a case that can be run.
"""
from rest_framework import serializers

from workflow_backend.thresholds import (
    EVAL_MAX_CONCURRENCY,
    EVAL_REVIEW_QUEUE_LIMIT,
    EVAL_RUN_LIST_LIMIT,
)

from . import graders
from .models import EvalCase, EvalResult, EvalReview, EvalRun, EvalSuite
from .supervision import POLICIES


class EvalCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvalCase
        fields = [
            'id', 'suite', 'name', 'order', 'goal', 'input_data', 'reference',
            'graders', 'weight', 'tags', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'suite', 'created_at', 'updated_at']

    def validate_graders(self, value):
        try:
            return graders.validate_specs(value)
        except graders.GraderError as exc:
            raise serializers.ValidationError(str(exc))

    def validate_weight(self, value):
        if value <= 0:
            raise serializers.ValidationError('weight must be positive')
        return value


class EvalSuiteSerializer(serializers.ModelSerializer):
    case_count = serializers.SerializerMethodField(read_only=True)
    last_run = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EvalSuite
        fields = [
            'id', 'name', 'slug', 'description', 'subagent', 'pass_threshold',
            'supervision', 'sample_percent', 'reviewer', 'concurrency', 'tags',
            'is_active', 'case_count', 'last_run', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']

    def get_case_count(self, obj) -> int:
        return obj.cases.filter(is_active=True).count()

    def get_last_run(self, obj):
        run = obj.runs.order_by('-created_at').first()
        if run is None:
            return None
        return {
            'run_id': str(run.run_id),
            'status': run.status,
            'score': run.score,
            'passed': run.passed,
            'pending_review': run.pending_review_count,
            'created_at': run.created_at,
        }

    def validate_supervision(self, value):
        if value not in POLICIES:
            raise serializers.ValidationError(
                f'unknown supervision policy. Known: {", ".join(POLICIES)}'
            )
        return value

    def validate_pass_threshold(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError('pass_threshold is a fraction in [0, 1]')
        return value

    def validate_sample_percent(self, value):
        if not 0 <= value <= 100:
            raise serializers.ValidationError('sample_percent is a percentage in [0, 100]')
        return value

    def validate_concurrency(self, value):
        if not 1 <= value <= EVAL_MAX_CONCURRENCY:
            raise serializers.ValidationError(
                f'concurrency must be between 1 and {EVAL_MAX_CONCURRENCY}'
            )
        return value

    def validate(self, attrs):
        """Cross-tenant guard: every id a suite points at must be the caller's.

        Done here rather than in the view so it holds for create and update
        alike — an agent the caller does not own must not become the thing a
        suite runs against, and a reviewer they do not know must not start
        receiving their notifications.
        """
        user = self.context['request'].user
        agent = attrs.get('subagent')
        if agent is not None and agent.user_id != user.id:
            raise serializers.ValidationError({'subagent': 'No such agent.'})
        reviewer = attrs.get('reviewer')
        # A reviewer other than yourself is a person you are handing your
        # results to; until this app has a sharing model, the only reviewer a
        # suite may name is its owner.
        if reviewer is not None and reviewer.id != user.id:
            raise serializers.ValidationError(
                {'reviewer': 'A suite can only be reviewed by its owner for now.'}
            )
        return attrs


class EvalReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source='reviewer.username', read_only=True, default='')

    class Meta:
        model = EvalReview
        fields = [
            'id', 'verdict', 'agreed_with_graders', 'comment', 'corrected_answer',
            'reviewer', 'reviewer_name', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class EvalResultSerializer(serializers.ModelSerializer):
    review = EvalReviewSerializer(read_only=True)
    final_passed = serializers.BooleanField(read_only=True, allow_null=True)
    final_score = serializers.FloatField(read_only=True)
    execution_id = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EvalResult
        fields = [
            'id', 'run', 'case', 'case_name', 'goal', 'status', 'answer',
            'answer_truncated', 'auto_passed', 'auto_score', 'grades', 'weight',
            'review_state', 'review_reason', 'review', 'final_passed',
            'final_score', 'tokens', 'duration_ms', 'error_message',
            'execution_id', 'created_at',
        ]
        read_only_fields = fields

    def get_execution_id(self, obj):
        # The id, not the FK: it is what `/api/logs/executions/{id}/` takes,
        # so a client can go from a score to the full trace without a lookup.
        return str(obj.execution.execution_id) if obj.execution_id else None


class EvalRunSerializer(serializers.ModelSerializer):
    run_id = serializers.CharField(read_only=True)
    suite_name = serializers.CharField(source='suite.name', read_only=True, default='')
    agent_name = serializers.CharField(source='subagent.name', read_only=True, default='')
    revision_number = serializers.IntegerField(
        source='revision.number', read_only=True, default=None,
    )

    class Meta:
        model = EvalRun
        fields = [
            'run_id', 'suite', 'suite_name', 'subagent', 'agent_name',
            'revision', 'revision_number', 'status', 'supervision',
            'total_cases', 'passed_count', 'failed_count', 'error_count',
            'pending_review_count', 'score', 'passed', 'grader_agreement',
            'tokens_used', 'duration_ms', 'started_at', 'completed_at',
            'error_message', 'notes', 'created_at',
        ]
        read_only_fields = fields


# ---------------------------------------------------------------- input shapes

class RunRequestSerializer(serializers.Serializer):
    """Body of `POST /api/eval/suites/{id}/run/`."""

    #: Which agent to sweep. Optional: falls back to the suite's own
    #: `subagent`, and 400s if neither names one — a suite that runs against
    #: nothing is the one shape this endpoint cannot guess its way out of.
    agent_id = serializers.IntegerField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class ReviewInputSerializer(serializers.Serializer):
    """Body of `POST /api/eval/results/{id}/review/`."""

    verdict = serializers.ChoiceField(choices=['pass', 'fail', 'unsure'])
    comment = serializers.CharField(required=False, allow_blank=True, default='')
    corrected_answer = serializers.CharField(required=False, allow_blank=True, default='')


class RunListFilterSerializer(serializers.Serializer):
    suite_id = serializers.IntegerField(required=False, allow_null=True)
    agent_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.ChoiceField(
        choices=[c[0] for c in EvalRun.STATUS_CHOICES], required=False, allow_null=True,
    )
    limit = serializers.IntegerField(default=20, min_value=1, max_value=EVAL_RUN_LIST_LIMIT)


class QueueFilterSerializer(serializers.Serializer):
    suite_id = serializers.IntegerField(required=False, allow_null=True)
    run_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    limit = serializers.IntegerField(
        default=25, min_value=1, max_value=EVAL_REVIEW_QUEUE_LIMIT,
    )
