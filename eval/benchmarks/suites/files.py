"""
File work — the virtual filesystem (`inference/vfs.py`) end to end: write, read
back, edit in place, search. Each case uses a unique token so a pass cannot come
from a file an earlier run left behind.
"""

SUITE = {
    'slug': 'files',
    'group': 'capability',
    'name': 'Benchmark: File operations',
    'agent': 'clerk',
    'proves': 'The agent can create, verify, edit and find files in its workspace — the basis for any agent that produces deliverables.',
    'pass_threshold': 0.75,
    'cases': [
        {
            'name': 'Write then read back',
            'goal': (
                'Create the file /bench/roundtrip.txt containing exactly: benchmark-ok-7731\n'
                'Then read the file back and tell me exactly what it contains.'
            ),
            'graders': [
                {'type': 'tool_used', 'tool': 'write_file'},
                {'type': 'tool_used', 'tool': 'read_file'},
                {'type': 'contains', 'value': 'benchmark-ok-7731'},
                {'type': 'no_error'},
            ],
            'tags': ['write', 'read', 'smoke'],
        },
        {
            'name': 'Edit in place',
            'goal': (
                'Create /bench/config.txt with two lines: "mode=draft" and "retries=3". '
                'Then use edit_file to change mode=draft to mode=final (do not rewrite the '
                'whole file). Finally read it and report both lines.'
            ),
            'graders': [
                {'type': 'tool_used', 'tool': 'edit_file'},
                {'type': 'contains', 'value': 'mode=final'},
                {'type': 'contains', 'value': 'retries=3'},
            ],
            'tags': ['edit', 'smoke'],
        },
        {
            'name': 'Find by content',
            'goal': (
                'Create three files in /bench/notes/: a.md containing "groceries", b.md '
                'containing "quarterly-target-4412", c.md containing "gym". Then use '
                'find_files to locate which file mentions quarterly-target-4412 and tell me its name.'
            ),
            'graders': [
                {'type': 'tool_used', 'tool': 'find_files'},
                {'type': 'contains', 'value': 'b.md'},
            ],
            'tags': ['search'],
        },
        {
            'name': 'Structured deliverable',
            'goal': (
                'Write a markdown report to /bench/report.md with a "# Weekly summary" '
                'heading and a table with columns Task and Status containing two rows: '
                'Deploy | Done, and Docs | Pending. Then confirm the path.'
            ),
            'graders': [
                {'type': 'tool_used', 'tool': 'write_file'},
                {'type': 'contains', 'value': 'report.md'},
                {'type': 'no_error'},
            ],
            'tags': ['write', 'deliverable'],
        },
    ],
}
