"""
Web research — facts chosen because they do not drift, so a failure means the
agent searched or read badly, not that the world changed.
"""

SUITE = {
    'slug': 'research',
    'group': 'capability',
    'name': 'Benchmark: Web research',
    'agent': 'researcher',
    'proves': 'The agent actually searches, answers from sources it cites, and pushes back on false premises.',
    'pass_threshold': 0.7,
    'cases': [
        {
            'name': 'Sourced fact',
            'goal': 'When was the Python programming language first released, and who created it? Cite your source.',
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'contains', 'value': '1991'},
                {'type': 'contains', 'value': 'van rossum'},
                {'type': 'regex', 'pattern': r'https?://'},
            ],
            'tags': ['fact', 'citation'],
        },
        {
            'name': 'Precise number',
            'goal': 'What is the official height of Mount Everest announced jointly by Nepal and China in 2020? Cite your source.',
            'graders': [
                {'type': 'tool_used', 'tool': 'web_search'},
                {'type': 'regex', 'pattern': r'8,?848\.86'},
                {'type': 'regex', 'pattern': r'https?://'},
            ],
            'tags': ['fact', 'number'],
        },
        {
            'name': 'Standards lookup',
            'goal': 'What does IETF RFC 9110 specify? One sentence, with a link.',
            'graders': [
                # Any correct wording: "HTTP Semantics", "the semantics of HTTP", ...
                {'type': 'regex', 'pattern': r'semantics'},
                {'type': 'regex', 'pattern': r'http'},
                {'type': 'regex', 'pattern': r'https?://'},
            ],
            'tags': ['fact'],
        },
        {
            'name': 'False premise',
            'goal': 'Summarise who won the 2019 Nobel Prize in Mathematics and for what.',
            'reference': (
                'There is no Nobel Prize in Mathematics. A good answer says so clearly, '
                'and may mention the Fields Medal or Abel Prize as the closest equivalents. '
                'It must not name a fictional Nobel mathematics laureate.'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r"no nobel|not a nobel|isn't a nobel|does not exist|doesn't exist|there is no"},
                {'type': 'llm_judge'},
            ],
            'tags': ['honesty', 'false-premise'],
        },
        {
            'name': 'Comparative synthesis',
            'goal': (
                'For a small web app, compare SQLite and PostgreSQL on handling many '
                'concurrent writes. Give a recommendation and cite at least two sources.'
            ),
            'reference': (
                'Explains that SQLite allows one writer at a time (database-level write '
                'lock, WAL helps readers not writers) while PostgreSQL handles concurrent '
                'writers via MVCC and row-level locks; recommends PostgreSQL when writes are '
                'concurrent; cites at least two distinct URLs.'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'(?s)https?://.+https?://'},
                {'type': 'llm_judge'},
            ],
            'tags': ['synthesis'],
        },
    ],
}
