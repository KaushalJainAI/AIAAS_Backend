"""
Eval app URL configuration — suites, cases, sweeps, and the review queue.

A sweep starts at exactly one place (`suites/<id>/run/`) and is read at exactly
one place (`runs/<run_id>/`), matching how agent runs are routed in
`agents/urls.py`.
"""
from django.urls import path

from . import views

app_name = 'eval'

urlpatterns = [
    # What a case can assert. Served from the runner's registry.
    path('graders/', views.grader_catalog, name='grader_catalog'),

    # Suites and their cases
    path('suites/', views.suite_list, name='suite_list'),
    path('suites/<int:suite_id>/', views.suite_detail, name='suite_detail'),
    path('suites/<int:suite_id>/cases/', views.case_list, name='case_list'),
    path('cases/<int:case_id>/', views.case_detail, name='case_detail'),

    # Sweeps
    path('suites/<int:suite_id>/run/', views.suite_run, name='suite_run'),
    path('runs/', views.run_list, name='run_list'),
    path('runs/<str:run_id>/', views.run_detail, name='run_detail'),
    path('runs/<str:run_id>/cancel/', views.run_cancel, name='run_cancel'),

    # Supervision — who checks the checker.
    path('reviews/pending/', views.review_queue, name='review_queue'),
    path('results/<int:result_id>/review/', views.submit_review, name='submit_review'),

    # Scorecard
    path('agents/<int:agent_id>/scorecard/', views.agent_scorecard, name='agent_scorecard'),
]
