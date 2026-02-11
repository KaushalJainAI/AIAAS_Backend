import os
import django
from django.conf import settings

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from logs.models import ExecutionLog, NodeExecutionLog

def verify_logs():
    print(f"Total Execution Logs: {ExecutionLog.objects.count()}")
    
    last_exec = ExecutionLog.objects.last()
    if last_exec:
        print("\nLatest Execution:")
        print(f"  ID: {last_exec.execution_id}")
        print(f"  Status: {last_exec.status}")
        print(f"  Workflow: {last_exec.workflow.name if last_exec.workflow else 'None'}")
        print(f"  Created At: {last_exec.created_at}")
        
        node_logs_count = last_exec.node_logs.count()
        print(f"  Node Logs Count: {node_logs_count}")
        
        if node_logs_count > 0:
            print("  Node Logs:")
            for log in last_exec.node_logs.all().order_by('execution_order'):
                print(f"    - [{log.status}] {log.node_type} (ID: {log.node_id})")
    else:
        print("No executions found.")

if __name__ == "__main__":
    verify_logs()
