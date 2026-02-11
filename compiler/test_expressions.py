import sys
import os
import uuid
from typing import Any

# Add the current directory to sys.path to allow importing from compiler and nodes
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from compiler.schemas import ExecutionContext

def test_expression_resolution():
    print("Starting Expression Resolution Tests...")
    
    # 1. Setup Context
    context = ExecutionContext(
        execution_id=uuid.uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={
            "node_1_id": [{"json": {"message": "Hello from Node 1", "data": {"score": 95}}}],
            "node_2_id": {"items": [{"json": {"status": "success"}}]}
        },
        node_label_to_id={
            "First Node": "node_1_id",
            "Second Node": "node_2_id"
        },
        variables={
            "user_name": "Alice"
        }
    )
    
    # 2. Test Cases
    test_cases = [
        {
            "name": "Node output by label - message",
            "expr": "{{ $node['First Node'].json.message }}",
            "expected": "Hello from Node 1"
        },
        {
            "name": "Node output by label - nested JSON",
            "expr": "{{ $node['First Node'].json.data.score }}",
            "expected": 95
        },
        {
            "name": "Node output by ID",
            "expr": "{{ $node['node_1_id'].json.message }}",
            "expected": "Hello from Node 1"
        },
        {
            "name": "Workflow variable",
            "expr": "{{ $vars.user_name }}",
            "expected": "Alice"
        },
        {
            "name": "String interpolation",
            "expr": "Greeting: {{ $node['First Node'].json.message }}, User: {{ $vars.user_name }}",
            "expected": "Greeting: Hello from Node 1, User: Alice"
        },
        {
            "name": "Nested path with brackets",
            "expr": "{{ $node['First Node'].json.data['score'] }}",
            "expected": 95
        }
    ]
    
    for case in test_cases:
        resolved = context._resolve_string_expression(case["expr"])
        if resolved == case["expected"]:
            print(f"PASS: {case['name']}")
        else:
            print(f"FAIL: {case['name']}")
            print(f"   Expected: {case['expected']} ({type(case['expected'])})")
            print(f"   Actual:   {resolved} ({type(resolved)})")

    # 3. Test resolve_expressions (Recursive/Path-based)
    print("\nTesting resolve_expressions (Pre-analyzed paths)...")
    config = {
        "text": "The score is {{ $node['First Node'].json.data.score }}",
        "user": "{{ $vars.user_name }}",
        "static": "No expression here"
    }
    paths = [["text"], ["user"]]
    
    resolved_config = context.resolve_expressions(config, paths)
    
    if resolved_config["text"] == "The score is 95" and resolved_config["user"] == "Alice":
        print("PASS: resolve_expressions correctly updated targeted fields")
    else:
        print("FAIL: resolve_expressions failed")
        print(f"   Result: {resolved_config}")

if __name__ == "__main__":
    try:
        test_expression_resolution()
    except Exception as e:
        print(f"CRASH: {e}")
        import traceback
        traceback.print_exc()
