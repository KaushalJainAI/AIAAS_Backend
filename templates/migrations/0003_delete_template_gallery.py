"""
Drop the DAG template gallery.

`WorkflowTemplate` stored `nodes` and `edges` and existed to clone one node
graph into another. Both ends of that are gone: there is no canvas to author a
graph on and no runtime to execute one, so every row here describes a thing the
product can no longer run. Ratings, bookmarks and comments go with it because
they are comments *on* those rows.

Agent templates are a different feature — a `SubAgent` used as a starting
point, which needs no separate table — and are not what this app was.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('templates', '0002_alter_workflowtemplate_options_and_more'),
        # Workflow.parent_template pointed here, so the model holding that FK
        # has to be gone before this table can drop.
        ('orchestrator', '0019_remove_conversationmessage_workflow_delete_workflow'),
    ]

    operations = [
        migrations.DeleteModel(name='TemplateComment'),
        migrations.DeleteModel(name='WorkflowBookmark'),
        migrations.DeleteModel(name='WorkflowRating'),
        migrations.DeleteModel(name='WorkflowTemplate'),
    ]
