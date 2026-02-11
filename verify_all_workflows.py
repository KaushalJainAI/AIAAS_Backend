import os
import django
import sys
import asyncio
import json

# Setup Django environment
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
django.setup()

from asgiref.sync import sync_to_async
from orchestrator.models import Workflow
from compiler.compiler import WorkflowCompiler
from executor.engine import ExecutionEngine
from logs.models import ExecutionLog

from orchestrator.interface import SupervisionLevel

async def verify_workflow(workflow):
    print(f"Verifying workflow: {workflow.name} (ID: {workflow.id})")
    try:
        # 1. Compile
        workflow_data = {
            'nodes': workflow.nodes,
            'edges': workflow.edges,
            'workflow_settings': workflow.workflow_settings
        }
        compiler = WorkflowCompiler(workflow_data, user=workflow.user)
        graph = compiler.compile()
        print(f"  - Compilation successful")
        
        # 2. Run
        engine = ExecutionEngine()
        log = ExecutionLog.objects.create(
            workflow=workflow,
            user=workflow.user,
            status='running'
        )
        
        print(f"  - Starting execution...")
        
        result = await engine.run_workflow(
            execution_id=log.execution_id,
            workflow_id=workflow.id,
            user_id=workflow.user.id,
            workflow_json=workflow_data,
            input_data={},
            credentials={},
            supervision_level=SupervisionLevel.NONE
        )
        
        # Refresh log
        log.refresh_from_db()
        print(f"  - Execution finished with status: {log.status}")
        
        return True, None
    except Exception as e:
        print(f"  - ERROR: {str(e)}")
        return False, str(e)

async def main():
    workflows = await sync_to_async(list)(Workflow.objects.all())
    print(f"Found {len(workflows)} workflows to verify.\n")
    
    results = []
    for wf in workflows:
        success, error = await verify_workflow(wf)
        results.append({
            'id': wf.id,
            'name': wf.name,
            'success': success,
            'error': error
        })
    
    print("\n--- Verification Summary ---")
    passed = len([r for r in results if r['success']])
    failed = len(results) - passed
    print(f"Total: {len(results)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    
    if failed > 0:
        print("\nFailures:")
        for r in results:
            if not r['success']:
                print(f"  - {r['name']} (ID: {r['id']}): {r['error']}")

if __name__ == "__main__":
    asyncio.run(main())
