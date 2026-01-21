"""
Executor Tests - Unit tests for NodeRunner and WorkflowExecutor

Tests cover:
- Individual node execution with mocked handlers
- Workflow execution with multiple nodes
- Conditional routing (If node branching)
- Error handling and timeouts
"""
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from uuid import uuid4

from django.test import TestCase

from compiler.schemas import ExecutionContext, WorkflowExecutionPlan, NodeExecutionPlan
from nodes.handlers.base import NodeExecutionResult


class TestExecutionContext(TestCase):
    """Tests for ExecutionContext helper methods."""
    
    def setUp(self):
        self.context = ExecutionContext(
            execution_id=uuid4(),
            user_id=1,
            workflow_id=1
        )
    
    def test_set_and_get_node_output(self):
        """Test storing and retrieving node outputs."""
        output = {"result": "success", "data": [1, 2, 3]}
        self.context.set_node_output("node_1", output)
        
        retrieved = self.context.get_node_output("node_1")
        self.assertEqual(retrieved, output)
        self.assertTrue(self.context.has_executed("node_1"))
    
    def test_get_node_output_not_found(self):
        """Test getting output for non-existent node."""
        result = self.context.get_node_output("nonexistent")
        self.assertIsNone(result)
    
    def test_get_input_for_node(self):
        """Test collecting input from upstream nodes."""
        # Set up outputs from upstream nodes
        self.context.set_node_output("node_1", {"a": 1})
        self.context.set_node_output("node_2", {"b": 2})
        
        edges = [
            {"source": "node_1", "target": "node_3"},
            {"source": "node_2", "target": "node_3"},
        ]
        
        input_data = self.context.get_input_for_node("node_3", edges)
        
        self.assertEqual(input_data, {"a": 1, "b": 2})
    
    def test_variables(self):
        """Test workflow variable get/set."""
        self.context.set_variable("api_key", "secret123")
        
        self.assertEqual(self.context.get_variable("api_key"), "secret123")
        self.assertEqual(self.context.get_variable("nonexistent", "default"), "default")
    
    def test_credentials(self):
        """Test credential access."""
        self.context.credentials = {"openai": {"api_key": "sk-123"}}
        
        cred = self.context.get_credential("openai")
        self.assertEqual(cred, {"api_key": "sk-123"})
        self.assertIsNone(self.context.get_credential("nonexistent"))


class TestNodeRunner(TestCase):
    """Tests for NodeRunner class."""
    
    def setUp(self):
        self.context = ExecutionContext(
            execution_id=uuid4(),
            user_id=1,
            workflow_id=1
        )
    
    @patch('executor.runner.get_registry')
    def test_run_successful_node(self, mock_get_registry):
        """Test running a node that succeeds."""
        from executor.runner import NodeRunner
        
        # Mock the handler
        mock_handler = Mock()
        mock_handler.execute = AsyncMock(return_value=NodeExecutionResult(
            success=True,
            data={"response": "ok"},
            output_handle="success"
        ))
        mock_handler.validate_config.return_value = []
        
        # Mock the registry
        mock_registry = Mock()
        mock_registry.has_handler.return_value = True
        mock_registry.get_handler.return_value = mock_handler
        mock_get_registry.return_value = mock_registry
        
        runner = NodeRunner()
        result = asyncio.run(runner.run(
            node_id="test_node",
            node_type="http_request",
            config={"url": "https://example.com"},
            input_data={"key": "value"},
            context=self.context
        ))
        
        self.assertTrue(result.success)
        self.assertEqual(result.data, {"response": "ok"})
        self.assertEqual(result.output_handle, "success")
    
    @patch('executor.runner.get_registry')
    def test_run_unknown_node_type(self, mock_get_registry):
        """Test running an unknown node type."""
        from executor.runner import NodeRunner
        
        mock_registry = Mock()
        mock_registry.has_handler.return_value = False
        mock_get_registry.return_value = mock_registry
        
        runner = NodeRunner()
        result = asyncio.run(runner.run(
            node_id="test_node",
            node_type="unknown_type",
            config={},
            input_data={},
            context=self.context
        ))
        
        self.assertFalse(result.success)
        self.assertIn("Unknown node type", result.error)
    
    @patch('executor.runner.get_registry')
    def test_run_node_with_timeout(self, mock_get_registry):
        """Test node timeout handling."""
        from executor.runner import NodeRunner
        
        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)  # Longer than timeout
            return NodeExecutionResult(success=True, data={})
        
        mock_handler = Mock()
        mock_handler.execute = slow_execute
        mock_handler.validate_config.return_value = []
        
        mock_registry = Mock()
        mock_registry.has_handler.return_value = True
        mock_registry.get_handler.return_value = mock_handler
        mock_get_registry.return_value = mock_registry
        
        runner = NodeRunner()
        result = asyncio.run(runner.run(
            node_id="slow_node",
            node_type="slow",
            config={},
            input_data={},
            context=self.context,
            timeout_seconds=1  # Short timeout
        ))
        
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)


