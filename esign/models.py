"""
Signature requests: an offer letter (or any document) out for e-signature.

`request_signature` sends through `ESIGN_ENGINE` (decision D6: Documenso for
v1); completion arrives on the webhook as event `esign.completed`, which a
mission (P7) can wait on. Until then the row is polled with
`signature_status`. An Aadhaar eSign provider only if a customer needs it —
and payments are out entirely, so this signs documents, never charges.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


class SignatureRequest(models.Model):
    """One document out for signature."""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('completed', 'Completed'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='signature_requests',
    )
    document = models.ForeignKey(
        'inference.Document',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='signature_requests',
        help_text='The file sent. Null if it has since been deleted.',
    )
    path = models.CharField(
        max_length=500,
        help_text='VFS path of the document, as the model named it',
    )
    signers = models.JSONField(
        default=list,
        help_text='[{name, email}] in signing order',
    )
    message = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    provider_request_id = models.CharField(max_length=255, blank=True, default='')
    #: The webhook credential. In the path, like every other receiver here —
    #: and every refusal is the same 404, so it cannot be probed.
    secret = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['secret']),
        ]

    def __str__(self):
        return f'Signature {self.id} ({self.status})'
