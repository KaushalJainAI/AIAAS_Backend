"""
Tuning — fine-tune a small model on your own corrections.

The pitch is cost, not capability. A tuned small model matching the big one on
one narrow task is only interesting if you can see what it saves, so every job
carries both its own numbers and the baseline it is being measured against.
Storing the baseline on the job rather than looking it up later matters: the
model you compared against may be retired or repriced by the time anyone reads
the result, and a saving computed against today's price list would be fiction.
"""
from django.conf import settings
from django.db import models


class TuningJob(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('training', 'Training'),
        ('completed', 'Completed'),
        ('deployed', 'Deployed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tuning_jobs'
    )
    name = models.CharField(max_length=200, help_text='e.g. invoice-extract-v3')
    base_model = models.CharField(max_length=100, help_text='The model being tuned')
    dataset = models.ForeignKey(
        'datasets.Dataset',
        on_delete=models.PROTECT,
        related_name='tuning_jobs',
        help_text='PROTECT: deleting the examples a deployed model was trained on '
                  'would destroy the only record of why it behaves as it does',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    epochs_total = models.IntegerField(default=3)
    epochs_done = models.IntegerField(default=0)

    # Results. Null until the job has produced them — 0.0 would read as "scored
    # zero" rather than "not scored yet".
    accuracy = models.FloatField(null=True, blank=True)
    baseline_accuracy = models.FloatField(
        null=True, blank=True, help_text='What the untuned base model scored on the same set'
    )
    cost_per_1k_paise = models.IntegerField(
        null=True, blank=True,
        help_text='Cost per 1k calls, in paise. Integer money — no float rounding.'
    )
    baseline_cost_per_1k_paise = models.IntegerField(null=True, blank=True)

    tuned_model_id = models.CharField(
        max_length=200, blank=True, help_text='Provider id of the resulting model'
    )
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return self.name

    @property
    def accuracy_delta(self):
        if self.accuracy is None or self.baseline_accuracy is None:
            return None
        return round(self.accuracy - self.baseline_accuracy, 2)

    @property
    def cost_saving_pct(self):
        """How much cheaper per call than the model it replaces."""
        if not self.baseline_cost_per_1k_paise or self.cost_per_1k_paise is None:
            return None
        saved = self.baseline_cost_per_1k_paise - self.cost_per_1k_paise
        return round(saved / self.baseline_cost_per_1k_paise * 100, 1)

    @property
    def progress_pct(self):
        if not self.epochs_total:
            return 0
        return min(100, round(self.epochs_done / self.epochs_total * 100))
