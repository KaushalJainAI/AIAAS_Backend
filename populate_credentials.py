import os
import django
import sys

from credentials.models import CredentialType

def populate_types():
    standard_api_key_schema = [
        {
            "id": "apiKey",
            "name": "apiKey",
            "type": "string",
            "required": True,
            "description": "The API Key for this service",
            "isPassword": True
        }
    ]

    types = [
        # --- EXISTING CREDENTIAL TYPES ---
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
        },
        {
            'name': 'Notion API',
            'slug': 'notion',
            'description': 'Notion Integration API Key',
            'icon': 'Database',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'api_key', 'label': 'Internal Integration Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Airtable API',
            'slug': 'airtable',
            'description': 'Airtable Personal Access Token',
            'icon': 'Database',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'api_key', 'label': 'Access Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Telegram Bot',
            'slug': 'telegram',
            'description': 'Telegram Bot Token',
            'icon': 'MessageSquare',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'bot_token', 'label': 'Bot Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Trello API',
            'slug': 'trello',
            'description': 'Trello API Key & Token',
            'icon': 'trello',
            'auth_method': 'custom',
            'fields_schema': [
                {'name': 'api_key', 'label': 'API Key', 'type': 'password', 'required': True},
                {'name': 'token', 'label': 'Access Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'GitHub Token',
            'slug': 'github',
            'description': 'GitHub Personal Access Token',
            'icon': 'Github',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'token', 'label': 'Personal Access Token', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'Discord Webhook',
            'slug': 'discord_webhook',
            'description': 'Discord Webhook URL',
            'icon': 'MessageSquare',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'webhook_url', 'label': 'Webhook URL', 'type': 'password', 'required': True}
            ]
        },
        {
            'name': 'IMAP Email',
            'slug': 'email',
            'description': 'Email Server (IMAP) Credentials',
            'icon': 'Mail',
            'auth_method': 'custom',
            'fields_schema': [
                {'name': 'host', 'label': 'IMAP Host', 'type': 'text', 'required': True, 'placeholder': 'imap.gmail.com'},
                {'name': 'port', 'label': 'Port', 'type': 'text', 'required': True, 'default': '993'},
                {'name': 'username', 'label': 'Email/Username', 'type': 'text', 'required': True},
                {'name': 'password', 'label': 'Password/App Password', 'type': 'password', 'required': True},
                {'name': 'secure', 'label': 'Use SSL/TLS', 'type': 'boolean', 'required': False, 'default': 'true'}
            ]
        },
        {
            'name': 'Discord Bot Token',
            'slug': 'discord_bot',
            'description': 'Discord Bot Token',
            'icon': 'MessageSquare',
            'auth_method': 'api_key',
            'fields_schema': [
                {'name': 'bot_token', 'label': 'Bot Token', 'type': 'password', 'required': True}
            ]
        },
        # --- NEW AI PROVIDER CREDENTIAL TYPES ---
        {
            'name': 'Anthropic API',
            'slug': 'anthropic',
            'description': 'API Key for Anthropic Claude',
            'icon': '🎭',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'OpenRouter API',
            'slug': 'openrouter',
            'description': 'API Key for OpenRouter.ai',
            'icon': '🛣️',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'Hugging Face API',
            'slug': 'huggingface',
            'description': 'Access Token for Hugging Face Inference API',
            'icon': '🤗',
            'auth_method': 'bearer',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'Mistral API',
            'slug': 'mistral',
            'description': 'API Key for Mistral AI',
            'icon': '🌪️',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'xAI API (Grok)',
            'slug': 'xai',
            'description': 'API Key for xAI',
            'icon': '✖️',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'DeepSeek API',
            'slug': 'deepseek',
            'description': 'API Key for DeepSeek',
            'icon': '🐳',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'Cohere API',
            'slug': 'cohere',
            'description': 'API Key for Cohere',
            'icon': '🪐',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        },
        {
            'name': 'Groq API',
            'slug': 'groq',
            'description': 'API Key for Groq Cloud',
            'icon': '⚡',
            'auth_method': 'api_key',
            'fields_schema': standard_api_key_schema
        }
    ]

    for cred_data in types:
        slug = cred_data['slug']
        defaults = {
            'name': cred_data['name'],
            'description': cred_data.get('description', ''),
            'icon': cred_data.get('icon', ''),
            'auth_method': cred_data.get('auth_method', 'api_key'),
            'fields_schema': cred_data.get('fields_schema', []),
            'is_active': True
        }
        # Only set service_identifier if it would not cause a collision 
        # or if it's already set on the existing object.
        # To be safe, for new records we'll just set it to the slug if it's uniquely identifying.
        # But actually, the DB has unique=True on service_identifier.
        # So we'll only set it if explicitly provided in the dict.
        if 'service_identifier' in cred_data:
            defaults['service_identifier'] = cred_data['service_identifier']

        obj, created = CredentialType.objects.update_or_create(
            slug=slug,
            defaults=defaults
        )
        print(f"{'Created' if created else 'Updated'} credential type: {obj.name} (slug: {slug})")

    print("Credential population complete.")

if __name__ == "__main__":
    populate_types()
