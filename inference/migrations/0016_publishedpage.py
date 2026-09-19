from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inference', '0015_document_office_types'),
    ]

    operations = [
        migrations.CreateModel(
            name='PublishedPage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=220, unique=True)),
                ('title', models.CharField(max_length=200)),
                ('kind', models.CharField(choices=[('report', 'Report'), ('html', 'HTML'), ('file', 'File')], default='report', max_length=10)),
                ('body', models.TextField(blank=True, default='')),
                ('visibility', models.CharField(choices=[('link', 'Anyone with the link'), ('platform', 'Everyone on the platform'), ('public', 'Anyone, including people without an account')], default='platform', max_length=10)),
                ('is_listed', models.BooleanField(default=True)),
                ('withdrawn_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=models.CASCADE, related_name='published_pages', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Published page',
                'verbose_name_plural': 'Published pages',
                'ordering': ['-updated_at'],
                'indexes': [models.Index(fields=['is_listed', 'visibility', '-updated_at'], name='inference_p_is_liste_2b0e1a_idx'), models.Index(fields=['owner', '-updated_at'], name='inference_p_owner_i_8c9d47_idx')],
            },
        ),
    ]
