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
    Sync Workflow to Template if is_template is True.
    """
    if getattr(instance, 'is_template', False):
        from .services import TemplateService
        service = TemplateService()
        # This will create/update the template
        service.publish_workflow_as_template(instance.id)
