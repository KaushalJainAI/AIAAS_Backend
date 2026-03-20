import os
import django
import sys

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from django.contrib.auth.models import User
from skills.models import Skill

def populate_skills():
    # Get or create a default user
    admin_user = User.objects.filter(is_superuser=True).first()
    if not admin_user:
        admin_user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        print("Created superuser 'admin' with password 'admin123'")

    skills_data = [
        {
            'title': 'Email Summary Generator',
            'description': 'Summarizes long email threads into concise bullet points.',
            'content': '# Email Summary Generator\n\nThis skill helps you to summarize long email threads.\n\n## Instructions\n1. Input the email thread.\n2. Get a summary.',
            'author_name': 'Me',
            'is_shared': True,
            'category': 'Productivity'
        },
        {
            'title': 'Python Data Expert',
            'description': 'Expertise in pandas, numpy, and matplotlib for data processing.',
            'content': 'You are a Python Data Expert. Always prefer using vectorization (numpy) over loops.',
            'author_name': 'Me',
            'is_shared': True,
            'category': 'Data Science'
        },
        {
            'title': 'Web Scraping Specialist',
            'description': 'Advanced scraping using BeautifulSoup and Playwright.',
            'content': 'You are a Web Scraping specialist. Always check robots.txt and use appropriate delays.',
            'author_name': 'Me',
            'is_shared': False,
            'category': 'Automation'
        },
        {
            'title': 'Market Trend Analyst',
            'description': 'Analyzing financial markets and predicting trends.',
            'content': 'You are a Financial Analyst. Use RSI, MACD, and Moving Averages to analyze time-series data.',
            'author_name': 'Me',
            'is_shared': True,
            'category': 'Finance'
        },
        {
            'title': 'Cybersecurity Auditor',
            'description': 'Identifying vulnerabilities and suggesting security best practices.',
            'content': 'You are a Security Auditor. Focus on OWASP Top 10.',
            'author_name': 'Security Team',
            'is_shared': True,
            'category': 'Security'
        },
        {
            'title': 'Python Runtime Developer',
            'description': 'How to write valid Python code for the AIAAS platform execution sandbox.',
            'content': """# Python Execution Runtime Guidelines

You are writing code to be executed in a secure Python sandbox within the AIAAS platform (specifically for the Code Node).

## 1. Entry Point
Always structure your code around a `main` function. This is the primary entry point.

```python
def main(item, context):
    # Your logic here
    # 'item' is the input data dict
    # 'context' contains metadata
    return {"status": "success", "processed_data": item}
```

## 2. Arguments
- `item` (dict): The input data object from the preceding node.
- `context` (dict): Contains `workflow_id`, `execution_id`, and `node_id`.

## 3. Return Value
**Critical**: You MUST return a Python dictionary (e.g., `{"key": "value"}`). Non-dict returns will cause errors.

## 4. Sandbox Utilities
- `extract_code(text)`: Strips markdown fences (e.g., ```python) from a string.
- `getattr(obj, name[, default])`: Standard getattr.
- `hasattr(obj, name)`: Standard hasattr.

## 5. Restrictions
- Standard library is available. Do not assume third-party libs are pre-installed.
- No direct disk I/O or unauthorized network calls.
""",
            'author_name': 'AIAAS Platform',
            'is_shared': True,
            'category': 'Developer Tools'
        },
        {
            'title': 'Deep Research Specialist',
            'description': 'Advanced research agent capable of deep-diving into complex topics, cross-referencing information, and synthesizing comprehensive reports.',
            'content': 'You are a Deep Research Specialist. Your goal is to conduct exhaustive research on a given topic. You should plan your research, break down the core queries, utilize available web search tools (such as Tavily or Perplexity) to gather information from multiple sources, cross-reference data for accuracy, and synthesize your findings into a comprehensive, well-structured, and fully cited markdown report. Ensure depth, objectivity, and clarity in your reporting. Restrict the final output to 350 words.',
            'author_name': 'Me',
            'is_shared': True,
            'category': 'Research'
        }
    ]

    for data in skills_data:
        skill, created = Skill.objects.get_or_create(
            title=data['title'],
            user=admin_user,
            defaults=data
        )
        if created:
            print(f"Created skill: {skill.title}")
        else:
            # Update existing
            for key, value in data.items():
                setattr(skill, key, value)
            skill.save()
            print(f"Updated skill: {skill.title}")

if __name__ == '__main__':
    populate_skills()
