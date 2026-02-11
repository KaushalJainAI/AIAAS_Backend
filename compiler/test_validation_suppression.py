import sys
import os

# Add the current directory and Backend to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(root_dir)
sys.path.append(backend_dir)

from compiler.validators import validate_expressions

def test_validate_expressions_suppression():
    print("Testing expression validation suppression...")
    
    node = {
        "id": "test_node",
        "data": {
            "label": "Test Node",
            "config": {
                "field": "{{ $node['Missing Node'].json.data }}"
            }
        }
    }
    
    all_nodes = [
        {
            "id": "test_node",
            "data": {"label": "Test Node"}
        }
    ]
    
    errors = validate_expressions(node, all_nodes)
    
    if len(errors) == 0:
        print("PASS: No warnings generated for missing node reference.")
    else:
        print(f"FAIL: {len(errors)} errors/warnings found.")
        for err in errors:
            print(f"   - {err.message}")

if __name__ == "__main__":
    test_validate_expressions_suppression()
