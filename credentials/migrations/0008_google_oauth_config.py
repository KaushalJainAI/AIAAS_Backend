"""
Data migration: give `google-oauth2` the `oauth_config` two code paths require.

The row was seeded with `oauth_config={}` — `seed_connector_credentials.py` sets
`auth_method='oauth2'` and nothing else — and two features read it before doing
anything:

  * `credentials/verification.py::_verify_oauth2` returns
    "Invalid Configuration: Missing OAuth2 setup for Google (OAuth2)" for a
    credential that is perfectly good. That is the Verify button on a connection
    the user has just successfully signed into.
  * `credentials/manager.py::refresh_oauth_token` returns False at its
    `if not oauth_config.get('token_url')` guard, so a token refreshed through
    the manager never happens at all.

Neither failure is visible as a crash; both read as "your credential is bad",
which is the one thing it is not. `Credential.get_valid_access_token` avoided
this by defaulting the URL inline (`config.get('token_url', '<google>')`), which
is why refresh worked *there* and nowhere else — a default that papers over
missing configuration hides it from every other reader.

The values are Google's published endpoints and belong on the type, not in code:
they are the same for every user, and a second Google-shaped provider (Workspace
under a different client) would need its own row with its own URLs.
"""
from django.db import migrations


GOOGLE_OAUTH_CONFIG = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "revoke_url": "https://oauth2.googleapis.com/revoke",
    # `access_type=offline` + `prompt=consent` is what makes Google return a
    # refresh token at all; without it the first connect yields an access token
    # that dies in an hour and cannot be renewed. Recorded here because the
    # value is a property of this provider, not of the view that happens to
    # build the URL today (`credentials/oauth.py`).
    "access_type": "offline",
    "prompt": "consent",
}


def _apply(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    CredentialType.objects.filter(slug="google-oauth2").update(
        oauth_config=GOOGLE_OAUTH_CONFIG
    )


def _reverse(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    CredentialType.objects.filter(slug="google-oauth2").update(oauth_config={})


class Migration(migrations.Migration):

    dependencies = [
        ("credentials", "0007_slack_workspace_id"),
    ]

    operations = [
        migrations.RunPython(_apply, reverse_code=_reverse),
    ]
