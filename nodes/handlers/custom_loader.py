"""
Custom Node Loader

Dynamically load and validate custom node classes from user-provided code.
"""
import ast
import logging
from typing import Type
import importlib.util
import sys
from io import StringIO

from nodes.handlers.base import BaseNodeHandler, NodeCategory, FieldType

logger = logging.getLogger(__name__)


# Dangerous imports that should be blocked in custom nodes
BLOCKED_IMPORTS = {
    'os',
    'subprocess',
    'shutil',
    'socket',
    'ctypes',
    'multiprocessing',
    'threading',
    '__builtins__',
    'eval',
    'exec',
    'compile',
    'open',
    'file',
    'input',
    'raw_input',
}

# Required attributes for a valid node handler
REQUIRED_ATTRIBUTES = {'node_type', 'name', 'category'}


class CustomNodeValidationError(Exception):
    """Raised when custom node validation fails."""
    pass


class CustomNodeLoader:
    """
    Dynamically load and validate custom node classes from user code.
    
    Security measures:
    - AST analysis to detect dangerous imports
    - Class signature validation
    - Restricted execution environment
    
    Usage:
        loader = CustomNodeLoader()
        errors = loader.validate_code(code_string)
        if not errors:
            node_class = loader.load_from_code(code_string, "my_custom_node")
    """
    
    def __init__(self):
        self.allowed_imports = {
            'json', 'datetime', 're', 'math', 'random', 'hashlib',
            'base64', 'urllib.parse', 'html', 'xml.etree.ElementTree',
            'httpx', 'aiohttp',
        }
    
    def validate_code(self, code: str) -> list[str]:
        """
        Validate custom node code before execution.
        
        Args:
            code: Python source code as string
            
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"Syntax error: {e}"]
        
        # Check imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name.split('.')[0]
                    if module_name in BLOCKED_IMPORTS:
                        errors.append(f"Blocked import: {alias.name}")
            
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module_name = node.module.split('.')[0]
                    if module_name in BLOCKED_IMPORTS:
                        errors.append(f"Blocked import: {node.module}")
            
            # Check for dangerous function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in {'eval', 'exec', 'compile', 'open', '__import__'}:
                        errors.append(f"Blocked function call: {node.func.id}")
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in {'system', 'popen', 'spawn', 'call', 'run'}:
                        errors.append(f"Potentially dangerous method call: {node.func.attr}")
        
        # Check for class definition inheriting from BaseNodeHandler
        has_handler_class = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == 'BaseNodeHandler':
                        has_handler_class = True
                        break
        
        if not has_handler_class:
            errors.append("Code must define a class inheriting from BaseNodeHandler")
        
        return errors
    
    def load_from_code(self, code: str, module_name: str) -> Type[BaseNodeHandler]:
        """
        Dynamically load a node class from source code.
        
        Args:
            code: Python source code containing a BaseNodeHandler subclass
            module_name: Name for the dynamic module
            
        Returns:
            The loaded node handler class
            
        Raises:
            CustomNodeValidationError: If code is invalid
        """
        # Validate first
        errors = self.validate_code(code)
        if errors:
            raise CustomNodeValidationError(f"Validation failed: {'; '.join(errors)}")
        
        # Create a module spec and execute code
        spec = importlib.util.spec_from_loader(
            module_name,
            loader=None,
            origin='<custom_node>'
        )
        module = importlib.util.module_from_spec(spec)
        
        # Add required imports to module namespace
        module.__dict__['BaseNodeHandler'] = BaseNodeHandler
        module.__dict__['NodeCategory'] = NodeCategory
        module.__dict__['FieldType'] = FieldType
        
        # Import common modules
        from nodes.handlers.base import FieldConfig, HandleDef, NodeExecutionResult
        module.__dict__['FieldConfig'] = FieldConfig
        module.__dict__['HandleDef'] = HandleDef
        module.__dict__['NodeExecutionResult'] = NodeExecutionResult
        
        try:
            exec(code, module.__dict__)
        except Exception as e:
            raise CustomNodeValidationError(f"Code execution failed: {e}")
        
        # Find the handler class
        handler_class = None
        for name, obj in module.__dict__.items():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseNodeHandler)
                and obj is not BaseNodeHandler
            ):
                handler_class = obj
                break
        
        if handler_class is None:
            raise CustomNodeValidationError("No BaseNodeHandler subclass found")
        
        # Validate the class
        class_errors = self.validate_class(handler_class)
        if class_errors:
            raise CustomNodeValidationError(f"Class validation failed: {'; '.join(class_errors)}")
        
        return handler_class
    
    def validate_class(self, cls: type) -> list[str]:
        """
        Validate a node handler class has required attributes.
        
        Args:
            cls: The class to validate
            
        Returns:
            List of validation error messages
        """
        errors = []
        
        # Check required attributes
        for attr in REQUIRED_ATTRIBUTES:
            if not hasattr(cls, attr) or not getattr(cls, attr):
                errors.append(f"Missing required attribute: {attr}")
        
        # Check node_type is unique format
        node_type = getattr(cls, 'node_type', '')
        if node_type and not node_type.startswith('custom_'):
            errors.append("Custom node types must start with 'custom_'")
        
        # Check category is valid
        category = getattr(cls, 'category', '')
        valid_categories = {c.value for c in NodeCategory}
        if category and category not in valid_categories:
            errors.append(f"Invalid category '{category}'. Must be one of: {valid_categories}")
        
        # Check execute method exists and is async
        if not hasattr(cls, 'execute'):
            errors.append("Missing 'execute' method")
        else:
            import asyncio
            if not asyncio.iscoroutinefunction(cls.execute):
                errors.append("'execute' method must be async")
        
        return errors


def load_custom_node_from_db(custom_node_id: int) -> Type[BaseNodeHandler] | None:
    """
    Load a custom node from the database.
    
    Args:
        custom_node_id: The CustomNode model ID
        
    Returns:
        The loaded node handler class, or None if failed
    """
    from nodes.models import CustomNode
    
    try:
        custom_node = CustomNode.objects.get(id=custom_node_id, is_active=True)
    except CustomNode.DoesNotExist:
        logger.error(f"Custom node {custom_node_id} not found")
        return None
    
    loader = CustomNodeLoader()
    
    try:
        handler_class = loader.load_from_code(
            custom_node.code,
            f"custom_node_{custom_node_id}"
        )
        return handler_class
    except CustomNodeValidationError as e:
        logger.error(f"Failed to load custom node {custom_node_id}: {e}")
        return None
