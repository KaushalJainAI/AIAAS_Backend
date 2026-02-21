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
