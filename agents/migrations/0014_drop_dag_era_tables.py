from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orchestrator', '0013_alter_workflow_llm_provider'),
    ]

    operations = [
        migrations.DeleteModel(name='TriggerState'),
        migrations.DeleteModel(name='WorkflowCloneHistory'),
        migrations.DeleteModel(name='WorkflowTestResult'),
        migrations.DeleteModel(name='WorkflowVersion'),
    ]