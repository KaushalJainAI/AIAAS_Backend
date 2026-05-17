from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0007_rename_inference_k_user_id_is_default_idx_inference_k_user_id_9ba3d7_idx_and_more'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=['user', '-created_at', '-id'], name='inference_d_user_id_0319f0_idx'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=['sharing_mode', '-created_at', '-id'], name='inference_d_sharing_0f720a_idx'),
        ),
    ]
