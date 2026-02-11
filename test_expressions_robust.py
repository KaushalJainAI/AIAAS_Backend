import sys
import os
import uuid
from typing import Any

# Add the current directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from compiler.schemas import ExecutionContext

def test_robust_expressions():
    print("Starting Robust Expression Tests...")
    
    # 1. Setup Context with realistic Perplexity output (items list)
    context = ExecutionContext(
        execution_id=uuid.uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={
            "perp_id": [{"json": {"content": "Paris", "data": {"pop": 2000000}}}],
            "simple_dict": {"message": "Success"}
        },
        node_label_to_id={
            "Perplexity": "perp_id",
            "SimpleNode": "simple_dict"
        }
    )
    
    test_cases = [
        {
            "name": "Standard bracket notation",
            "expr": "{{ $node['Perplexity'].json.content }}",
            "expected": "Paris"
        },
        {
            "name": "Dot notation for node name",
            "expr": "{{ $node.Perplexity.json.content }}",
            "expected": "Paris"
        },
        {
            "name": "Flexible spacing in brackets",
            "expr": "{{ $node[ 'Perplexity' ].json.content }}",
            "expected": "Paris"
        },
        {
            "name": "Double quotes and brackets",
            "expr": '{{ $node["Perplexity"].json.content }}',
            "expected": "Paris"
        },
        {
            "name": "Auto-item diving (skipping .json explicitly)",
            "expr": "{{ $node.Perplexity.content }}",
            "expected": "Paris"
        },
        {
            "name": "Deep path with dot notation",
            "expr": "{{ $node.Perplexity.json.data.pop }}",
            "expected": 2000000
        },
        {
            "name": "Simple node (not items list)",
            "expr": "{{ $node.SimpleNode.message }}",
            "expected": "Success"
        },
        {
            "name": "$json alias (current input json)",
            "expr": "{{ $json.content }}",
            "expected": "Paris"
        },
        {
            "name": "$input alias (current input json)",
            "expr": "{{ $input.content }}",
            "expected": "Paris"
        },
        {
            "name": "$json nested access",
            "expr": "{{ $json.data.pop }}",
            "expected": 2000000
        }
    ]
    
    # Simulate current input being the Perplexity items
    context.current_input = context.node_outputs["perp_id"]
    
    for case in test_cases:
        resolved = context._resolve_string_expression(case["expr"])
        if resolved == case["expected"]:
            print(f"PASS: {case['name']}")
        else:
            print(f"FAIL: {case['name']}")
            print(f"   Expr:     {case['expr']}")
            print(f"   Expected: {case['expected']} ({type(case['expected'])})")
            print(f"   Actual:   {resolved} ({type(resolved)})")

if __name__ == "__main__":
    test_robust_expressions()
