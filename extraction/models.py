"""
Extract — turn a pile of documents into rows.

The rule that makes the output usable for accounting is that low-confidence
fields are flagged, not quietly guessed. So confidence is stored per row and the
threshold lives on the schema: what counts as "sure enough" for a delivery
challan is not what counts for a GST certificate, and hardcoding one number
would force the stricter case to accept the looser one's mistakes.
"""
from django.conf import settings
from django.db import models


class ExtractionSchema(models.Model):
    SOURCE_CHOICES = [
        ('upload', 'Manual upload'),
        ('gmail', 'Gmail'),
        ('gdrive', 'Google Drive'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='extraction_schemas'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # [{"name": "gstin", "label": "GSTIN", "type": "string", "required": true}, ...]
    fields = models.JSONField(default=list, help_text='The columns to fill')

    source_kind = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='upload')
    source_ref = models.CharField(
        max_length=300, blank=True, help_text='Label, folder or query the documents come from'
    )

    confidence_threshold = models.FloatField(
        default=0.8, help_text='Below this, a row is held for review rather than accepted'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return self.name

    @property
    def field_count(self):
        return len(self.fields) if isinstance(self.fields, list) else 0


class ExtractedRow(models.Model):
    STATUS_CHOICES = [
        ('accepted', 'Accepted'),
        ('needs_review', 'Needs review'),
        ('reviewed', 'Reviewed'),
        ('rejected', 'Rejected'),
    ]

    schema = models.ForeignKey(ExtractionSchema, on_delete=models.CASCADE, related_name='rows')
    document_name = models.CharField(max_length=300)
    document = models.ForeignKey(
        'inference.Document',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extracted_rows',
    )

    data = models.JSONField(default=dict, help_text='Field name -> extracted value')
    field_confidence = models.JSONField(
        default=dict, blank=True, help_text='Field name -> confidence, so review can point at the cell'
    )
    confidence = models.FloatField(default=0.0, help_text='Lowest field confidence in the row')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='accepted', db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_extractions',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['schema', 'status']),
            models.Index(fields=['schema', '-created_at']),
        ]

    def __str__(self):
        return f'{self.document_name} ({self.schema.name})'

    def apply_threshold(self):
        """Set status from confidence. Called on write so the flag can never
        disagree with the number it is derived from."""
        if self.status in ('reviewed', 'rejected'):
            return
        self.status = (
            'needs_review' if self.confidence < self.schema.confidence_threshold else 'accepted'
        )
