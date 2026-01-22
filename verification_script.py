import unittest
from typing import Any
from collections import defaultdict

# Mock imports to avoid Django dependencies for this test
import sys
from unittest.mock import MagicMock

# Mock Django modules
sys.modules['django'] = MagicMock()
sys.modules['django.conf'] = MagicMock()
sys.modules['django.utils'] = MagicMock()
sys.modules['django.utils.timezone'] = MagicMock()
sys.modules['django.db'] = MagicMock()

# Import the code we want to test
# We need to make sure we can import from local directory
import os
sys.path.append(os.getcwd())

from compiler.validators import topological_sort, validate_node_configs
from compiler.schemas import CompileError

# Mock Registry for validation
class MockHandler:
    node_type = 'loop'
    def validate_config(self, config): return []

sys.modules['nodes.handlers.registry'] = MagicMock()
registry_mock = sys.modules['nodes.handlers.registry']
registry_mock.get_registry.return_value.has_handler.return_value = True
registry_mock.get_registry.return_value.get_handler.return_value = MockHandler()

class TestCompilerDeterminism(unittest.TestCase):
    def test_topological_sort_determinism(self):
        """Test that topological sort is deterministic."""
        nodes = [
            {'id': 'node_c', 'type': 'code'},
            {'id': 'node_a', 'type': 'trigger'},
            {'id': 'node_b', 'type': 'code'},
            {'id': 'node_d', 'type': 'code'},
        ]
        # Diamond shape: A -> B, A -> C, B -> D, C -> D
        # Valid sorts: A, B, C, D or A, C, B, D
        # Our sort should always pick one consistently (lexicographical tie break)
        edges = [
            {'source': 'node_a', 'target': 'node_b'},
            {'source': 'node_a', 'target': 'node_c'},
            {'source': 'node_b', 'target': 'node_d'},
            {'source': 'node_c', 'target': 'node_d'},
        ]
        
        result1 = topological_sort(nodes, edges)
        result2 = topological_sort(nodes, edges)
        
        print(f"Sort 1: {result1}")
        print(f"Sort 2: {result2}")
        
        self.assertEqual(result1, result2)
        # Expect alphabetical order for siblings: A, B, C, D (since B < C)
        self.assertEqual(result1, ['node_a', 'node_b', 'node_c', 'node_d'])

    def test_loop_validation(self):
        """Test that missing max_loop_count raises error."""
        nodes = [
            {
                'id': 'loop_1',
                'type': 'loop',
                'data': {'config': {}} # Missing max_loop_count
            }
        ]
        
        errors = validate_node_configs(nodes)
        print(f"Errors: {errors}")
        
        self.assertTrue(any(e.error_type == 'missing_config' for e in errors))
        
        # Test valid
        nodes[0]['data']['config']['max_loop_count'] = 10
        errors = validate_node_configs(nodes)
        self.assertFalse(any(e.error_type == 'missing_config' for e in errors))

if __name__ == '__main__':
    unittest.main()
