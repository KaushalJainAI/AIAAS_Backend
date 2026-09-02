"""
Seeds agents and extraction rows.

Complements seed_demo.py (workflows, KBs, chat, skills) and seed_runs.py
(execution history). Without this the screens are correct but empty, and an
empty screen tells you nothing about whether it works.

The data is shaped to exercise the cases that actually matter rather than the
happy path:
  - extraction rows below the confidence threshold, so the review queue is not
    empty;
  - an agent that has never run, so the "Not run yet" branch is exercised.

Idempotent: removes only this user's rows in these apps first.

Run:  python manage.py shell < seed_improve.py
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from inference.models import ExtractedRow, ExtractionSchema, KnowledgeBase
from agents.models import SubAgent, Trigger

EMAIL = "kaushal@nidhimasala.com"
try:
    user = User.objects.get(email=EMAIL)
except User.DoesNotExist:
    # Local dev: seed_demo.py creates the `demo` user; don't fail the seed
    # just because the production account is not in this database.
    user = User.objects.get_or_create(username="demo", email="demo@aiaas.dev")[0]
now = timezone.now()

ExtractionSchema.objects.filter(user=user).delete()
SubAgent.objects.filter(user=user).delete()

kb = KnowledgeBase.objects.filter(user=user).first()

# ---------------------------------------------------------------- agents

def connector_ids(*names):
    """Curated connector ids by name — ids are per-install, so never literals.

    `agent_context['connectors']` holds `MCPServer` ids and is enforced by the
    runtime, so a seed that wrote the old presentation slugs ('gmail', 'gdrive')
    would produce agents whose selection is skipped and which therefore run
    unrestricted — the opposite of what these demo rows are meant to show.
    A name that is not seeded is dropped rather than faked.
    """
    from mcp_integration.models import MCPServer

    return list(
        MCPServer.objects.filter(name__in=names, user__isnull=True)
        .values_list("id", flat=True)
    )



AGENTS = [
    {
        "name": "Finance agent",
        "context": "Reads invoices from Gmail, reconciles them against the vendor "
                   "master, and chases anything overdue by more than 30 days.",
        "llm_provider": "openrouter",
        "llm_model": "anthropic/claude-sonnet-5",
        "tool_grants": {"codeExecution": True, "fileOps": True, "rag": True,
                        "shell": False, "webSearch": True, "scrape": False},
        "agent_context": {"connectors": connector_ids("Gmail", "Google Sheets"),
                          "knowledgeBases": [kb.id] if kb else [],
                          "skills": [], "useOrgContext": True, "useEnvironment": False},
        "trigger": {"mode": "maintenance", "cron": "0 9 * * 1"},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 500, "maxRunSeconds": 900, "egress": "none"},
        "sandbox": {"fileAccess": "scoped", "workdir": "/workspace", "venv": True},
    },
    {
        "name": "Support agent",
        "context": "Classifies inbound tickets, drafts a first reply and routes "
                   "anything it is not confident about to a human.",
        "llm_provider": "openrouter",
        "llm_model": "openai/gpt-5.6-luna",
        "tool_grants": {"codeExecution": False, "fileOps": False, "rag": True,
                        "shell": False, "webSearch": True, "scrape": True},
        "agent_context": {"connectors": connector_ids("Slack"), "knowledgeBases": [],
                          "skills": [], "useOrgContext": True, "useEnvironment": False},
        "trigger": {"mode": "goal", "cron": ""},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 400, "maxRunSeconds": 900, "egress": "none"},
        "sandbox": {"fileAccess": "none", "workdir": "/workspace", "venv": True},
    },
    {
        "name": "Data agent",
        "context": "Answers questions about uploaded spreadsheets by writing and "
                   "running Python in the sandbox.",
        "llm_provider": "nvidia",
        "llm_model": "nvidia/nemotron-3-super-120b-a12b",
        "tool_grants": {"codeExecution": True, "fileOps": True, "rag": True,
                        "shell": False, "webSearch": False, "scrape": False},
        "agent_context": {"connectors": [], "knowledgeBases": [kb.id] if kb else [],
                          "skills": [], "useOrgContext": True, "useEnvironment": False},
        "trigger": {"mode": "goal", "cron": ""},
        # Unattended is defensible here: everything it does is read-only and
        # reversible — it computes and reports, it never writes back or sends.
        "guardrails": {"autonomy": "full", "notifyOnHitl": False, "reviewAgent": False,
                       "spendCapRupees": 200, "maxRunSeconds": 900, "egress": "none"},
        "sandbox": {"fileAccess": "readonly", "workdir": "/workspace", "venv": True},
    },
    {
        # Never run, so the "Not run yet" branch on the card is exercised.
        "name": "Ops agent",
        "context": "Audits Drive for files nothing has opened in three years and "
                   "proposes what to archive.",
        "llm_provider": "openrouter",
        "llm_model": "openai/gpt-5.6-terra",
        "tool_grants": {"codeExecution": False, "fileOps": True, "rag": False,
                        "shell": False, "webSearch": False, "scrape": False},
        "agent_context": {"connectors": connector_ids("Google Drive", "Google Sheets"), "knowledgeBases": [],
                          "skills": [], "useOrgContext": True, "useEnvironment": True},
        "trigger": {"mode": "maintenance", "cron": "0 9 1 * *"},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 1000, "maxRunSeconds": 900, "egress": "none"},
        "sandbox": {"fileAccess": "scoped", "workdir": "/workspace", "venv": True},
    },
]

agents = {}
for spec in AGENTS:
    trigger = spec.pop("trigger", None)
    a = SubAgent.objects.create(
        user=user,
        status="active",
        prompt=spec.pop("context"),
        runtime_settings={"temperature": 0, "recursiveContext": True,
                          "compaction": True, "indexing": True},
        **spec,
    )
    if trigger and trigger.get("mode") == "maintenance":
        # The sweep refuses an unattended run unless the agent opts in — a
        # schedule is a way for something other than the user to spend credits.
        Trigger.objects.create(
            subagent=a, mode="schedule",
            config={"cron": trigger.get("cron", "")},
            enabled=True, overlap="skip",
            next_due_at=now + timedelta(days=1),
        )
    agents[a.name] = a
print(f"agents: {len(agents)}")

# ---------------------------------------------------------------- extraction

schema = ExtractionSchema.objects.create(
    user=user, name="Purchase invoices",
    description="Fields the accountant needs off every purchase invoice.",
    fields=[
        {"name": "vendor", "label": "Vendor", "type": "string", "required": True},
        {"name": "date", "label": "Date", "type": "date", "required": True},
        {"name": "gstin", "label": "GSTIN", "type": "string", "required": True},
        {"name": "total", "label": "Total", "type": "currency", "required": True},
    ],
    source_kind="gmail", source_ref='label "invoices"',
    confidence_threshold=0.8,
)

ROWS = [
    ("invoice_4471.pdf", "Shree Traders", "2026-07-12", "27AAECS1234F1Z5", "₹48,200", 0.99, {}),
    ("invoice_4472.pdf", "Acme Supplies", "2026-07-14", "29AAACA5678M1Z2", "₹12,750", 0.97, {}),
    ("invoice_4473.pdf", "Baxter Traders", "2026-07-15", "24AABCB9012K1Z8", "₹8,650", 0.62,
     {"total": 0.62, "gstin": 0.91}),
    ("invoice_4474.pdf", "Cole & Co", "2026-07-18", "27AADCC3456L1Z0", "₹69,600", 0.94, {}),
    ("invoice_4475.pdf", "Nirmal Packaging", "2026-07-21", "27AAFCN7890P1Z3", "₹5,120", 0.41,
     {"vendor": 0.41, "gstin": 0.55}),
    ("invoice_4476.pdf", "Vasant Metals", "2026-07-23", "27AAGCV2345H1Z9", "₹31,000", 0.88, {}),
    ("invoice_4477.pdf", "Deep Enterprises", "2026-07-25", "24AAHCD6789J1Z4", "₹1,980", 0.73,
     {"date": 0.73}),
]

for doc, vendor, date, gstin, total, conf, field_conf in ROWS:
    row = ExtractedRow.objects.create(
        schema=schema, document_name=doc,
        data={"vendor": vendor, "date": date, "gstin": gstin, "total": total},
        field_confidence=field_conf, confidence=conf,
    )
    row.apply_threshold()
    row.save(update_fields=["status"])

ExtractionSchema.objects.create(
    user=user, name="Vendor GST certificates",
    fields=[
        {"name": "vendor", "label": "Vendor", "type": "string"},
        {"name": "gstin", "label": "GSTIN", "type": "string"},
        {"name": "valid_from", "label": "Valid from", "type": "date"},
    ],
    source_kind="gdrive", source_ref="/compliance",
)

flagged = ExtractedRow.objects.filter(schema=schema, status="needs_review").count()
print(f"extraction: {len(ROWS)} rows, {flagged} need review")
print("done")
