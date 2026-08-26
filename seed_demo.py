"""
Idempotent demo seed for interview walkthrough.
Creates a single `demo` user (email login) with curated data across the
main product surfaces: Agents, Knowledge Bases + Documents, AI Chat,
and Skills. Re-running wipes ONLY the demo user's owned rows and recreates
them, so it is safe to run repeatedly. It does not touch other users.

Run:  /home/ec2-user/.venvs/shared/bin/python manage.py shell < seed_demo.py
"""
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils import timezone
from datetime import timedelta

from agents.models import SubAgent
from inference.models import KnowledgeBase, Document
from chat.models import ChatSession, ChatMessage
from skills.models import Skill

EMAIL = "demo@aiaas.dev"
USERNAME = "demo"
PASSWORD = "Demo@2026"

# ---- user -----------------------------------------------------------------
user, created = User.objects.get_or_create(
    username=USERNAME,
    defaults={"email": EMAIL, "first_name": "Demo", "last_name": "User"},
)
user.email = EMAIL
user.first_name, user.last_name = "Demo", "User"
user.is_active = True
user.set_password(PASSWORD)
user.save()
if hasattr(user, "profile") and user.profile:
    try:
        user.profile.tier = "pro"
        user.profile.save()
    except Exception as e:
        print("profile tier skip:", e)

# wipe prior demo-owned data so re-runs are clean
for M in (SubAgent, KnowledgeBase, Document, ChatSession, Skill):
    M.objects.filter(user=user).delete()

now = timezone.now()

# Fast NVIDIA NIM model (server key fallback, no per-user credential needed).
LLM_MODEL = "microsoft/phi-4-mini-instruct"

# ---- agents ---------------------------------------------------------------
# Seeded as `SubAgent` rows: a prompt, a model and the capabilities each one is
# granted. The demo used to seed node graphs; there is no canvas to draw one on
# and no runtime to execute one.
AGENTS = [
    ("Invoice chaser", "active",
     "Reads incoming invoices, extracts vendor, date and total, and chases anything overdue.",
     ["finance", "email"], "receipt", "#6366f1",
     {"webSearch": False, "scrape": False, "rag": True, "codeExecution": True},
     {"autonomy": "ask", "spendCapRupees": 500, "egress": "none"}),
    ("Market researcher", "active",
     "Researches a topic across several angles, reads the pages, and reports with sources.",
     ["research"], "search", "#0ea5e9",
     {"webSearch": True, "scrape": True, "rag": False, "codeExecution": False},
     {"autonomy": "full", "spendCapRupees": 500, "egress": "none"}),
    ("Docs librarian", "draft",
     "Answers questions from the product documentation knowledge base, with citations.",
     ["docs", "support"], "book", "#22c55e",
     {"webSearch": False, "scrape": False, "rag": True, "codeExecution": False},
     {"autonomy": "ask", "spendCapRupees": 200, "egress": "none"}),
]

agent_objs = []
for name, status, desc, tags, icon, color, grants, guards in AGENTS:
    agent_objs.append(SubAgent.objects.create(
        user=user, name=name, description=desc, prompt=desc, status=status,
        tags=tags, icon=icon, color=color,
        llm_provider="nvidia", llm_model=LLM_MODEL,
        tool_grants=grants, guardrails=guards,
        agent_context={}, sandbox={}, output_schema={}, fanout={},
    ))
print(f"agents: {len(agent_objs)}")

# ---- knowledge bases + documents -----------------------------------------
kb1 = KnowledgeBase.objects.create(user=user, name="Product Documentation",
                                   description="User guides, API references and release notes.", is_default=True)
kb2 = KnowledgeBase.objects.create(user=user, name="Sales Playbook",
                                   description="Objection handling, pricing and competitor notes.")
DOCS = [
    (kb1, "getting-started.md", "md",
     "# Getting started\n\nCreate a workflow, drop in a trigger, connect the nodes, hit run. That's it.\n"),
    (kb1, "api-notes.md", "md",
     "# API notes\n\nAuth is a bearer JWT from /api/auth/login/. Token expires in 60 min, use the refresh endpoint.\n"),
    (kb1, "release-notes-2.4.pdf", "pdf",
     "v2.4\n- new orchestrator engine\n- indexing is faster now\n- MCP servers supported\n"),
    (kb2, "objection-handling.md", "md",
     "# Objections\n\n\"Too expensive\": pivot to hours saved per month, not sticker price.\n\"We already have a tool\": ask what it can't do.\n"),
]
from inference.engine import get_hnsw_kb
from asgiref.sync import async_to_sync

