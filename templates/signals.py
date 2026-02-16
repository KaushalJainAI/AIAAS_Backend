from django.db.models.signals import post_save
from django.dispatch import receiver
from orchestrator.models import Workflow
# from .models import WorkflowTemplate # Avoid circular import if needed
# from .services import TemplateService

# Signals to auto-publish workflows or sync stats?
# User requested "The respective workflow should be publically available as template"
# but defaulting to auto-publish every draft might be too aggressive.
# Let's keep it manual for now via the 'publish' endpoint, 
# OR check if 'is_template' flag on Workflow is set (I removed that flag though? No, Workflow model still has it).

# If Workflow.is_template is True, we sync it to WorkflowTemplate.

@receiver(post_save, sender=Workflow)
def sync_workflow_template(sender, instance, created, **kwargs):
    """
    Sync Workflow to Template ONLY if it is deployed (active).
    If it becomes non-active, we should hide/archive the template.
    """
    from .services import TemplateService
    from .models import WorkflowTemplate
    from asgiref.sync import async_to_sync
    
    service = TemplateService()
    
    if instance.status == 'active':
        # This will create/update the template (scrubbed)
        # Calling async from sync signal
        async_to_sync(service.publish_workflow_as_template)(instance.id)
    else:
        # If it's no longer active, we should unpublish it from the gallery
        # We can either delete it or set status to 'draft'
        WorkflowTemplate.objects.filter(source_workflow_id=instance.id).update(status='draft')
