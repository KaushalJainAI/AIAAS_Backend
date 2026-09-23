"""
Orchestrator-driven agent creation wizard (EVAL_EXPANSION_PLAN §7).

Replaces the Agent Builder's left chat pane. Two read-only endpoints — no
rows are written here, so there is no second write path:

* `wizard_questions` — description in, questions out. Reads `UserMemory` for
  personalisation hints (timezone, currency, working hours) and the
  capabilities endpoint for what can actually run.
* `wizard_propose` — description + answers in, proposed AgentConfig out with
  plain-words explanations and warnings. Creation itself goes through the
  ordinary POST agents endpoint (with its approval UI), so grants, scopes and
  unattended still pass the same serializer and the same consent step.

Ambiguity rule shared with evals: if the answer changes the plan, ask — the
wizard models it so the agent copies it at runtime (`asked_when_ambiguous`).
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


def _memory_facts(user) -> list[str]:
    try:
        from core.memory import render_for_prompt
        block = render_for_prompt(user) or ''
    except Exception:  # noqa: BLE001
        return []
    facts = [line.strip('- ').strip() for line in block.splitlines() if line.strip()]
    return [f for f in facts if f][:10]


def _question(id_: str, text: str, *, options: list[str] | None = None,
              why: str = '', multi: bool = False) -> dict:
    q: dict = {'id': id_, 'text': text, 'why': why, 'multi': multi}
    if options:
        q['options'] = options
    return q


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def wizard_questions(request):
    """Questions to ask before creating an agent. Read-only."""
    description = str((request.data or {}).get('description', '')).strip()
    if not description:
        return Response({'error': 'Describe what the agent should do.'}, status=400)

    low = description.lower()
    questions: list[dict] = []
    if any(w in low for w in ('mail', 'gmail', 'inbox', 'email')):
        questions.append(_question(
            'mailbox', 'Which mailbox should it work in?',
            why='A mailbox is a connection in your account — the agent cannot guess which one.',
            options=['Gmail (choose account on next screen)']))
    if any(w in low for w in ('document', 'knowledge', 'kb', 'corpus', 'policy', 'handbook')):
        questions.append(_question(
            'corpus', 'Which documents should it answer from?',
            why='A scoped agent reads one corpus, so an answer from the wrong one cannot look right.'))
    questions.extend([
        _question('outputs', 'What should it hand back?',
                  options=['Chat answer', 'Files (workbook / deck / doc)', 'Drafts for me to approve'],
                  why='The output shape decides the contract the eval checks.'),
        _question('autonomy', 'How much may it act on its own?',
                  options=['Ask me before anything outward (send/publish/commit)',
                           'Act, ask only before irreversible steps',
                           'Run fully unattended on a schedule'],
                  why='Sending, publishing and pushing code pause for approval unless you say otherwise.'),
        _question('schedule', 'Should it run on its own, or only when you ask?',
                  options=['Only when I run it', 'Every morning', 'Every Monday', 'Custom cron'],
                  why='A schedule needs unattended permission and a timezone.'),
        _question('spend', 'Monthly spend cap in rupees?',
                  options=['100', '300', '500', '1000'],
                  why='A cap is blast-radius control; evals never count against it.'),
    ])
    facts = _memory_facts(request.user)
    hints = []
    if facts:
        hints.append('I already know: ' + '; '.join(facts[:4]))
    return Response({'questions': questions, 'memory_hints': hints})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def wizard_propose(request):
    """Proposed AgentConfig for a description + answers. Read-only."""
    from agents.agent.runtime import AUTONOMY_LADDER, GRANT_TOOLS

    description = str((request.data or {}).get('description', '')).strip()
    answers = dict((request.data or {}).get('answers') or {})
    if not description:
        return Response({'error': 'Describe what the agent should do.'}, status=400)

    low = description.lower()
    tools: dict[str, bool] = {}
    explanations: list[str] = []

    def grant(key: str, reason: str):
        if key in GRANT_TOOLS:
            tools[key] = True
            explanations.append(f'{key}: {reason}')

    if any(w in low for w in ('research', 'search', 'competitor', 'lead')):
        grant('webSearch', 'needs the public web')
        grant('scrape', 'must open pages, not just snippets')
    if any(w in low for w in ('spreadsheet', 'csv', 'data', 'reconcil', 'total', 'analy')):
        grant('codeExecution', 'every number is computed with code, never guessed')
        grant('fileOps', 'reads inputs and writes outputs in its own folder')
    if any(w in low for w in ('deck', 'slide', 'presentation', 'workbook', 'excel', 'word', 'report', 'memo')):
        grant('office', 'builds real .pptx/.xlsx/.docx files')
        grant('fileOps', 'saves them where you can open them')
    if any(w in low for w in ('mail', 'gmail', 'inbox', 'calendar', 'drive')):
        grant('mcp', 'reaches the mailbox/calendar/drive connection you pick')
    if any(w in low for w in ('database', 'sql', 'postgres')):
        grant('data', 'lists connections and describes schemas before querying')
    if any(w in low for w in ('api ', 'webhook', 'http')):
        grant('api', 'calls your HTTP APIs; writes pause for approval')
    if any(w in low for w in ('code', 'repo', 'pull request', 'test')):
        grant('shell', 'works in your connected code workspace')
    if any(w in low for w in ('sign', 'signature')):
        grant('esign', 'sends for signature only after you approve exact file+signers')
    if any(w in low for w in ('image', 'illustration', 'cover')):
        grant('media', 'every image is billed — each one pauses for approval')
    if not tools:
        grant('webSearch', 'default: answer from the web rather than memory')
        grant('fileOps', 'default: saves its work where you can open it')

    autonomy = 'ask'
    auto_answer = str(answers.get('autonomy', '')).lower()
    if 'unattended' in auto_answer or 'schedule' in auto_answer or 'fully' in auto_answer:
        autonomy = 'auto' if 'irreversible' in auto_answer else 'full'
        if str(answers.get('schedule', '')).lower().startswith('only'):
            autonomy = 'ask'
    if autonomy not in AUTONOMY_LADDER:
        autonomy = 'ask'

    outputs = str(answers.get('outputs', '')).lower()
    contract = ''
    if 'file' in outputs or 'workbook' in outputs or 'deck' in outputs or 'doc' in outputs:
        contract = 'files'

    warnings = []
    if tools.get('mcp'):
        warnings.append('You will pick the exact mailbox/calendar on the next screen — never a shared id.')
    if autonomy == 'full':
        warnings.append('Full autonomy never pauses. Prefer ask/auto unless this runs unattended.')
    if str(answers.get('schedule', '')).lower() not in ('', 'only when i run it') and not answers.get('allowUnattended'):
        warnings.append('A schedule needs Allow unattended on — the create screen will ask for it.')

    return Response({
        'config': {
            'name': str(answers.get('name') or description[:60]),
            'brief': description,
            'tools': tools,
            'autonomy': autonomy,
            'fileAccess': 'read_all_write_own' if tools.get('fileOps') else 'none',
            'outputContract': contract,
            'spendCapRupees': int(str(answers.get('spend') or '300').strip() or 300),
        },
        'explanations': explanations,
        'warnings': warnings,
        'eval_suggestion': {
            'message': 'After install, clone the starter eval for this agent and run it for a 0–100 scorecard.',
        },
    })
