"""
What the platform can message on, as data the Connections page renders.

Each channel names the vault credential that unlocks it, the tools it serves,
what it costs, and the setup steps in order — so a user can see what is
supported and what is missing without asking the model. Status is computed
per caller; the static half lives here, next to the adapters it describes.
"""
from __future__ import annotations

TOOLS = ('message_channels', 'message_search', 'message_read',
         'message_draft', 'message_send')

CHANNELS: dict[str, dict] = {
    'slack': {
        'label': 'Slack',
        'blurb': 'Post to channels and DMs, search history, draft replies.',
        'credential_slug': 'slack',
        'cost': 'Free.',
        'setup': [
            'Create a Slack app at api.slack.com/apps and install it to the workspace.',
            'Store the Bot User OAuth Token (xoxb-…) as a Slack credential.',
            'For searching history, also store a User OAuth Token (xoxp-…) with search:read.',
        ],
        'inbound': 'Point the app\u2019s Event Subscriptions at the webhook URL. '
                   'Needs SLACK_SIGNING_SECRET on the platform.',
    },
    'telegram': {
        'label': 'Telegram',
        'blurb': 'Message any chat that wrote to the bot first, from a BotFather token.',
        'credential_slug': 'telegram',
        'cost': 'Free.',
        'setup': [
            'Talk to @BotFather and create a bot to get its token.',
            'Store the token as a Telegram credential.',
            'Create the account below, then Register webhook so replies arrive.',
        ],
        'inbound': 'Registered from here — no dashboard needed.',
    },
    'whatsapp': {
        'label': 'WhatsApp',
        'blurb': 'Customer messaging on the Cloud API, with templates outside the 24 h window.',
        'credential_slug': 'whatsapp-cloud',
        'cost': 'Paid per message, recorded in the ledger.',
        'setup': [
            'Finish Meta Business verification and create a WhatsApp app.',
            'Store the access token and phone number ID as a WhatsApp Cloud API credential.',
            'Register the webhook URL in the Meta dashboard.',
        ],
        'inbound': 'Register the webhook URL in the Meta dashboard; the handshake is answered automatically.',
    },
    'sms': {
        'label': 'SMS',
        'blurb': 'Text any number through Twilio.',
        'credential_slug': 'twilio',
        'cost': 'Paid per message, recorded in the ledger.',
        'setup': [
            'Add a Twilio account SID, auth token and sender number as a Twilio credential.',
            'Set SMS_ENGINE=twilio on the platform.',
        ],
        'inbound': 'Point the Twilio number\u2019s webhook at the webhook URL.',
    },
    'teams': {
        'label': 'Teams',
        'blurb': 'Drafts and local history work; sending waits on admin consent.',
        'credential_slug': 'microsoft-teams',
        'cost': 'Free.',
        'setup': [
            'Ask an Azure AD admin to consent Chat.ReadWrite and ChannelMessage.Send.',
        ],
        'inbound': 'Unavailable until consent exists.',
    },
}