doc_count = {kb1.id: 0, kb2.id: 0}
for kb, name, ftype, body in DOCS:
    content = body.encode()
    d = Document.objects.create(
        user=user, knowledge_base=kb, name=name, file_type=ftype,
        file_size=len(content), status="indexed", content_text=body,
    )
    d.file.save(name, ContentFile(content), save=True)

    # Index the text into the KB's vector store so RAG / search actually works.
    hnsw = get_hnsw_kb(kb.id, kb.s3_index_key or f'indices/kb_{kb.id}')

    async def _index(h=hnsw, doc=d, txt=body, k=kb):
        await h.initialize()
        return await h.add_document(
            doc.id, txt, {'name': doc.name, 'user_id': user.id, 'kb_id': k.id})

    chunks = async_to_sync(_index)()
    d.chunk_count = len(chunks)
    d.save(update_fields=['chunk_count'])
    doc_count[kb.id] += 1

for kb in (kb1, kb2):
    hnsw = get_hnsw_kb(kb.id, kb.s3_index_key or f'indices/kb_{kb.id}')
    kb.doc_count = doc_count[kb.id]
    kb.vector_count = hnsw.ntotal
    kb.save()
print(f"knowledge_bases: 2  documents: {len(DOCS)} (indexed)")

# ---- chat sessions --------------------------------------------------------
CHATS = [
    ("slack workflow", [
        ("user", "how do i post to slack when a form comes in"),
        ("assistant", "Webhook trigger on the form, then a Slack node pointed at your channel. "
                      "If you want the lead enriched first, stick an HTTP node in between. "
                      "The 'Lead enrichment' workflow already does this if you want to copy it."),
        ("user", "can it also enrich the company"),
        ("assistant", "Yeah, that's the HTTP node. Point it at Clearbit with the email and map the "
                      "response fields into the Slack message."),
    ]),
    ("q3 numbers", [
        ("user", "what were the q3 takeaways"),
        ("assistant", "Revenue was up on mid-market, win rate a bit better than Q2. Sales cycle came "
                      "down slightly. Main thing to watch is enterprise pipeline, coverage is thin going into Q4."),
    ]),
    ("onboarding email for alex", [
        ("user", "draft an onboarding email for a new customer, name is Alex"),
        ("assistant", "Subject: Getting you set up\n\nHi Alex,\n\nThanks for signing up. Quickest way to "
                      "get value: build a workflow from one of the templates, connect a credential, and "
                      "upload a doc or two to your knowledge base.\n\nAnything's unclear, just reply here.\n\nBest"),
        ("user", "make it a little warmer"),
        ("assistant", "Swapped the opener to \"Really glad to have you\" and softened the close. Want me to add a line about the demo call?"),
    ]),
]
for i, (title, msgs) in enumerate(CHATS):
    sess = ChatSession.objects.create(user=user, title=title)
    ChatSession.objects.filter(pk=sess.pk).update(
        created_at=now - timedelta(days=i + 1), updated_at=now - timedelta(days=i + 1))
    for j, (role, content) in enumerate(msgs):
        m = ChatMessage.objects.create(session=sess, role=role, content=content)
        ChatMessage.objects.filter(pk=m.pk).update(created_at=now - timedelta(days=i + 1, minutes=-j))
print(f"chat_sessions: {len(CHATS)}")

# ---- skills ---------------------------------------------------------------
SKILLS = [
    ("Meeting notes -> summary", "productivity",
     "Takes my raw notes and gives back a short summary and the action items with who owns them."),
    ("Pull action items", "productivity",
     "Reads a thread or doc and spits out just the todos as a checklist. Ignore everything else."),
]
for title, cat, content in SKILLS:
    Skill.objects.create(user=user, title=title, description=content[:120],
                         content=content, category=cat, is_shared=True)
print(f"skills: {len(SKILLS)}")

print("\nDEMO USER READY")
print(f"  email:    {EMAIL}")
print(f"  password: {PASSWORD}")
