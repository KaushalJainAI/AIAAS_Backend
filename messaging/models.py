"""
Messaging channels: one account row, one outbox, one inbox per channel.

Gmail is the only channel that ends at send. Everything else — Slack, Teams,
WhatsApp, SMS, Telegram — goes through here so drafts, sends, searches and
inbound replies share one shape instead of five similar ones that drift.

- `MessagingAccount`: one user's presence on one channel. The `secret` in its
  webhook path attributes inbound traffic to its owner — a provider-level
  webhook could never say whose customer wrote in.
- `OutboundMessage`: drafts and sends. A draft never leaves the platform; a
  send writes the provider id back, or the reason it could not.
- `InboundMessage`: what customers and teammates wrote in. The only
  searchable store for WhatsApp and SMS, whose providers keep no history API
  worth using. Purged after `MESSAGING_RETENTION_DAYS` (default 90).
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


class MessagingAccount(models.Model):
    """One user's presence on one channel."""

    CHANNEL_CHOICES = [
        ('slack', 'Slack'),
        ('whatsapp', 'WhatsApp'),
        ('teams', 'Teams'),
        ('sms', 'SMS'),
        ('telegram', 'Telegram'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='messaging_accounts',
    )
    channel = models.CharField(max_length=12, choices=CHANNEL_CHOICES)
    label = models.CharField(
        max_length=120, blank=True, default='',
        help_text='Workspace, number or team name, for the picker',
    )
    credential_slug = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Vault credential type holding this channel\'s tokens',
    )
    verified = models.BooleanField(
        default=False,
        help_text='The channel handshake completed (Slack OAuth, WhatsApp '
                  'verification, Telegram webhook)',
    )
    #: The webhook credential. In the path, like every other receiver here.
    secret = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['channel', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'channel', 'label'],
                name='unique_messaging_account_per_label',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'channel']),
            models.Index(fields=['secret']),
        ]

    def __str__(self):
        return f'{self.user_id}/{self.channel} ({self.label or "default"})'


class OutboundMessage(models.Model):
    """Something drafted or sent on a channel."""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='outbound_messages',
    )
    account = models.ForeignKey(
        MessagingAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='outbound',
    )
    channel = models.CharField(max_length=12)
    to = models.CharField(
        max_length=255, help_text='Channel id, phone number or handle sent to',
    )
    body = models.TextField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    provider_message_id = models.CharField(max_length=255, blank=True, default='')
    error = models.TextField(blank=True)
    #: What the send cost, in rupees — WhatsApp and SMS only; Slack/Teams
    #: reads and posts are free at our scale.
    cost_inr = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'channel', '-created_at']),
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return f'{self.channel} → {self.to} ({self.status})'


class InboundMessage(models.Model):
    """Something written in on a channel."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='inbound_messages',
        help_text='Null when the sender could not be attributed to an account',
    )
    account = models.ForeignKey(
        MessagingAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inbound',
    )
    channel = models.CharField(max_length=12)
    sender = models.CharField(max_length=255, blank=True, default='')
    thread_id = models.CharField(max_length=255, blank=True, default='')
    body = models.TextField(blank=True)
    raw = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['user', 'channel', '-received_at']),
            models.Index(fields=['account', '-received_at']),
        ]

    def __str__(self):
        return f'{self.channel} from {self.sender or "?"}'
