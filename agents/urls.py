"""
Agents App URL Configuration

Agent CRUD, execution, approval/steering, triggers, HITL and platform
settings. An agent is a `SubAgent` row; runs start from
`agents/<id>/execute/`.
"""
from django.urls import path

from .views import (
    agents,
    builder,
    gallery,
    hitl,
    runs,
    triggers,
)

app_name = 'orchestrator'

urlpatterns = [
    # Agents
    path('agents/', agents.agent_list, name='agent_list'),
    path('agents/<int:agent_id>/', agents.agent_detail, name='agent_detail'),
    # Roll the configuration back to an earlier revision, recorded as a new one.
    path('agents/<int:agent_id>/revisions/<int:number>/restore/',
         agents.agent_restore_revision, name='agent_restore_revision'),
    # The builder's chat pane: a description in, knob changes out. Nothing is
    # saved — it proposes against the board the caller sends, and the save it
    # leads to is the ordinary PATCH above. Not nested under an agent id
    # because a brand-new agent has none.
    path('agents/configure/', builder.configure_agent, name='agent_configure'),
    # The builder chat for one saved agent, kept so a reload does not lose why
    # each knob moved. Written by `configure/` when it is given an agent id.
    path('agents/<int:agent_id>/builder-chat/', builder.builder_chat,
         name='agent_builder_chat'),
    path('agents/<int:agent_id>/execute/', runs.agent_execute, name='agent_execute'),
    path('agents/<int:agent_id>/approve/', runs.agent_approve, name='agent_approve'),
    path('agents/<int:agent_id>/reject/', runs.agent_reject, name='agent_reject'),
    path('agents/<int:agent_id>/steer/', runs.agent_steer, name='agent_steer'),
    path('agents/<int:agent_id>/autonomy/', runs.agent_autonomy, name='agent_autonomy'),
    # Stop one run. By execution, not by agent: an agent may have several
    # runs going, and "stop the agent" would not say which.
    path('runs/<str:execution_id>/cancel/', runs.run_cancel, name='run_cancel'),

    # Explore — everything installable, from two sources: the curated
    # catalogue (code, `agents/gallery.py`) and agents users have published
    # (`SharedAgent` rows). They are presented and installed identically;
    # install writes through the same serializer the builder saves through.
    path('templates/', gallery.template_list, name='template_list'),
    path('templates/install-pack/', gallery.template_install_pack,
         name='template_install_pack'),
    path('templates/<slug:slug>/', gallery.template_detail, name='template_detail'),
    path('templates/<slug:slug>/install/', gallery.template_install,
         name='template_install'),
    # Publishing is on the *agent*, not on the catalogue: what you share is
    # something you own, and the ownership check is the same
    # `user=request.user` lookup every other agent route makes.
    path('agents/<int:agent_id>/share/', gallery.agent_share, name='agent_share'),

    # The public catalogue: the second unauthenticated surface in this app,
    # after the webhook receiver. Reads only, `visibility='public'` only, and
    # 404 for everything else — a `link` share, a `platform` share, a withdrawn
    # one and a slug that never existed must be indistinguishable from outside,
    # or this becomes an oracle for enumerating what people published privately.
    path('public/agents/', gallery.public_agent_list, name='public_agent_list'),
    path('public/agents/<slug:slug>/', gallery.public_agent_detail,
         name='public_agent_detail'),

    # Triggers — how something other than the user starts a run.
    path('triggers/', triggers.trigger_list, name='trigger_list'),
    # Dry-run a cron expression before it is saved. Sits above the
    # `<int:trigger_id>` route only by convention — the converter would
    # not match 'preview' anyway.
    path('triggers/preview/', triggers.schedule_preview,
         name='schedule_preview'),
    path('triggers/<int:trigger_id>/', triggers.trigger_detail, name='trigger_detail'),
    # Fire a schedule now, through the sweep's own path — the only way to find
    # out whether a schedule works without waiting for its next slot.
    path('triggers/<int:trigger_id>/run/', triggers.trigger_run_now,
         name='trigger_run_now'),
    # Re-issue a webhook's secret. The URL is the only credential on the
    # public receiver, so it has to be revocable without losing the row.
    path('triggers/<int:trigger_id>/rotate/', triggers.trigger_rotate_secret,
         name='trigger_rotate_secret'),
    # The one unauthenticated route. The secret in the path is the credential;
    # see agents/views/triggers.py for why it answers 404 for every refusal.
    path('hooks/<str:secret>/', triggers.webhook_receive, name='webhook_receive'),

    # HITL
    path('hitl/pending/', hitl.pending_hitl_requests, name='pending_hitl'),
    path('hitl/<str:request_id>/respond/', hitl.respond_to_hitl, name='respond_hitl'),

    # `settings/update/` was retired 2026-09-18. It wrote a credential id no
    # model call reads, and a provider/model pair with no validation; the
    # account defaults are set through `auth/profile/`, which checks them.
]
