import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0005_alter_document_file_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeBase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(default='Default', max_length=255)),
                ('description', models.TextField(blank=True)),
                ('embedding_model', models.CharField(default='Qwen/Qwen3-VL-Embedding-2B', help_text='Embedding model used for this KB', max_length=100)),
                ('vector_dim', models.IntegerField(default=1024)),
                ('doc_count', models.IntegerField(default=0)),
                ('vector_count', models.IntegerField(default=0)),
                ('index_size_bytes', models.BigIntegerField(default=0)),
                ('s3_index_key', models.CharField(blank=True, help_text='S3 key for the FAISS index bundle (not publicly downloadable)', max_length=500)),
                ('is_default', models.BooleanField(default=False, help_text="User's default KB — auto-created on first document upload")),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='knowledge_bases', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Knowledge Base',
                'verbose_name_plural': 'Knowledge Bases',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='knowledgebase',
            constraint=models.UniqueConstraint(fields=['user', 'name'], name='unique_kb_name_per_user'),
        ),
        migrations.AddIndex(
            model_name='knowledgebase',
            index=models.Index(fields=['user', 'is_default'], name='inference_k_user_id_is_default_idx'),
        ),
        migrations.AddField(
            model_name='document',
            name='knowledge_base',
            field=models.ForeignKey(
                blank=True,
                help_text='KB this document is indexed into',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='documents',
                to='inference.knowledgebase',
            ),
        ),
    ]
