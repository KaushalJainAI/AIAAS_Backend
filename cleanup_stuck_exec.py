import os
import django
from uuid import UUID

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from logs.models import ExecutionLog, NodeExecutionLog

EXECUTION_ID = '26adf2ea-7eb8-41b3-9651-0068b1e4aaa4'

def cleanup():
    try:
        exec_log = ExecutionLog.objects.get(execution_id=EXECUTION_ID)
        print(f"Found execution {EXECUTION_ID}")
        
        # Mark all running nodes as failed
        running_nodes = exec_log.node_logs.filter(status='running')
        for node in running_nodes:
            print(f"Marking node {node.node_id} as failed")
            node.status = 'failed'
            node.error_message = 'Cleaned up by debug script due to hang'
            node.save()
            
        # Mark execution as failed
        if exec_log.status == 'running':
            print("Marking execution as failed")
            exec_log.status = 'failed'
            exec_log.error_message = 'Execution cancelled and cleaned up due to hang'
            exec_log.save()
            
        print("Cleanup successful")
    except ExecutionLog.DoesNotExist:
        print(f"Execution {EXECUTION_ID} not found")
    except Exception as e:
        print(f"Cleanup failed: {e}")

if __name__ == "__main__":
    cleanup()
