import sys
import os
import uuid
from typing import Any

# Add the current directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from compiler.schemas import ExecutionContext

def reproduce_issue():
    print("Reproducing Expression Resolution Issue...")
    
    # Simulate Perplexity node output (serialized items list)
    perplexity_output = [
        {
            "json": {
                "content": "The capital of France is Paris.",
                "model": "pplx-70b-online",
                "usage": {"total_tokens": 150}
            }
        }
    ]
    
    context = ExecutionContext(
        execution_id=uuid.uuid4(),
        user_id=1,
        workflow_id=1,
        node_outputs={
            "perplexity_node_id": perplexity_output
        },
        node_label_to_id={
            "Perplexity": "perplexity_node_id"
        }
    )
    
    # The expression the user reported
    expr = "{{ $node['Perplexity'].json.content }}"
    
    print(f"Testing expression: {expr}")
    resolved = context._resolve_string_expression(expr)
    
    if resolved == "The capital of France is Paris.":
        print(f"PASS: Resolved to: {resolved}")
    else:
        print(f"FAIL: Resolved to: {resolved}")
        
    # Test case 2: what if it's double quotes?
    expr2 = '{{ $node["Perplexity"].json.content }}'
    print(f"Testing expression (double quotes): {expr2}")
    resolved2 = context._resolve_string_expression(expr2)
    if resolved2 == "The capital of France is Paris.":
        print(f"PASS: Resolved to: {resolved2}")
    else:
        print(f"FAIL: Resolved to: {resolved2}")

    # Test case 3: what if they use dot notation without brackets (not supported yet?)
    expr3 = "{{ $node.Perplexity.json.content }}"
    print(f"Testing expression (dot notation): {expr3}")
    resolved3 = context._resolve_string_expression(expr3)
    print(f"Result for dot notation: {resolved3}")

if __name__ == "__main__":
    reproduce_issue()
