from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0016_publishedpage'),
    ]

    operations = [
        migrations.AddField(
            model_name='publishedpage',
            name='file',
            field=models.FileField(blank=True, default='', upload_to='published_pages/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='publishedpage',
            name='file_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
