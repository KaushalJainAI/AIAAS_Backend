from django.contrib import admin

from .models import EvalCase, EvalResult, EvalReview, EvalRun, EvalSuite


class EvalCaseInline(admin.TabularInline):
    model = EvalCase
    extra = 0
    fields = ['order', 'name', 'goal', 'graders', 'weight', 'is_active']


@admin.register(EvalSuite)
class EvalSuiteAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'subagent', 'supervision', 'pass_threshold',
                    'is_active', 'updated_at']
    list_filter = ['supervision', 'is_active', 'created_at']
    search_fields = ['name', 'description', 'user__username']
    readonly_fields = ['slug', 'created_at', 'updated_at']
    inlines = [EvalCaseInline]


@admin.register(EvalCase)
class EvalCaseAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'suite', 'order', 'weight', 'is_active']
    list_filter = ['is_active', 'suite']
    search_fields = ['name', 'goal', 'reference']


class EvalResultInline(admin.TabularInline):
    model = EvalResult
    extra = 0
    can_delete = False
    fields = ['case_name', 'status', 'auto_passed', 'auto_score', 'review_state',
              'review_reason']
    readonly_fields = fields


@admin.register(EvalRun)
class EvalRunAdmin(admin.ModelAdmin):
    list_display = ['run_id', 'suite', 'subagent', 'status', 'score', 'passed',
                    'pending_review_count', 'grader_agreement', 'created_at']
    list_filter = ['status', 'supervision', 'created_at']
    search_fields = ['run_id', 'suite__name', 'subagent__name']
    readonly_fields = ['run_id', 'created_at', 'updated_at', 'duration_ms']
    inlines = [EvalResultInline]


@admin.register(EvalResult)
class EvalResultAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'run', 'status', 'auto_passed', 'auto_score',
                    'review_state', 'created_at']
    list_filter = ['status', 'review_state', 'created_at']
    search_fields = ['case_name', 'goal', 'answer']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(EvalReview)
class EvalReviewAdmin(admin.ModelAdmin):
    list_display = ['result', 'verdict', 'agreed_with_graders', 'reviewer', 'created_at']
    list_filter = ['verdict', 'agreed_with_graders', 'created_at']
    search_fields = ['comment', 'corrected_answer', 'reviewer__username']
    readonly_fields = ['agreed_with_graders', 'created_at', 'updated_at']
