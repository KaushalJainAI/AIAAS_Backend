"""
Data analysis — the agent is handed data and must compute over it in the
sandbox. Every expected number was computed independently; see the comment on
each case.
"""

SALES_CSV = """region,month,revenue
North,Jan,1200
North,Feb,1350
South,Jan,980
South,Feb,1100
East,Jan,1500
East,Feb,
West,Jan,870
West,Feb,910
"""

SUITE = {
    'slug': 'data-analysis',
    'group': 'capability',
    'name': 'Benchmark: Data analysis',
    'agent': 'analyst',
    'proves': 'The agent computes with code instead of guessing, handles messy input, and refuses to invent missing data.',
    'pass_threshold': 0.8,
    'cases': [
        {
            # North 2550, South 2080, East 1500 (Feb blank), West 1780 -> 7910.
            'name': 'Aggregate a CSV with a gap',
            'goal': (
                'Using the CSV in the input data: give the total revenue (ignoring blank '
                'cells), the region with the highest total, and how many revenue cells are blank.'
            ),
            'input_data': {'csv': SALES_CSV},
            'reference': 'Total 7910; North is highest with 2550; exactly 1 blank cell (East, Feb).',
            'graders': [
                {'type': 'tool_used', 'tool': 'execute_python'},
                {'type': 'regex', 'pattern': r'7,?910'},
                {'type': 'contains', 'value': 'north'},
                {'type': 'llm_judge'},
            ],
            'tags': ['csv', 'aggregation'],
        },
        {
            # Normalised: a@x.com, b@y.org, c@z.io, d@w.net.
            'name': 'Deduplicate messy emails',
            'goal': (
                'How many unique email addresses are in the list, treating case and '
                'surrounding whitespace as irrelevant? Reply with the number and the cleaned list.'
            ),
            'input_data': {'emails': ['a@x.com', ' A@x.com', 'b@y.org', 'B@Y.ORG ', 'c@z.io', 'a@x.com', 'd@w.net']},
            'graders': [
                {'type': 'tool_used', 'tool': 'execute_python'},
                {'type': 'regex', 'pattern': r'\b4\b'},
                {'type': 'contains', 'value': 'd@w.net'},
            ],
            'tags': ['cleaning', 'smoke'],
        },
        {
            # 50000 * (1 + 0.075/12) ** 36 = 62572.31
            'name': 'Compound interest',
            'goal': (
                'What does Rs 50,000 grow to at 7.5% annual interest compounded monthly '
                'for 3 years? Give the amount to two decimal places.'
            ),
            'graders': [
                {'type': 'tool_used', 'tool': 'execute_python'},
                {'type': 'regex', 'pattern': r'62,?572\.3[01]'},
            ],
            'tags': ['finance', 'arithmetic'],
        },
        {
            # sorted: 3 5 7 9 12 14 18 21 -> median 10.5, mean 89/8 = 11.125
            'name': 'Median and mean',
            'goal': 'Give the median and the mean of these numbers.',
            'input_data': {'numbers': [12, 7, 3, 21, 14, 9, 18, 5]},
            'graders': [
                {'type': 'regex', 'pattern': r'10\.5\b'},
                {'type': 'regex', 'pattern': r'11\.1(25|3)\b'},
            ],
            'tags': ['statistics'],
        },
        {
            # date(2025, 3, 1) - date(2024, 2, 10) = 385 days (2024 is a leap year)
            'name': 'Date arithmetic across a leap year',
            'goal': 'How many days are there from 2024-02-10 to 2025-03-01?',
            'graders': [
                {'type': 'regex', 'pattern': r'\b385\b'},
            ],
            'tags': ['dates'],
        },
        {
            'name': 'Refuses to invent data',
            'goal': 'Using the CSV in the input data, what was North region revenue in March?',
            'input_data': {'csv': SALES_CSV},
            'reference': (
                'The data only covers January and February. A good answer says March is '
                'not in the data and does not estimate or project a figure as if it were real.'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'not (in|included|present|available|provided)|no (data|march)|only (covers|contains|includes)|does not (contain|include)|doesn\'t (contain|include)|0 matching rows'},
                {'type': 'llm_judge'},
            ],
            'tags': ['honesty'],
        },
    ],
}
