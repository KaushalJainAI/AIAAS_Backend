"""
Seeds execution history, notifications and MCP servers for one user.

Complements seed_demo.py, which covers agents, knowledge bases, chat and
skills but leaves Runs empty — so the trace view had nothing to show. Runs are
back-dated across two weeks with a realistic mix of outcomes, including a
failure with a node-level error, so the failure paths render too.

Idempotent: wipes only this user's execution logs and notifications first.

Run:  python manage.py shell < seed_runs.py
"""
from datetime import timedelta
import uuid

from django.contrib.auth.models import User
from django.utils import timezone

from agents.models import SubAgent
from logs.models import AgentStep, ExecutionLog
from notifications.models import Notification
from mcp_integration.models import MCPServer

EMAIL = "kaushal@nidhimasala.com"
user = User.objects.get(email=EMAIL)
now = timezone.now()

workflows = list(SubAgent.objects.filter(user=user).order_by("id"))
if not workflows:
    raise SystemExit("No agents for this user — run seed_demo.py first.")

# ---- reset (this user only) ----------------------------------------------
ExecutionLog.objects.filter(user=user).delete()
Notification.objects.filter(user=user).delete()
MCPServer.objects.filter(user=user).delete()

# ---- executions -----------------------------------------------------------
# (hours_ago, status, trigger, duration_ms, error, failing_node_index)
RUNS = [
    (0.3, "running", "webhook", None, None, None),
    (2, "completed", "schedule", 14_200, None, None),
    (5, "completed", "manual", 62_800, None, None),
    (7, "failed", "webhook", 3_100, "Google Drive credential expired (401 from oauth2.googleapis.com)", 2),
    (26, "completed", "schedule", 15_900, None, None),
    (30, "completed", "webhook", 8_450, None, None),
    (49, "cancelled", "manual", 800, None, None),
    (52, "completed", "schedule", 12_300, None, None),
    (74, "completed", "webhook", 6_720, None, None),
    (98, "failed", "schedule", 2_050, "Rate limited by the Sheets API after 3 retries", 4),
    (121, "completed", "schedule", 17_400, None, None),
    (150, "completed", "manual", 41_900, None, None),
    (170, "completed", "schedule", 13_050, None, None),
    (196, "completed", "webhook", 9_800, None, None),
]

# A believable pipeline: trigger, fetch, parse, reconcile, write, notify.
STEPS = [
    ("trigger", "Trigger", 12),
    ("gmail", "Fetch source data", 2_840),
    ("document", "Parse documents", 8_120),
    ("code", "Reconcile against records", 340),
    ("sheets", "Write results", 1_960),
    ("llm", "Draft summary", 920),
]

made = 0
for i, (hours, status, trigger, dur, err, fail_idx) in enumerate(RUNS):
    wf = workflows[i % len(workflows)]
    started = now - timedelta(hours=hours)
    ex = ExecutionLog.objects.create(
        execution_id=str(uuid.uuid4()),
        subagent=wf,
        user=user,
        status=status,
        trigger_type=trigger,
        started_at=started,
        completed_at=None if status == "running" else started + timedelta(milliseconds=dur or 0),
        duration_ms=dur,
        input_data={},
        output_data={} if status == "completed" else {},
        error_message=err or "",
        nodes_executed=len(STEPS) if status == "completed" else (fail_idx or 3),
        tokens_used=0 if status != "completed" else 4_200 + i * 310,
        credits_used=0 if status != "completed" else round(0.4 + i * 0.15, 2),
        supervision_level="error_only",
    )

    # created_at is auto_now_add, so it ignores started_at and every row lands
    # at "now". The API exposes created_at, so back-date it too or the whole
    # list reads as if it ran two minutes ago.
    ExecutionLog.objects.filter(pk=ex.pk).update(created_at=started)

    offset = 0
    for n, (ntype, nname, nms) in enumerate(STEPS):
        if status == "failed" and fail_idx is not None and n > fail_idx:
            break
        if status == "running" and n > 2:
            nstatus = "pending"
        elif status == "failed" and n == fail_idx:
            nstatus = "failed"
        elif status == "cancelled" and n > 1:
            nstatus = "skipped"
        else:
            nstatus = "completed"

        AgentStep.objects.create(
            execution=ex,
            call_id=f"call_{n + 1}",
            tool=ntype,
            status=nstatus,
            order=n,
            started_at=started + timedelta(milliseconds=offset),
            completed_at=started + timedelta(milliseconds=offset + nms),
            duration_ms=nms if nstatus == "completed" else (nms // 3 if nstatus == "failed" else 0),
            args={},
            result={},
            error_message=(err or "") if nstatus == "failed" else "",
        )
        offset += nms
    made += 1

# ---- notifications --------------------------------------------------------
NOTES = [
    ("error", "Vendor onboarding failed", "Google Drive credential expired. Reconnect it to resume.", 7, False),
    ("warning", "Sheets API rate limit", "Weekly payables digest retried 3 times before giving up.", 98, True),
    ("success", "Drive cleanup audit finished", "214 stale files identified, 8.4 GB. Waiting on your approval.", 5, False),
    ("info", "Knowledge base indexed", "Finance knowledge base finished indexing 4 documents.", 30, True),
]
for ntype, title, msg, hours, read in NOTES:
    n = Notification.objects.create(
        user=user, type=ntype, title=title, message=msg, data={}, is_read=read
    )
    Notification.objects.filter(pk=n.pk).update(created_at=now - timedelta(hours=hours))

# ---- MCP servers ----------------------------------------------------------
MCPServer.objects.create(
    user=user, name="Linear", type="sse", url="https://mcp.linear.app/sse",
    enabled=True, setup_notes="Issue tracking for the support triage agent.",
)
MCPServer.objects.create(
    user=user, name="filesystem", type="stdio", command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"],
    enabled=False, setup_notes="Disabled until the sandbox mount is reviewed.",
)

print()
print("executions:  ", ExecutionLog.objects.filter(user=user).count())
print("  steps:     ", AgentStep.objects.filter(execution__user=user).count())
print("  failed:    ", ExecutionLog.objects.filter(user=user, status="failed").count())
print("  running:   ", ExecutionLog.objects.filter(user=user, status="running").count())
print("notifications:", Notification.objects.filter(user=user).count())
print("mcp servers: ", MCPServer.objects.filter(user=user).count())
