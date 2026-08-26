from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodel',
            name='cached_input_price_per_million',
            field=models.DecimalField(blank=True, decimal_places=4, help_text='USD per 1M cached input tokens, if provider offers caching', max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='aimodel',
            name='context_window',
            field=models.IntegerField(default=0, help_text='Max context tokens (0 = unknown/variable)'),
        ),
        migrations.AddField(
            model_name='aimodel',
            name='input_price_per_million',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.0000'), help_text='USD per 1M input tokens (0 = local/free)', max_digits=10),
        ),
        migrations.AddField(
            model_name='aimodel',
            name='output_price_per_million',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.0000'), help_text='USD per 1M output tokens', max_digits=10),
        ),
    ]
