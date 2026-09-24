"""Email belongs to the digest alone (GAP_CLOSURE_PLAN G1).

Every `create_notification(` call outside the digest
(`notifications/reminders.py`) and user-scheduled reminders
(`notifications/scheduled.py`, explicit per-reminder consent) must pass
`send_email=False`, or the day SMTP is configured users get one email per
tool approval / review / system event. This test scans the source tree with
`ast` and fails on any call site that can email.
"""
import ast
from pathlib import Path
from unittest import TestCase

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Directories that are not our code (notably `venv/`, which holds all of
# site-packages — scanning it turns this test into a very slow hang).
SKIP_DIRS = frozenset({
    'venv', '.venv', '.git', '__pycache__', '.pytest_cache',
    'node_modules', '.aegis_patches',
})

# Files allowed to send email: the digest (meant to reach the inbox) and
# user-scheduled reminders (explicit opt-in per reminder).
ALLOWED = {
    'notifications/reminders.py',
    'notifications/scheduled.py',
    'notifications/utils.py',  # the definition itself + its gated send
}


def _prune(root: Path):
    """Yield *.py files under root, not descending into SKIP_DIRS."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIRS or entry.name.endswith('.egg-info'):
                    continue
                stack.append(entry)
            elif entry.suffix == '.py':
                yield entry


def _call_can_email(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == 'send_email' and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return True


class EmailPolicyTests(TestCase):
    def test_only_digest_and_scheduled_reminders_can_email(self):
        offenders = []
        for path in sorted(_prune(BACKEND_ROOT)):
            rel = path.relative_to(BACKEND_ROOT).as_posix()
            if str(rel) in ALLOWED:
                continue
            parts = Path(rel).parts
            if 'tests' in parts or 'migrations' in parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_create = (
                    (isinstance(func, ast.Name) and func.id == 'create_notification')
                    or (isinstance(func, ast.Attribute) and func.attr == 'create_notification')
                )
                if is_create and _call_can_email(node):
                    offenders.append(f'{rel}:{node.lineno}')
        self.assertEqual(
            offenders, [],
            'create_notification() call sites that can send email outside '
            'reminders.py/scheduled.py (email belongs to the digest alone): '
            + ', '.join(offenders),
        )