class TestWorkflowExecutor(TestCase):
    """Tests for WorkflowExecutor class."""
    
    def setUp(self):
        self.context = ExecutionContext(
            execution_id=uuid4(),
            user_id=1,
            workflow_id=1
        )
    
    @patch('executor.runner.get_registry')
    def test_execute_simple_workflow(self, mock_get_registry):
        """Test executing a simple 2-node workflow."""
        from executor.runner import WorkflowExecutor
        
        # Mock handler that echoes input
        mock_handler = Mock()
        mock_handler.execute = AsyncMock(side_effect=lambda inp, cfg, ctx: NodeExecutionResult(
            success=True,
            data={"received": inp},
            output_handle="output"
        ))
        mock_handler.validate_config.return_value = []
        
        mock_registry = Mock()
        mock_registry.has_handler.return_value = True
        mock_registry.get_handler.return_value = mock_handler
        mock_get_registry.return_value = mock_registry
        
        # Create execution plan
        plan = WorkflowExecutionPlan(
            workflow_id=1,
            execution_order=["trigger", "action"],
            nodes={
                "trigger": NodeExecutionPlan(
                    node_id="trigger",
                    node_type="manual_trigger",
                    config={},
                    dependencies=[]
                ),
                "action": NodeExecutionPlan(
                    node_id="action",
                    node_type="http_request",
                    config={"url": "https://api.example.com"},
                    dependencies=["trigger"]
                )
            },
            entry_points=["trigger"]
        )
        
        edges = [{"source": "trigger", "target": "action"}]
        
        executor = WorkflowExecutor(execution_plan=plan, edges=edges)
        output, status = asyncio.run(executor.execute(
            input_data={"initial": "data"},
            context=self.context
        ))
        
        self.assertEqual(status, "completed")
        self.assertIn("received", output)


class TestConditionalRouting(TestCase):
    """Tests for If/Switch node conditional routing."""
    
    def setUp(self):
        self.context = ExecutionContext(
            execution_id=uuid4(),
            user_id=1,
            workflow_id=1
        )
    
    @patch('executor.runner.get_registry')
    def test_if_node_true_branch(self, mock_get_registry):
        """Test If node routing to 'true' branch."""
        from executor.runner import WorkflowExecutor
        
        def mock_execute(inp, cfg, ctx):
            node_type = ctx.current_node_id
            if node_type == "if_node":
                return NodeExecutionResult(
                    success=True,
                    data={"condition": True},
                    output_handle="true"  # Take true branch
                )
            return NodeExecutionResult(success=True, data={"ran": node_type})
        
        mock_handler = Mock()
        mock_handler.execute = AsyncMock(side_effect=mock_execute)
        mock_handler.validate_config.return_value = []
        
        mock_registry = Mock()
        mock_registry.has_handler.return_value = True
        mock_registry.get_handler.return_value = mock_handler
        mock_get_registry.return_value = mock_registry
        
        # Create execution plan with branching
        plan = WorkflowExecutionPlan(
            workflow_id=1,
            execution_order=["trigger", "if_node", "true_action", "false_action"],
            nodes={
                "trigger": NodeExecutionPlan(node_id="trigger", node_type="manual_trigger", config={}, dependencies=[]),
                "if_node": NodeExecutionPlan(node_id="if_node", node_type="if", config={}, dependencies=["trigger"]),
                "true_action": NodeExecutionPlan(node_id="true_action", node_type="set", config={}, dependencies=["if_node"]),
                "false_action": NodeExecutionPlan(node_id="false_action", node_type="set", config={}, dependencies=["if_node"]),
            },
            entry_points=["trigger"]
        )
        
        edges = [
            {"source": "trigger", "target": "if_node"},
            {"source": "if_node", "target": "true_action", "sourceHandle": "true"},
            {"source": "if_node", "target": "false_action", "sourceHandle": "false"},
        ]
        
        executor = WorkflowExecutor(execution_plan=plan, edges=edges)
        output, status = asyncio.run(executor.execute(
            input_data={},
            context=self.context
        ))
        
        self.assertEqual(status, "completed")
        # Verify true_action ran and false_action was skipped
        self.assertTrue(self.context.has_executed("true_action"))
        # false_action should be skipped (not in executed_nodes)
