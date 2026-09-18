"""
Deep research — GAIA-style questions: several lookups chained together, often
with a calculation at the end, and exactly one checkable answer. Chosen for
facts that do not drift (standards, historical releases, surveyed heights), so a
failure means the chain broke, not that the world changed.

The agent must search (`tool_used: web_search`) and end with an `ANSWER:` line,
which is what the answer regex anchors on so a correct figure buried in an
unrelated sentence does not pass.
"""

SUITE = {
    'slug': 'work-research',
    'group': 'capability',
    'name': 'Work: Deep research',
    'agent': 'deep_researcher',
    'repeats': 3,
    'proves': 'The agent chains several lookups and a calculation into one correct, sourced answer instead of stopping at the first search result.',
    'pass_threshold': 0.6,
    'cases': [
        {
            'name': 'Standard and its successor',
            'goal': ('The IETF RFC that first standardised HTTP/2 was later obsoleted. '
                     'What is the number of the RFC that obsoleted it?'),
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'ANSWER:[^\n]*\b9113\b'},
            ],
            'tags': ['multi-hop', 'standards'],
        },
        {
            'name': 'Creator and prior language',
            'goal': ('The creator of the Python programming language previously worked on another '
                     'programming language at the same research institute. What was that language called?'),
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'ANSWER:[^\n]*\bABC\b'},
            ],
            'tags': ['multi-hop', 'history'],
        },
        {
            'name': 'Two heights and a difference',
            'goal': ('How many metres taller is Mount Everest (using the height announced jointly by '
                     'Nepal and China in 2020) than K2 (8,611 m)? Give the difference to two decimal places.'),
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'ANSWER:[^\n]*\b237\.86\b'},
            ],
            'tags': ['multi-hop', 'calculation'],
        },
        {
            'name': 'Release-year arithmetic',
            'goal': ('How many years after the first public release of the Linux kernel was Git first '
                     'released? Both were created by the same person; use the release years.'),
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'ANSWER:[^\n]*\b14\b'},
            ],
            'tags': ['multi-hop', 'calculation'],
        },
        {
            'name': 'Framework gap',
            'goal': ('In what year was the Django web framework first publicly released, in what year was '
                     'Flask first released, and how many years apart are they? '
                     'Answer as "ANSWER: <django year>, <flask year>, <gap>".'),
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'ANSWER:[^\n]*2005[^\n]*2010[^\n]*\b5\b'},
            ],
            'tags': ['multi-hop', 'calculation'],
        },
    ],
}

IDEAL_OUTPUTS: dict = {}
