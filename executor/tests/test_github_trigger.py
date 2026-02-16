import pytest
import asyncio
from uuid import uuid4
from datetime import datetime
from compiler.schemas import ExecutionContext
from nodes.handlers.triggers import GitHubTriggerNode

def test_event_expression_resolution():
    """Test that {{event...}} expressions resolve correctly."""
    context = ExecutionContext(
        execution_id=uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={
            "_input_global": {
                "body": {
                    "commits": [
                        {"message": "Fixed authentication bug"}
                    ],
                    "repository": {"full_name": "owner/repo"}
                },
                "event": "push",
                "trigger_type": "github"
            }
        }
    )
    
    # Test top-level event
    assert context._evaluate_expression("event.event") == "push"
    
    # Test nested event (should dive into 'body')
    assert context._evaluate_expression("event.commits[0].message") == "Fixed authentication bug"
    assert context._evaluate_expression("event.repository.full_name") == "owner/repo"

def test_github_payload_normalization_logic():
    """Test payload normalization logic in ExecutionContext."""
    github_event = "pull_request"
    body_data = {
        "action": "opened",
        "pull_request": {"title": "New feature"},
        "sender": {"login": "octocat"}
    }
    
    input_data = {
        "headers": {"X-GitHub-Event": "pull_request"},
        "body": body_data,
        "trigger_type": "github",
        "event": github_event,
        "action": body_data.get("action", ""),
        "payload": body_data,
        "sender": body_data.get("sender", {}),
    }
    
    context = ExecutionContext(
        execution_id=uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={"_input_global": input_data}
    )
    
    assert context._evaluate_expression("event.pull_request.title") == "New feature"
    assert context._evaluate_expression("event.sender.login") == "octocat"
    assert context._evaluate_expression("event.action") == "opened"

@pytest.mark.asyncio
async def test_github_trigger_node_refinement():
    """Test the refined execute method of GitHubTriggerNode."""
    node = GitHubTriggerNode()
    
    # Mock GitHub push payload
    payload = {
        "ref": "refs/heads/main",
        "after": "head_sha_123",
        "repository": {
            "full_name": "owner/repo"
        },
        "sender": {
            "login": "octocat"
        },
        "commits": [
            {
                "distinct": True,
                "message": "Integrated AI feedback",
                "stats": {"additions": 10, "deletions": 2},
                "files": [
                    {"filename": "app.py", "status": "modified", "patch": "@@ -1 +1,2 @@\n+print('hi')"}
                ]
            }
        ]
    }
    
    input_data = {
        "event": "push",
        "payload": payload,
        "action": "triggered",
        "ref": "refs/heads/main"
    }
    
    config = {"include_raw": True}
    context = ExecutionContext(execution_id=uuid4(), user_id=1, workflow_id=1)
    
    result = await node.execute(input_data, config, context)
    assert result.success is True
    data = result.items[0].json
    
    # Verify refined structure
    assert data["project_context"]["repository"] == "owner/repo"
    assert data["project_context"]["branch"] == "main"
    assert data["project_context"]["head_sha"] == "head_sha_123"
    assert data["change_summary"]["commit_count"] == 1
    assert data["change_summary"]["total_additions"] == 10
    assert data["code_changes"][0]["file"] == "app.py"
    assert "patch" in data["code_changes"][0]
    assert data["raw_payload"] == payload
    
    # Verify backward compatibility
    assert data["repository"] == "owner/repo"
    assert data["event"] == "push"
    assert data["payload"] == payload
    assert "triggered_at" in data

if __name__ == "__main__":
    import sys
    # Manual execution helper
    async def run_all():
        try:
            test_event_expression_resolution()
            print("test_event_expression_resolution PASSED")
            test_github_payload_normalization_logic()
            print("test_github_payload_normalization_logic PASSED")
            await test_github_trigger_node_refinement()
            print("test_github_trigger_node_refinement PASSED")
        except Exception as e:
            print(f"TEST FAILED: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
            
    asyncio.run(run_all())
