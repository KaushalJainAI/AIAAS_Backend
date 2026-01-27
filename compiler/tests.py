from django.test import TestCase
from compiler.compiler import WorkflowCompiler, WorkflowCompilationError
from langgraph.graph.state import CompiledStateGraph

class WorkflowCompilerTests(TestCase):
    def test_compile_valid_linear_workflow(self):
        """Test compiling a simple linear workflow A->B"""
        workflow_data = {
            "nodes": [
                {"id": "node_1", "type": "manual_trigger", "data": {}},
                {"id": "node_2", "type": "code", "data": {"config": {"code": "print('hi')"}}}
            ],
            "edges": [
                {"source": "node_1", "target": "node_2"}
            ],
            "settings": {}
        }
        
        compiler = WorkflowCompiler(workflow_data)
        graph = compiler.compile()
        
        self.assertIsInstance(graph, CompiledStateGraph)
        
    def test_compile_invalid_dag_cycle(self):
        """Test cycle detection raises WorkflowCompilationError"""
        workflow_data = {
            "nodes": [
                {"id": "node_1", "type": "code"},
                {"id": "node_2", "type": "code"}
            ],
            "edges": [
                {"source": "node_1", "target": "node_2"},
                {"source": "node_2", "target": "node_1"}
            ],
            "settings": {}
        }
        
        compiler = WorkflowCompiler(workflow_data)
        
        with self.assertRaises(WorkflowCompilationError) as cm:
            compiler.compile()
            
        self.assertIn("Invalid DAG", str(cm.exception))

    def test_compile_missing_credential(self):
        """Test credential validation"""
        workflow_data = {
            "nodes": [
                {
                    "id": "node_1", 
                    "type": "http_request", 
                    "data": {
                        "config": {"credential_id": "cred_missing"}
                    }
                }
            ],
            "edges": [],
            "settings": {}
        }
        
        # User has no creds
        compiler = WorkflowCompiler(workflow_data, user_credentials=set())
        
        with self.assertRaises(WorkflowCompilationError) as cm:
            compiler.compile()
            
        self.assertIn("Workflow validation failed", str(cm.exception))
