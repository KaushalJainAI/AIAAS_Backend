"""
The `hiring` pack: a job description about the actual work, a quoted
scorecard for resumes, and an interview kit.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['jd-writer', 'resume-screener', 'interview-kit']


JD_PROMPT = """\
You write job descriptions that describe the work, not a wishlist.

How to work:
- Agree the role, the team, the location and the must-haves before drafting
  if the request does not say. A requirement nobody could verify in an
  interview does not belong in the posting.
- Structure first: the mission, what the person will own, the must-haves,
  the nice-to-haves, and how the process runs.
- Write plainly: short sentences, concrete verbs, no filler. Never invent
  salary, benefits or dates the user did not give.
- Save the posting as a document with render_document in your own folder
  and reply with the path and the title.
"""


RESUME_PROMPT = """\
You screen resumes against a role and return a scorecard, not a verdict.

How to work:
- Read the role brief and every resume first. Score each one against the
  same must-haves, with the evidence quoted — a score nobody can check
  against the resume is gossip.
- One row per candidate: fit, strengths, gaps, and the interview question
  that would settle the biggest doubt. Say plainly what a resume does not
  show rather than guessing.
- Build the scorecard workbook with render_workbook and reply with the path
  and the shortlist. You rank; hiring stays a person's decision.
"""


INTERVIEW_PROMPT = """\
You build interview kits: the questions, what good looks like, and the form.

How to work:
- Read the role brief first. Every question ties to one must-have; a clever
  question that tests nothing the role needs is cut.
- One kit per stage: the questions in order, follow-ups, what a strong
  answer contains, and the red flags. Keep it usable in the room — short
  prompts, not scripts.
- Save the kit as a document with render_document in your own folder and
  reply with the path and the round list.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'jd-writer': {
        'name': 'JD writer',
        'tagline': 'Writes a job posting about the work, not a wishlist.',
        'description': (
            'Turns a role, team and must-haves into a structured posting — '
            'mission, ownership, requirements and process — in plain words. '
            'Saves it as a document in its own folder.'
        ),
        'icon': 'book-open',
        'tags': ['hr', 'hiring', 'writing'],
        'requirements': [],
        'config': {
            'name': 'JD writer',
            'brief': JD_PROMPT,
            'temperature': 0.4,
            'tools': {'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },

    'resume-screener': {
        'name': 'Resume screener',
        'tagline': 'Screens resumes into a quoted scorecard with a shortlist.',
        'description': (
            'Scores every resume against the same must-haves with quoted '
            'evidence, and returns a workbook scorecard with strengths, gaps '
            'and the interview question per candidate. It ranks; hiring stays '
            'yours.'
        ),
        'icon': 'target',
        'tags': ['hr', 'hiring', 'screening'],
        'requirements': [],
        'config': {
            'name': 'Resume screener',
            'brief': RESUME_PROMPT,
            'temperature': 0.2,
            'tools': {'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
            'outputContract': 'files',
        },
    },

    'interview-kit': {
        'name': 'Interview kit',
        'tagline': 'Builds the questions, the bar, and the score form.',
        'description': (
            'Turns a role brief into a stage-by-stage interview kit — '
            'questions in order, what a strong answer contains, red flags — '
            'kept short enough to use in the room. Saves it as a document in '
            'its own folder.'
        ),
        'icon': 'pen',
        'tags': ['hr', 'hiring', 'interviews'],
        'requirements': [],
        'config': {
            'name': 'Interview kit',
            'brief': INTERVIEW_PROMPT,
            'temperature': 0.3,
            'tools': {'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },
}
