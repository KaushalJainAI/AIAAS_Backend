import sys
import os
import uuid
import re
from typing import Any

# Add the current directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from compiler.schemas import ExecutionContext

def test_final_polish():
    print("Starting Final Polish Expression Tests...")
    
    # Setup Context
    context = ExecutionContext(
        execution_id=uuid.uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={
            "node-123": [{"json": {"content": "Final Success"}}]
        },
        node_label_to_id={
            "perplexity": "node-123" # Lowercase in map
        },
        current_node_id="target"
    )
    
    test_cases = [
        {
            "name": "Case-insensitive type fallback (User used 'Perplexity')",
            "expr": "{{ $node.Perplexity.json.content }}",
            "expected": "Final Success"
        },
        {
            "name": "Dot notation with dashes in ID",
            "expr": "{{ $node['node-123'].json.content }}",
            "expected": "Final Success"
        },
        {
            "name": "Whole node reference with dot (no path)",
            "expr": "{{ $node.Perplexity }}",
            "expected": [{"json": {"content": "Final Success"}}]
        },
        {
            "name": "Auto-dive with case-insensitive label",
            "expr": "{{ $node.PERPLEXITY.content }}",
            "expected": "Final Success"
        }
    ]
    
    for case in test_cases:
        resolved = context._resolve_string_expression(case["expr"])
        if resolved == case["expected"]:
            print(f"PASS: {case['name']}")
        else:
            print(f"FAIL: {case['name']}")
            print(f"   Actual: {resolved}")
            
if __name__ == "__main__":
    test_final_polish()
