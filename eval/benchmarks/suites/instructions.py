"""
Instruction following — the baseline. No tools, so a failure here is the model
or the prompt assembly, never a tool. Cheapest suite; run it first.
"""

SUITE = {
    'slug': 'instructions',
    'group': 'capability',
    'name': 'Benchmark: Instruction following',
    'agent': 'assistant',
    'proves': 'The agent obeys output formats that downstream automation depends on, and admits what it cannot know.',
    'pass_threshold': 0.8,
    'cases': [
        {
            'name': 'Strict JSON output',
            'goal': (
                'Extract the person from this sentence and return ONLY a JSON object '
                'with keys "name" (string) and "age" (integer). No prose, no code fences.\n\n'
                'Sentence: "Priya turned 29 last week and moved to Pune."'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'(?s)^\s*\{.*"name"\s*:\s*"Priya".*\}\s*$'},
                {'type': 'regex', 'pattern': r'"age"\s*:\s*29\b'},
                {'type': 'not_contains', 'value': '```'},
            ],
            'tags': ['format', 'json'],
        },
        {
            'name': 'Exactly three bullets',
            'goal': (
                'Give exactly three bullet points (each starting with "- ") on why '
                'code review matters. Output nothing except the three bullets.'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'^\s*(?:- [^\n]+\s*){3}$'},
            ],
            'tags': ['format'],
        },
        {
            'name': 'Length limit',
            'goal': 'In one sentence of at most 25 words, explain what an API is.',
            'graders': [
                {'type': 'max_length', 'value': 220},
                {'type': 'contains', 'value': 'interface'},
            ],
            'tags': ['format', 'length'],
        },
        {
            'name': 'Field extraction to ISO date',
            'goal': (
                'From the email below, reply with exactly two lines: '
                '"invoice: <number>" and "due: <YYYY-MM-DD>".'
            ),
            'input_data': {
                'email': (
                    'Hi Kaushal, please find attached invoice INV-2291 for the September '
                    'retainer. Payment is due on 14 March 2026. Thanks, Meera'
                ),
            },
            'graders': [
                {'type': 'regex', 'pattern': r'invoice:\s*INV-2291'},
                {'type': 'regex', 'pattern': r'due:\s*2026-03-14'},
            ],
            'tags': ['extraction'],
        },
        {
            'name': 'One-word classification',
            'goal': (
                'Classify the sentiment of this review as positive, negative or neutral. '
                'Answer with the single word only.\n\n'
                'Review: "Delivery was two weeks late and support never replied."'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'^\W*negative\W*$'},
            ],
            'tags': ['classification'],
        },
        {
            'name': 'Admits it has no live data',
            'goal': 'What is the price of Bitcoin in US dollars right now?',
            'reference': (
                'The agent has no web access. A good answer says it cannot see live '
                'prices and suggests where to check. It must not state a current price.'
            ),
            'graders': [
                {'type': 'regex', 'pattern': r'\$\s?\d{2,3},\d{3}', 'negate': True},
                {'type': 'llm_judge'},
            ],
            'tags': ['honesty'],
        },
    ],
}
