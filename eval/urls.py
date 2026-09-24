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
    path('judge/calibration/', views.judge_calibration, name='judge_calibration'),

    # Suites and their cases
    path('suites/', views.suite_list, name='suite_list'),
    path('suites/from-template/', views.suite_from_template, name='suite_from_template'),
    path('starter-kits/', views.starter_kits, name='starter_kits'),
    path('suites/<int:suite_id>/', views.suite_detail, name='suite_detail'),
    path('suites/<int:suite_id>/cases/', views.case_list, name='case_list'),
    path('suites/<int:suite_id>/generate/', views.suite_generate, name='suite_generate'),
    path('suites/<int:suite_id>/import-runs/', views.suite_import_runs, name='suite_import_runs'),
    path('suites/<int:suite_id>/drafts/', views.suite_review_drafts, name='suite_review_drafts'),
    path('cases/<int:case_id>/', views.case_detail, name='case_detail'),

    # Worlds — the fake situations a suite's cases share. Generated, never
    # hand-written; accepted on the Evals page only.
    path('suites/<int:suite_id>/world/', views.suite_world, name='suite_world'),
    path('suites/<int:suite_id>/world/generate/', views.suite_world_generate,
         name='suite_world_generate'),
    path('worlds/<int:world_id>/accept/', views.world_accept, name='world_accept'),
    path('worlds/<int:world_id>/', views.world_detail, name='world_detail'),

    # Sweeps
    path('suites/<int:suite_id>/run/', views.suite_run, name='suite_run'),
    path('runs/', views.run_list, name='run_list'),
    path('runs/<str:run_id>/', views.run_detail, name='run_detail'),
    path('runs/<str:run_id>/cancel/', views.run_cancel, name='run_cancel'),

    # A bad run becomes a test.
    path('cases/from-run/', views.case_from_run, name='case_from_run'),

    # Supervision — who checks the checker.
    path('reviews/pending/', views.review_queue, name='review_queue'),
    path('results/<int:result_id>/review/', views.submit_review, name='submit_review'),

    # Scorecard
    path('agents/<int:agent_id>/scorecard/', views.agent_scorecard, name='agent_scorecard'),
]
