from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('eval', '0004_judgecalibration'),
    ]

    operations = [
        migrations.AddField(
            model_name='evalsuite',
            name='template_slug',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
