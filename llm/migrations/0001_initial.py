"""
Initial tables for the provider/model registry.

The `AIProvider` / `AIModel` tables were born in the `nodes` app
(`nodes_aiprovider` / `nodes_aimodel`) and handed over here state-only by
`nodes.0005_move_ai_models_to_llm`. The `nodes` app is gone now, so this
migration creates the tables directly — `db_table` stays pinned to the
historical names, which is what makes the deploy a no-op on instances where
the tables already exist.
"""
from django.db import migrations, models
import django.db.models.deletion


CAPABILITIES = [
    ('supports_text_input', True),
    ('supports_text_generation', True),
    ('supports_image_input', False),
    ('supports_image_generation', False),
    ('supports_audio_input', False),
    ('supports_audio_generation', False),
    ('supports_video_input', False),
    ('supports_video_generation', False),
    ('supports_numeric_input', False),
    ('supports_numeric_generation', False),
    ('supports_time_series_input', False),
    ('supports_time_series_generation', False),
    ('supports_document_input', False),
    ('supports_document_generation', False),
    ('supports_tabular_input', False),
    ('supports_tabular_generation', False),
    ('supports_structured_output', False),
    ('supports_tool_calling', False),
    ('supports_embedding_generation', False),
]


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='AIProvider',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('slug', models.SlugField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True)),
                ('icon', models.CharField(blank=True, max_length=50)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'AI Provider',
                'verbose_name_plural': 'AI Providers',
                'db_table': 'nodes_aiprovider',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='AIModel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('value', models.CharField(help_text='The technical name/ID of the model', max_length=150, unique=True)),
                ('description', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('is_free', models.BooleanField(default=False)),
            ] + [
                (field, models.BooleanField(default=default))
                for field, default in CAPABILITIES
            ] + [
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('provider', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='models', to='llm.aiprovider')),
            ],
            options={
                'verbose_name': 'AI Model',
                'verbose_name_plural': 'AI Models',
                'db_table': 'nodes_aimodel',
                'ordering': ['provider', 'name'],
            },
        ),
    ]