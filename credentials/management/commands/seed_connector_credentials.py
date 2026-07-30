"""
Seed the credential types that connector nodes reference.

Why this exists as a command rather than another entry in populate_credentials.py:
a connector's `credential_type="foo"` is a foreign key in all but name. If no
CredentialType with that slug exists, the credential picker on the node renders
an empty dropdown and the connector simply cannot be configured — with no error
anywhere to explain why. Seven connectors were already in that state before this
was written (aws, serpapi, wolfram_alpha, openweathermap, bing_search, and two
slug typos), which is exactly the failure mode this is meant to stop recurring.

`test_every_connector_credential_type_is_seeded` in nodes/tests_connectors.py
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
]


# Types this command does NOT own but connectors depend on. Google Drive and
# Google Calendar both authenticate with the shared google-oauth2 credential,
# which populate_credentials.py defines complete with its oauth_config.
#
# These are created only when absent, never updated: an update_or_create here
# would overwrite that richer definition — silently dropping the OAuth client
# configuration and breaking the consent flow for every Google node — purely as
# a side effect of seeding connectors.
EXTERNALLY_OWNED: list[dict] = [
    {
        "name": "Google OAuth2", "slug": "google-oauth2", "auth_method": "oauth2",
        "description": "Google OAuth for Gmail, Drive, Sheets and Calendar",
        "icon": "Cloud",
        "fields_schema": [
            _field("access_token", "Access Token", secret=True),
            _field("refresh_token", "Refresh Token", secret=True, required=False),
        ],
    },
]


class Command(BaseCommand):
    help = "Create or update the CredentialTypes that connector nodes reference."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Report what is missing without writing anything.",
        )

    def handle(self, *args, **options):
        check_only = options["check"]
        existing = set(CredentialType.objects.values_list("slug", flat=True))

        missing = [c for c in CREDENTIAL_TYPES + EXTERNALLY_OWNED
                   if c["slug"] not in existing]

        if check_only:
            if missing:
                self.stdout.write(self.style.WARNING(
                    f"{len(missing)} credential type(s) missing: "
                    + ", ".join(c["slug"] for c in missing)
                ))
            else:
                self.stdout.write(self.style.SUCCESS("All connector credential types present."))
            return

        created = updated = 0
        with transaction.atomic():
            for spec in CREDENTIAL_TYPES:
                # service_identifier is unique and nullable; leaving it unset
                # avoids colliding with the types populate_credentials.py owns.
                _obj, was_created = CredentialType.objects.update_or_create(
                    slug=spec["slug"],
                    defaults={
                        "name": spec["name"],
                        "description": spec.get("description", ""),
                        "icon": spec.get("icon", ""),
                        "auth_method": spec.get("auth_method", "api_key"),
                        "fields_schema": spec["fields_schema"],
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            # Create-if-absent only — see EXTERNALLY_OWNED.
            for spec in EXTERNALLY_OWNED:
                _obj, was_created = CredentialType.objects.get_or_create(
                    slug=spec["slug"],
                    defaults={
                        "name": spec["name"],
                        "description": spec.get("description", ""),
                        "icon": spec.get("icon", ""),
                        "auth_method": spec.get("auth_method", "api_key"),
                        "fields_schema": spec["fields_schema"],
                    },
                )
                if was_created:
                    created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Connector credential types: {created} created, {updated} updated "
            f"({len(CREDENTIAL_TYPES)} owned, {len(EXTERNALLY_OWNED)} shared)."
        ))
