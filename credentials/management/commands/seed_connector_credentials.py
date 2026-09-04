"""
Seed the credential types that connector nodes reference.

Why this exists as a command rather than another entry in populate_credentials.py:
a connector's `credential_type="foo"` is a foreign key in all but name. If no
CredentialType with that slug exists, the credential picker on the node renders
an empty dropdown and the connector simply cannot be configured — with no error
anywhere to explain why. Seven connectors were already in that state before this
was written (aws, serpapi, wolfram_alpha, openweathermap, bing_search, and two
slug typos), which is exactly the failure mode this is meant to stop recurring.

`test_every_connector_credential_type_is_seeded` in nodes/tests/test_connectors.py
holds the two sides together, so a new connector with an unseeded credential
type fails the suite instead of shipping broken.

Idempotent: run it as often as you like.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from credentials.models import CredentialType


def _api_key(label: str = "API Key", placeholder: str = "") -> list[dict]:
    return [{
        "name": "apiKey", "label": label, "type": "password",
        "required": True, "placeholder": placeholder,
    }]


def _field(name: str, label: str, *, secret: bool = False,
           required: bool = True, placeholder: str = "") -> dict:
    return {
        "name": name, "label": label,
        "type": "password" if secret else "text",
        "required": required, "placeholder": placeholder,
    }


# Every credential type a node references. Grouped to match the connector packs.
CREDENTIAL_TYPES: list[dict] = [
    # ---- Previously referenced by nodes but never seeded -------------------
    # These are the pre-existing breakages: the nodes shipped, the credential
    # types did not, so the nodes could never be configured.
    {
        "name": "AWS", "slug": "aws", "auth_method": "custom",
        "description": "AWS access keys for S3 and SQS",
        "icon": "Cloud",
        "fields_schema": [
            _field("accessKeyId", "Access Key ID"),
            _field("secretAccessKey", "Secret Access Key", secret=True),
            _field("region", "Default Region", required=False, placeholder="ap-south-1"),
            _field("sessionToken", "Session Token", secret=True, required=False),
        ],
    },
    {
        "name": "SerpAPI", "slug": "serpapi", "auth_method": "api_key",
        "description": "SerpAPI search results", "icon": "Search",
        "fields_schema": _api_key(),
    },
    {
        "name": "Wolfram Alpha", "slug": "wolfram_alpha", "auth_method": "api_key",
        "description": "Wolfram Alpha computational knowledge", "icon": "Calculator",
        "fields_schema": _api_key("App ID"),
    },
    {
        "name": "OpenWeatherMap", "slug": "openweathermap", "auth_method": "api_key",
        "description": "OpenWeatherMap forecasts", "icon": "Cloud",
        "fields_schema": _api_key(),
    },
    {
        "name": "Bing Search", "slug": "bing_search", "auth_method": "api_key",
        "description": "Bing Web Search", "icon": "Search",
        "fields_schema": _api_key("Subscription Key"),
    },
    # Two nodes ask for these hyphenated slugs while the seeded types use the
    # bare name. Rather than edit the nodes and risk breaking anyone's existing
    # saved credential, seed the alias too — both now resolve.
    {
        "name": "Hugging Face (API)", "slug": "huggingface-api", "auth_method": "api_key",
        "description": "Hugging Face Inference API", "icon": "Cpu",
        "fields_schema": _api_key("Access Token"),
    },
    {
        "name": "xAI (API)", "slug": "xai-api", "auth_method": "api_key",
        "description": "xAI Grok API", "icon": "Cpu",
        "fields_schema": _api_key(),
    },

    # ---- Messaging ---------------------------------------------------------
    {
        "name": "Microsoft Teams", "slug": "microsoft-teams", "auth_method": "custom",
        "description": "Teams Incoming Webhook", "icon": "MessageSquare",
        "fields_schema": [
            _field("webhookUrl", "Webhook URL", secret=True,
                   placeholder="https://outlook.office.com/webhook/..."),
        ],
    },
    {
        "name": "Twilio", "slug": "twilio", "auth_method": "basic",
        "description": "Twilio SMS and WhatsApp", "icon": "Phone",
        "fields_schema": [
            _field("accountSid", "Account SID"),
            _field("authToken", "Auth Token", secret=True),
            _field("fromNumber", "Default From Number", required=False,
                   placeholder="+15551234567"),
        ],
    },
    {
        "name": "WhatsApp Cloud API", "slug": "whatsapp-cloud", "auth_method": "bearer",
        "description": "Meta WhatsApp Cloud API", "icon": "MessageCircle",
        "fields_schema": [
            _field("accessToken", "Access Token", secret=True),
            _field("phoneNumberId", "Phone Number ID"),
        ],
    },
    {
        "name": "Mattermost", "slug": "mattermost", "auth_method": "bearer",
        "description": "Mattermost server", "icon": "MessageSquare",
        "fields_schema": [
            _field("serverUrl", "Server URL", placeholder="https://chat.example.com"),
            _field("accessToken", "Personal Access Token", secret=True),
        ],
    },

    # ---- CRM ---------------------------------------------------------------
    {
        "name": "HubSpot", "slug": "hubspot", "auth_method": "bearer",
        "description": "HubSpot CRM private app token", "icon": "Users",
        "fields_schema": _api_key("Private App Token"),
    },
    {
        "name": "Pipedrive", "slug": "pipedrive", "auth_method": "api_key",
        "description": "Pipedrive CRM", "icon": "Users",
        "fields_schema": [
            _field("apiToken", "API Token", secret=True),
            _field("companyDomain", "Company Domain", required=False,
                   placeholder="mycompany"),
        ],
    },
    {
        "name": "Salesforce", "slug": "salesforce", "auth_method": "oauth2",
        "description": "Salesforce REST API", "icon": "Cloud",
        "fields_schema": [
            _field("accessToken", "Access Token", secret=True),
            _field("instanceUrl", "Instance URL",
                   placeholder="https://yourorg.my.salesforce.com"),
        ],
    },

    # ---- Ticketing / project management ------------------------------------
    {
        "name": "Jira", "slug": "jira", "auth_method": "basic",
        "description": "Jira Cloud", "icon": "CheckSquare",
        "fields_schema": [
            _field("domain", "Site Domain", placeholder="yourcompany.atlassian.net"),
            _field("email", "Account Email"),
            _field("apiToken", "API Token", secret=True),
        ],
    },
    {
        "name": "Linear", "slug": "linear", "auth_method": "api_key",
        "description": "Linear issue tracking", "icon": "Triangle",
        "fields_schema": _api_key("Personal API Key", "lin_api_..."),
    },
    {
        "name": "Asana", "slug": "asana", "auth_method": "bearer",
        "description": "Asana tasks", "icon": "CheckCircle",
        "fields_schema": _api_key("Personal Access Token"),
    },
    {
        "name": "ClickUp", "slug": "clickup", "auth_method": "api_key",
        "description": "ClickUp tasks", "icon": "CheckSquare",
        "fields_schema": _api_key("API Token", "pk_..."),
    },
    {
        "name": "Todoist", "slug": "todoist", "auth_method": "bearer",
        "description": "Todoist tasks", "icon": "CheckCircle",
        "fields_schema": _api_key("API Token"),
    },

    # ---- Developer tooling -------------------------------------------------
    {
        "name": "GitLab", "slug": "gitlab", "auth_method": "api_key",
        "description": "GitLab issues, merge requests and pipelines", "icon": "GitBranch",
        "fields_schema": [
            _field("apiKey", "Personal Access Token", secret=True, placeholder="glpat-..."),
            _field("baseUrl", "GitLab URL", required=False,
                   placeholder="https://gitlab.com"),
        ],
    },
    {
        "name": "Sentry", "slug": "sentry", "auth_method": "bearer",
        "description": "Sentry error tracking", "icon": "AlertTriangle",
        "fields_schema": _api_key("Auth Token"),
    },
    {
        "name": "PagerDuty", "slug": "pagerduty", "auth_method": "api_key",
        "description": "PagerDuty incidents", "icon": "Bell",
        "fields_schema": [
            _field("apiKey", "REST API Key", secret=True),
            _field("fromEmail", "From Email", required=False,
                   placeholder="you@example.com"),
        ],
    },

    # ---- Storage -----------------------------------------------------------
    {
        "name": "Dropbox", "slug": "dropbox", "auth_method": "bearer",
        "description": "Dropbox files", "icon": "Package",
        "fields_schema": [_field("accessToken", "Access Token", secret=True)],
    },

    # ---- Commerce ----------------------------------------------------------
    {
        "name": "Stripe", "slug": "stripe", "auth_method": "bearer",
        "description": "Stripe payments", "icon": "CreditCard",
        "fields_schema": _api_key("Secret Key", "sk_live_... or sk_test_..."),
    },
    {
        "name": "Shopify", "slug": "shopify", "auth_method": "api_key",
        "description": "Shopify Admin API", "icon": "ShoppingBag",
        "fields_schema": [
            _field("shopDomain", "Shop Domain", placeholder="mystore"),
            _field("accessToken", "Admin API Access Token", secret=True,
                   placeholder="shpat_..."),
        ],
    },

    # ---- Support -----------------------------------------------------------
    {
        "name": "Zendesk", "slug": "zendesk", "auth_method": "basic",
        "description": "Zendesk Support tickets", "icon": "LifeBuoy",
        "fields_schema": [
            _field("subdomain", "Subdomain", placeholder="mycompany"),
            _field("email", "Agent Email"),
            _field("apiToken", "API Token", secret=True),
        ],
    },
    {
        "name": "Intercom", "slug": "intercom", "auth_method": "bearer",
        "description": "Intercom contacts and conversations", "icon": "MessageCircle",
        "fields_schema": [_field("accessToken", "Access Token", secret=True)],
    },

    # ---- Marketing ---------------------------------------------------------
    {
        "name": "SendGrid", "slug": "sendgrid", "auth_method": "bearer",
        "description": "SendGrid transactional email", "icon": "Mail",
        "fields_schema": [
            _field("apiKey", "API Key", secret=True, placeholder="SG...."),
            _field("fromEmail", "Default From Address", required=False),
        ],
    },
    {
        "name": "Mailchimp", "slug": "mailchimp", "auth_method": "api_key",
        "description": "Mailchimp audiences and campaigns", "icon": "Mail",
        "fields_schema": [
            _field("apiKey", "API Key", secret=True, placeholder="....-us21"),
            _field("dc", "Datacentre", required=False,
                   placeholder="us21 (derived from the key if omitted)"),
        ],
    },

    # ---- Chat platforms ----------------------------------------------------
    # These ten arrived with the REST connector pack, which has since been
    # deleted. They are kept because the curated MCP servers reference them:
    # `mcp_integration/tests/test_credential_bridge.py` is what fails if a
    # curated mapping names a credential type that is missing here.
    {
        "name": "Slack", "slug": "slack", "auth_method": "bearer",
        "description": "Slack bot token", "icon": "MessageSquare",
        "fields_schema": [
            _field("token", "Bot User OAuth Token", secret=True,
                   placeholder="xoxb-..."),
            # The Slack MCP server refuses to start without a workspace id:
            # "Please set SLACK_BOT_TOKEN and SLACK_TEAM_ID environment
            # variables". A token on its own could never connect.
            _field("teamId", "Workspace ID", placeholder="T01234567"),
        ],
    },
    {
        "name": "Discord", "slug": "discord", "auth_method": "none",
        "description": "Discord incoming webhook", "icon": "MessageCircle",
        "fields_schema": [
            _field("webhookUrl", "Webhook URL", secret=True,
                   placeholder="https://discord.com/api/webhooks/..."),
        ],
    },
    {
        "name": "Telegram", "slug": "telegram", "auth_method": "api_key",
        "description": "Telegram bot token", "icon": "Send",
        "fields_schema": [
            _field("token", "Bot Token", secret=True,
                   placeholder="123456:ABC-DEF..."),
        ],
    },

    # ---- Google Workspace --------------------------------------------------
    {
        # Shared by GoogleDriveNode and GoogleCalendarNode, which have been
        # registered connectors for a while. It was only ever created by the
        # standalone populate_credentials.py script, so a deployment that ran
        # migrations and this seeder — but not that script — showed an empty
        # credential picker on both nodes.
        "name": "Google (OAuth2)", "slug": "google-oauth2", "auth_method": "oauth2",
        "description": "Google account access for Drive and Calendar", "icon": "Chrome",
        # Both `verification._verify_oauth2` and `manager.refresh_oauth_token`
        # refuse to act on an oauth2 type with no `token_url`, reporting it as a
        # bad credential rather than as missing configuration. Seeded here so a
        # fresh install is correct without depending on migration `0008`.
        "oauth_config": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
            "revoke_url": "https://oauth2.googleapis.com/revoke",
            "access_type": "offline",
            "prompt": "consent",
        },
        "fields_schema": [
            _field("access_token", "Access Token", secret=True),
            _field("refresh_token", "Refresh Token", secret=True, required=False),
        ],
    },
    {
        "name": "Gmail", "slug": "gmail", "auth_method": "oauth2",
        "description": "Gmail send and draft access", "icon": "Mail",
        "fields_schema": [
            _field("access_token", "Access Token", secret=True),
            _field("refresh_token", "Refresh Token", secret=True, required=False),
        ],
    },
    {
        "name": "Google Sheets", "slug": "google-sheets", "auth_method": "oauth2",
        "description": "Google Sheets read and write access", "icon": "Table",
        "fields_schema": [
            _field("access_token", "Access Token", secret=True),
            _field("refresh_token", "Refresh Token", secret=True, required=False),
        ],
    },

    # ---- Productivity ------------------------------------------------------
    {
        "name": "Notion", "slug": "notion", "auth_method": "bearer",
        "description": "Notion internal integration token", "icon": "FileText",
        "fields_schema": [
            _field("token", "Integration Token", secret=True,
                   placeholder="secret_..."),
        ],
    },
    {
        "name": "Airtable", "slug": "airtable", "auth_method": "bearer",
        "description": "Airtable personal access token", "icon": "Grid",
        "fields_schema": _api_key("Personal Access Token", "pat..."),
    },
    {
        "name": "Trello", "slug": "trello", "auth_method": "api_key",
        "description": "Trello API key and token", "icon": "Trello",
        "fields_schema": [
            _field("apiKey", "API Key", secret=True),
            _field("token", "API Token", secret=True),
        ],
    },

    # ---- Developer tooling (continued) -------------------------------------
    {
        "name": "GitHub", "slug": "github", "auth_method": "api_key",
        "description": "GitHub personal access token", "icon": "Github",
        "fields_schema": [
            _field("token", "Personal Access Token", secret=True,
                   placeholder="ghp_..."),
        ],
    },

    # ---- Web scraping ------------------------------------------------------
    {
        "name": "Firecrawl", "slug": "firecrawl", "auth_method": "bearer",
        "description": "Firecrawl scraping and crawling", "icon": "Globe",
        "fields_schema": _api_key("API Key", "fc-..."),
    },

    # ---- LLM providers -----------------------------------------------------
    # Referenced by the LLM nodes (nodes/llm*.py) and by the frontend node
    # configs, but previously unseeded — same empty-dropdown failure as above.
    {
        "name": "OpenAI", "slug": "openai", "auth_method": "api_key",
        "description": "OpenAI API key", "icon": "Brain",
        "fields_schema": _api_key("API Key", "sk-..."),
    },
    {
        "name": "OpenRouter", "slug": "openrouter", "auth_method": "api_key",
        "description": "OpenRouter API key (multi-provider LLM gateway)",
        "icon": "Brain",
        "fields_schema": _api_key("API Key", "sk-or-..."),
    },
    {
        "name": "NVIDIA NIM", "slug": "nvidia", "auth_method": "api_key",
        "description": "NVIDIA NIM / build.nvidia.com API key", "icon": "Brain",
        "fields_schema": _api_key("API Key", "nvapi-..."),
    },

    # ---- Search / research -------------------------------------------------
    {
        "name": "Tavily", "slug": "tavily", "auth_method": "api_key",
        "description": "Tavily search API", "icon": "Search",
        "fields_schema": _api_key("API Key", "tvly-..."),
    },

    # ---- Chat platforms (continued) ----------------------------------------
    # `discord` (bot token) is seeded above; these are the two other shapes the
    # nodes and the frontend node configs ask for by slug.
    {
        "name": "Discord Bot", "slug": "discord_bot", "auth_method": "bearer",
        "description": "Discord bot token", "icon": "MessageSquare",
        "fields_schema": [
            _field("botToken", "Bot Token", secret=True),
        ],
    },
    {
        "name": "Discord Webhook", "slug": "discord_webhook", "auth_method": "custom",
        "description": "Discord incoming webhook URL", "icon": "MessageSquare",
        "fields_schema": [
            _field("webhookUrl", "Webhook URL", secret=True,
                   placeholder="https://discord.com/api/webhooks/..."),
        ],
    },

    # ---- Email -------------------------------------------------------------
    {
        "name": "Email (SMTP)", "slug": "email", "auth_method": "basic",
        "description": "SMTP server credentials for sending mail", "icon": "Mail",
        "fields_schema": [
            _field("host", "SMTP Host", placeholder="smtp.gmail.com"),
            _field("port", "Port", required=False, placeholder="587"),
            _field("username", "Username"),
            _field("password", "Password", secret=True),
            _field("fromEmail", "From Address", required=False),
        ],
    },
]

class Command(BaseCommand):
    help = "Seed the CredentialType rows that connector nodes reference."

    @transaction.atomic
    def handle(self, *args, **options):
        verbosity = options.get("verbosity", 1)
        created_count = 0
        updated_count = 0

        for spec in CREDENTIAL_TYPES:
            defaults = {
                "name": spec["name"],
                # The frontend resolves a node's `credentialType` against this
                # column, so it must mirror the slug or the credential picker
                # on the node cannot narrow the list to the right type.
                "service_identifier": spec.get("service_identifier", spec["slug"]),
                "auth_method": spec.get("auth_method", "api_key"),
                "description": spec.get("description", ""),
                "icon": spec.get("icon", "Key"),
                "fields_schema": spec.get("fields_schema", []),
                "is_active": True,
            }
            # Only written when the spec declares one. Listing it
            # unconditionally would blank the column on every other type each
            # time this command is re-run, which is the failure this seeder
            # exists to prevent rather than cause.
            if spec.get("oauth_config"):
                defaults["oauth_config"] = spec["oauth_config"]
            _obj, created = CredentialType.objects.update_or_create(
                slug=spec["slug"], defaults=defaults,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        if verbosity:
            self.stdout.write(self.style.SUCCESS(
                f"Credential types seeded: {created_count} created, "
                f"{updated_count} updated ({len(CREDENTIAL_TYPES)} total)."
            ))
