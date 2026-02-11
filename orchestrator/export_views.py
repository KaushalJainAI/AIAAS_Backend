"""
Export Views

API endpoint for exporting workflows as standalone Flask apps (ZIP download).
"""
import io
import re
import zipfile
import tempfile
import shutil
from pathlib import Path

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from django.http import HttpResponse
from django.core.management import call_command


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def export_workflow_zip(request, workflow_id):
    """
    Export a workflow as a standalone Flask app ZIP file.
    
    POST /api/workflows/{workflow_id}/export/
    → Returns: application/zip
    """
    from orchestrator.models import Workflow

    # Verify ownership
    try:
        workflow = Workflow.objects.get(id=workflow_id, user=request.user)
    except Workflow.DoesNotExist:
        return HttpResponse(
            '{"error": "Workflow not found"}',
            content_type='application/json',
            status=404
        )

    # Generate into a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / 'export'
        output_dir.mkdir()

        # Call the management command programmatically
        try:
            call_command(
                'export_standalone',
                workflow_id,
                output_dir=str(output_dir),
                zip=False,  # We'll ZIP it ourselves for the response
            )
        except Exception as e:
            return HttpResponse(
                f'{{"error": "Export failed: {str(e)}"}}',
                content_type='application/json',
                status=500
            )

        # Find the generated folder (named after the workflow)
        export_folders = [d for d in output_dir.iterdir() if d.is_dir()]
        if not export_folders:
            return HttpResponse(
                '{"error": "Export produced no output"}',
                content_type='application/json',
                status=500
            )

        export_folder = export_folders[0]
        safe_name = export_folder.name

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file in export_folder.rglob('*'):
                if file.is_file():
                    arcname = str(Path(safe_name) / file.relative_to(export_folder))
                    zf.write(file, arcname)

        zip_buffer.seek(0)

        response = HttpResponse(zip_buffer.read(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{safe_name}.zip"'
        return response
