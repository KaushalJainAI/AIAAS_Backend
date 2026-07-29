"""
Datasets — the examples that feed evals and tuning.

The design premise is that the valuable rows are not uploaded, they are captured:
a correction you made to an agent's output is worth more as training data than
anything synthetic, because it encodes a judgement the model got wrong. So
`source` is a first-class column and `source_execution` points back at the run a
row came from — without that link a corrected row is just an anonymous pair and
you can never ask "what was the agent looking at when it got this wrong?".
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Dataset(models.Model):
    SOURCE_CHOICES = [
        ('corrected', 'Corrected by a human'),
        ('captured', 'Captured from a run'),
        ('uploaded', 'Uploaded'),
        ('mixed', 'Mixed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='datasets'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='uploaded')

    # Percentages. Held as three columns rather than a ratio string so a split
    # can be validated and queried; the API rejects anything that isn't 100.
    train_pct = models.IntegerField(
        default=80, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    val_pct = models.IntegerField(
        default=10, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    test_pct = models.IntegerField(
        default=10, validators=[MinValueValidator(0), MaxValueValidator(100)]
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
    def row_count(self):
        return self.rows.count()

    @property
    def split_label(self):
        """"80/10/10", or "—" when nothing has been split."""
        if not any((self.train_pct, self.val_pct, self.test_pct)):
            return '—'
        return f'{self.train_pct}/{self.val_pct}/{self.test_pct}'


class DatasetRow(models.Model):
    SPLIT_CHOICES = [
        ('train', 'Train'),
        ('val', 'Validation'),
        ('test', 'Test'),
    ]

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='rows')
    inputs = models.JSONField(default=dict, help_text='What the model is given')
    expected = models.JSONField(default=dict, help_text='What it should produce')
    split = models.CharField(max_length=10, choices=SPLIT_CHOICES, default='train', db_index=True)

    # Provenance. Nullable because uploaded rows have none, but when it is set
    # you can walk back to the run and see the original document.
    source_execution = models.ForeignKey(
        'logs.ExecutionLog',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='captured_rows',
    )
    note = models.TextField(blank=True, help_text='Why this row was corrected')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        indexes = [models.Index(fields=['dataset', 'split'])]

    def __str__(self):
        return f'{self.dataset.name} row {self.pk}'
