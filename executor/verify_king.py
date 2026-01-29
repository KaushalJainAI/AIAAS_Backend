import os
import django
import asyncio
import sys
from uuid import uuid4

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from executor.king import get_orchestrator
from orchestrator.interface import ExecutionState

async def test_king_agent():
    print("--- Starting King Agent Verification ---")
    king = get_orchestrator()
    
    # 1. Test Intent Creation (Stub)
    print("\n1. Testing Intent Creation...")
    plan = await king.create_workflow_from_intent(
        user_id=1,
        prompt="Create a simple test workflow"
    )
    print(f"Generated Plan: {plan['name']}")
    assert plan['nodes'] == []
    
    # 2. Test Execution Start (Mock Workflow)
    print("\n2. Testing Execution Start...")
    # Using a dummy workflow JSON that won't actually compile/run far without real nodes
    # but sufficient to test Orchestrator->Engine handoff
    workflow_json = {
        "id": 999,
        "name": "Test Workflow",
        "nodes": [],
        "edges": []
    }
    
    handle = await king.start(
        workflow_json=workflow_json,
        user_id=1
    )
    print(f"Execution Started: {handle.execution_id}")
    print(f"Initial State: {handle.state}")
    
    # Allow some time for async task to pick up
    await asyncio.sleep(1)
    
    # Check if state progressed (likely to FAILED or COMPLETED depending on compilation)
    # Since nodes are empty, compiler might complain or pass an empty graph
    print(f"Current State: {handle.state}")
    
    # 3. Test Control (Pause/Resume)
    # Only valid if RUNNING, but it might verify the method calls at least
    print("\n3. Testing Controls...")
    paused = await king.pause(handle.execution_id)
    print(f"Pause requested: {paused}")
    
    await asyncio.sleep(0.1)
    # Check handle state
    print(f"State after pause: {handle.state}")
    
    resumed = await king.resume(handle.execution_id)
    print(f"Resume requested: {resumed}")
    
    await asyncio.sleep(0.1)
    print(f"State after resume: {handle.state}")
    
    # 4. Cleanup
    await king.stop(handle.execution_id)
    print("Test Complete.")

if __name__ == "__main__":
    asyncio.run(test_king_agent())
