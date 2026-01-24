import os
import django
import sys

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from credentials.models import CredentialType

def populate_types():
    types = [
        {
            'name': 'OpenAI',
            'slug': 'openai',
            'description': 'OpenAI API for GPT models',
            'icon': 'Cloud',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'apiKey', 'label': 'API Key', 'type': 'password', 'required': True},
                {'name': 'baseUrl', 'label': 'Base URL', 'type': 'text', 'required': False, 'placeholder': 'https://api.openai.com/v1'}
            ]
        },
        {
            'name': 'PostgreSQL',
            'slug': 'postgres',
            'description': 'PostgreSQL Database Connection',
            'icon': 'Database',
            'auth_method': 'custom',
            'fields_schema': [
                {'name': 'host', 'label': 'Host', 'type': 'text', 'required': True, 'default': 'localhost'},
                {'name': 'port', 'label': 'Port', 'type': 'text', 'required': True, 'default': '5432'},
                {'name': 'database', 'label': 'Database Name', 'type': 'text', 'required': True},
                {'name': 'username', 'label': 'Username', 'type': 'text', 'required': True},
                {'name': 'password', 'label': 'Password', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Google OAuth2',
            'slug': 'google-oauth2',
            'description': 'Google Cloud Platform OAuth2',
            'icon': 'Mail',
            'auth_method': 'oauth2',
            'fields_schema': [] # Managed via OAuth flow
        },
        {
            'name': 'Slack Token',
            'slug': 'slack',
            'description': 'Slack Bot Token',
            'icon': 'MessageSquare',
            'auth_method': 'bearer',
            'fields_schema': [
                {'name': 'token', 'label': 'Bot User OAuth Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Website Login',
            'slug': 'website-login',
            'description': 'Login credentials for website automation',
            'icon': 'Globe',
            'auth_method': 'custom',
            'fields_schema': [
                {'name': 'loginUrl', 'label': 'Login Page URL', 'type': 'text', 'required': True, 'placeholder': 'https://example.com/login', 'public': True},
                {'name': 'username', 'label': 'Username or Email', 'type': 'text', 'required': True, 'public': True},
                {'name': 'password', 'label': 'Password', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'HTTP Bearer',
            'slug': 'http-bearer',
            'description': 'Standard HTTP Bearer Token Authentication',
            'icon': 'Shield',
            'auth_method': 'bearer',
            'fields_schema': [
                {'name': 'token', 'label': 'Bearer Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Gemini API',
            'slug': 'gemini-api',
            'description': 'Google Gemini API Key',
            'icon': 'Sparkles',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'api_key', 'label': 'API Key', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Perplexity API',
            'slug': 'perplexity-api',
            'description': 'Perplexity AI API Key',
            'icon': 'Search',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'api_key', 'label': 'API Key', 'type': 'password', 'required': True}
            ]
        }
    ]

    for t in types:
        ct, created = CredentialType.objects.update_or_create(
            slug=t['slug'],
            defaults=t
        )
        print(f"{'Created' if created else 'Updated'} credential type: {t['name']}")

if __name__ == '__main__':
    populate_types()
