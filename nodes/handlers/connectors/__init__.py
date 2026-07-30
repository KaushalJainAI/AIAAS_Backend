"""
Connector node handlers.

Connectors are how an agent reaches the outside world, so they are the part of
the system most exposed to other people's APIs — and the part where a small
inconsistency (a missing timeout, an unhandled 429, an error swallowed into
`str(e)`) turns into a workflow that fails in a way nobody can diagnose.

Everything here subclasses `RestConnectorNode`, which supplies SSRF-checked
requests, Retry-After-aware backoff, one error shape and response redaction. A
connector module should therefore contain endpoints and field definitions and
very little else; if a connector is growing its own HTTP handling, that is a
sign the base is missing something and should grow instead.

Grouped by what they are for, one module per group.
"""
from .messaging import (
    MattermostNode,
    MicrosoftTeamsNode,
    TwilioSMSNode,
    WhatsAppNode,
)
from .crm import (
    HubSpotNode,
    PipedriveNode,
    SalesforceNode,
)
from .ticketing import (
    AsanaNode,
    ClickUpNode,
    JiraNode,
    LinearNode,
    TodoistNode,
)
from .devtools import (
    GitLabNode,
    PagerDutyNode,
    SentryNode,
)
from .storage import (
    DropboxNode,
    GoogleCalendarNode,
    GoogleDriveNode,
    S3Node,
)
from .commerce import (
    ShopifyNode,
    StripeNode,
)
from .support import (
    IntercomNode,
    ZendeskNode,
)
from .marketing import (
    MailchimpNode,
    SendGridNode,
)

#: Every connector in this package, in registration order. The registry imports
#: this rather than naming each class, so adding a connector means editing one
#: list instead of three files and forgetting the third.
ALL_CONNECTORS = [
    # Messaging
    MicrosoftTeamsNode,
    TwilioSMSNode,
    WhatsAppNode,
    MattermostNode,
    # CRM
    HubSpotNode,
    PipedriveNode,
    SalesforceNode,
    # Ticketing / project management
    JiraNode,
    LinearNode,
    AsanaNode,
    ClickUpNode,
    TodoistNode,
    # Developer tooling
    GitLabNode,
    SentryNode,
    PagerDutyNode,
    # Storage / calendar
    GoogleDriveNode,
    GoogleCalendarNode,
    DropboxNode,
    S3Node,
    # Commerce
    StripeNode,
    ShopifyNode,
    # Support
    ZendeskNode,
    IntercomNode,
    # Marketing
    SendGridNode,
    MailchimpNode,
]

__all__ = ["ALL_CONNECTORS"] + [c.__name__ for c in ALL_CONNECTORS]
