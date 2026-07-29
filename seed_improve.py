"""
Seeds agents, datasets, eval suites/runs, tuning jobs and extraction rows.

Complements seed_demo.py (workflows, KBs, chat, skills) and seed_runs.py
(execution history). Without this the four new screens are correct but empty,
and an empty screen tells you nothing about whether it works.

The data is shaped to exercise the cases that actually matter rather than the
happy path:
  - an eval suite whose latest run *regressed* (score up, two cases broken),
    because a rising average hiding a regression is the thing the page exists
    to catch;
  - a tuning job with no scores yet, so the "—" rather than "0" path renders;
  - extraction rows below the confidence threshold, so the review queue is not
    empty;
  - an agent that has never run, so the "Not run yet" branch is exercised.

Idempotent: removes only this user's rows in these four apps first.

Run:  python manage.py shell < seed_improve.py
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from datasets.models import Dataset, DatasetRow
from evals.models import EvalCase, EvalCaseResult, EvalRun, EvalSuite
from extraction.models import ExtractedRow, ExtractionSchema
from inference.models import KnowledgeBase
from orchestrator.models import Workflow
from tuning.models import TuningJob

EMAIL = "kaushal@nidhimasala.com"
user = User.objects.get(email=EMAIL)
now = timezone.now()

# Order matters: tuning PROTECTs its dataset, so jobs go before datasets.
TuningJob.objects.filter(user=user).delete()
EvalSuite.objects.filter(user=user).delete()
Dataset.objects.filter(user=user).delete()
ExtractionSchema.objects.filter(user=user).delete()
Workflow.objects.filter(user=user, kind="agent").delete()

kb = KnowledgeBase.objects.filter(user=user).first()

# ---------------------------------------------------------------- agents

AGENTS = [
    {
        "name": "Finance agent",
        "context": "Reads invoices from Gmail, reconciles them against the vendor "
                   "master, and chases anything overdue by more than 30 days.",
        "llm_provider": "openrouter",
        "llm_model": "anthropic/claude-sonnet-5",
        "tool_grants": {"codeExecution": True, "fileOps": True, "rag": True,
                        "shell": False, "webSearch": True, "scrape": False},
        "agent_context": {"connectors": ["gmail", "sheets"],
                          "knowledgeBases": [kb.id] if kb else [],
                          "skills": [], "useOrgContext": True, "useEnvironment": False},
        "trigger": {"mode": "maintenance", "cron": "0 9 * * 1"},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 500, "egress": "none"},
        "sandbox": {"fileAccess": "scoped", "workdir": "/workspace", "venv": True,
                    "cpu": 1, "memoryMb": 1024},
        "supervision_level": "error_only",
    },
    {
        "name": "Support agent",
        "context": "Classifies inbound tickets, drafts a first reply and routes "
                   "anything it is not confident about to a human.",
        "llm_provider": "openrouter",
        "llm_model": "openai/gpt-5.6-luna",
        "tool_grants": {"codeExecution": False, "fileOps": False, "rag": True,
                        "shell": False, "webSearch": True, "scrape": True},
        "agent_context": {"connectors": ["slack"], "knowledgeBases": [],
                          "skills": [], "useOrgContext": True, "useEnvironment": False},
        "trigger": {"mode": "goal", "cron": ""},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 400, "egress": "none"},
        "sandbox": {"fileAccess": "none", "workdir": "/workspace", "venv": True,
                    "cpu": 1, "memoryMb": 1024},
        "supervision_level": "error_only",
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
                       "spendCapRupees": 200, "egress": "none"},
        "sandbox": {"fileAccess": "readonly", "workdir": "/workspace", "venv": True,
                    "cpu": 2, "memoryMb": 2048},
        "supervision_level": "none",
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
        "agent_context": {"connectors": ["gdrive", "sheets"], "knowledgeBases": [],
                          "skills": [], "useOrgContext": True, "useEnvironment": True},
        "trigger": {"mode": "maintenance", "cron": "0 9 1 * *"},
        "guardrails": {"autonomy": "ask", "notifyOnHitl": True, "reviewAgent": False,
                       "spendCapRupees": 1000, "egress": "none"},
        "sandbox": {"fileAccess": "scoped", "workdir": "/workspace", "venv": True,
                    "cpu": 1, "memoryMb": 1024},
        "supervision_level": "error_only",
    },
]

agents = {}
for spec in AGENTS:
    a = Workflow.objects.create(
        user=user, kind="agent", nodes=[], edges=[], status="active",
        workflow_settings={"temperature": 0, "recursiveContext": True,
                           "compaction": True, "indexing": True},
        **spec,
    )
    agents[a.name] = a
print(f"agents: {len(agents)}")

# ---------------------------------------------------------------- datasets

DATASETS = [
    ("Invoice fields — gold", "corrected", 80, 10, 10, 240),
    ("Ticket intents", "captured", 70, 15, 15, 180),
    ("House style replies", "corrected", 90, 10, 0, 60),
    ("GSTIN edge cases", "uploaded", 0, 0, 0, 8),   # too thin to tune on
]

datasets = {}
for name, source, tr, va, te, n in DATASETS:
    ds = Dataset.objects.create(
        user=user, name=name, source=source,
        train_pct=tr, val_pct=va, test_pct=te,
    )
    # Spread rows across the declared split so the split tabs show real counts.
    rows = []
    for i in range(n):
        if te and i % 10 == 9:
            split = "test"
        elif va and i % 10 == 8:
            split = "val"
        else:
            split = "train"
        rows.append(DatasetRow(
            dataset=ds, split=split,
            inputs={"document": f"invoice_{4400 + i}.pdf"},
            expected={"total": f"₹{(i * 137) % 90000 + 500:,}"},
            note="Model read the rounded-off line as the total" if i % 17 == 0 else "",
        ))
    DatasetRow.objects.bulk_create(rows)
    datasets[name] = ds
print(f"datasets: {len(datasets)} / {DatasetRow.objects.filter(dataset__user=user).count()} rows")

# ---------------------------------------------------------------- evals

CASES = [
    ("inv-011", "Handwritten total, smudged", "₹8,650"),
    ("inv-024", "Two invoices in one PDF", "2 records"),
    ("inv-037", "Rounded-off line, IGST split", "₹48,200"),
    ("inv-052", "Vendor name in Devanagari", "श्री ट्रेडर्स"),
    ("inv-068", "Credit note, negative total", "-₹4,100"),
    ("inv-074", "Two-page invoice, total on page 2", "₹15,900"),
    ("inv-090", "Scanned at an angle", "₹2,340"),
]

suite = EvalSuite.objects.create(
    user=user, name="Invoice extraction accuracy",
    description="Does the extractor still read the awkward invoices correctly?",
    agent=agents["Finance agent"], dataset=datasets["Invoice fields — gold"],
)
cases = {k: EvalCase.objects.create(suite=suite, key=k, description=d,
                                    inputs={"document": k}, expected={"value": e})
         for k, d, e in CASES}

# Two runs. The second scores *higher* overall while breaking two cases that
# passed before — the exact situation the regression panel exists to surface.
FIRST = {"inv-011": True, "inv-024": False, "inv-037": True, "inv-052": True,
         "inv-068": True, "inv-074": False, "inv-090": False}
SECOND = {"inv-011": True, "inv-024": True, "inv-037": True, "inv-052": False,
          "inv-068": False, "inv-074": True, "inv-090": True}

for offset, outcomes, model in (
    (timedelta(days=3), FIRST, "anthropic/claude-sonnet-5"),
    (timedelta(hours=2), SECOND, "openai/gpt-5.6-luna"),
):
    run = EvalRun.objects.create(
        suite=suite, user=user, status="completed",
        provider="openrouter", model=model,
        total_cases=len(outcomes), passed_cases=sum(outcomes.values()),
        started_at=now - offset, completed_at=now - offset + timedelta(minutes=4),
    )
    # created_at is auto_now_add, so back-date it after the fact.
    EvalRun.objects.filter(id=run.id).update(created_at=now - offset)
    for key, passed in outcomes.items():
        EvalCaseResult.objects.create(
            run=run, case=cases[key], passed=passed,
            got={"value": cases[key].expected["value"] if passed else "—"},
            reason="" if passed else "Read the wrong line as the total",
            duration_ms=800 + (hash(key) % 1200),
        )

second = EvalRun.objects.filter(suite=suite).order_by("-created_at").first()
print(f"evals: {suite.name} — latest {second.score}%, regressions {second.regressions()}")

# A second suite that has never been run.
EvalSuite.objects.create(
    user=user, name="Reply tone & house style",
    description="Do drafted replies sound like us?",
    agent=agents["Support agent"], dataset=datasets["House style replies"],
)

# ---------------------------------------------------------------- tuning

TuningJob.objects.create(
    user=user, name="invoice-extract-v3", base_model="openai/gpt-5.6-luna",
    dataset=datasets["Invoice fields — gold"], status="deployed",
    epochs_total=3, epochs_done=3,
    accuracy=96.1, baseline_accuracy=94.2,
    cost_per_1k_paise=42, baseline_cost_per_1k_paise=210,
    tuned_model_id="ft:gpt-5.6-luna:aiaas:invoice-extract-v3",
    completed_at=now - timedelta(days=2),
)
TuningJob.objects.create(
    user=user, name="ticket-intents-v2", base_model="openai/gpt-5.6-luna",
    dataset=datasets["Ticket intents"], status="training",
    epochs_total=4, epochs_done=2,
)
TuningJob.objects.create(
    user=user, name="house-style-v1", base_model="openai/gpt-5.6-luna",
    dataset=datasets["House style replies"], status="failed",
    epochs_total=3, epochs_done=1,
    error_message="Training diverged after epoch 1 — the set is probably too small "
                  "for three epochs at this learning rate.",
    completed_at=now - timedelta(days=5),
)
print(f"tuning: {TuningJob.objects.filter(user=user).count()} jobs")

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
