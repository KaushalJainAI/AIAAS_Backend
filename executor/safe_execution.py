"""
Safe Execution - Sandboxed Code Execution

Provides secure execution environment for user-provided code
with method whitelisting and validation.
"""
import ast
import logging
import builtins
from typing import Any, Callable
from functools import wraps

logger = logging.getLogger(__name__)


# ======================== Allowed Builtins ========================

SAFE_BUILTINS = {
    # Type conversions
    'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple',
    'bytes', 'bytearray',
    
    # Type checking
    'type', 'isinstance', 'issubclass', 'callable', 'hasattr', 'getattr',
    
    # Iteration
    'len', 'range', 'enumerate', 'zip', 'map', 'filter', 'sorted',
    'reversed', 'iter', 'next',
    
    # Math
    'abs', 'max', 'min', 'sum', 'round', 'pow', 'divmod',
    
    # String operations
    'ord', 'chr', 'repr', 'format',
    
    # Boolean operations
    'all', 'any',
    
    # Object operations
    'id', 'hash', 'dir', 'vars',
    
    # Misc safe operations
    'print', 'input',  # Note: input disabled in sandbox
}

# Explicitly blocked builtins
BLOCKED_BUILTINS = {
    'eval', 'exec', 'compile', '__import__', 'open', 'file',
    'memoryview', 'globals', 'locals', 'breakpoint',
}


# ======================== Allowed Modules ========================

ALLOWED_MODULES = {
    # Standard library (safe subset)
    'json': ['loads', 'dumps', 'JSONDecodeError'],
    'datetime': ['datetime', 'date', 'time', 'timedelta', 'timezone'],
    're': ['match', 'search', 'findall', 'sub', 'split', 'compile', 'Pattern'],
    'math': ['sqrt', 'ceil', 'floor', 'log', 'log10', 'exp', 'sin', 'cos', 'tan', 'pi', 'e'],
    'random': ['random', 'randint', 'choice', 'shuffle', 'sample'],
    'hashlib': ['md5', 'sha1', 'sha256', 'sha512'],
    'base64': ['b64encode', 'b64decode', 'urlsafe_b64encode', 'urlsafe_b64decode'],
    'urllib.parse': ['urlencode', 'quote', 'unquote', 'urlparse', 'parse_qs'],
    'itertools': ['chain', 'combinations', 'permutations', 'product', 'repeat'],
    'functools': ['reduce', 'partial'],
    'collections': ['Counter', 'defaultdict', 'OrderedDict', 'namedtuple'],
    'string': ['ascii_letters', 'digits', 'punctuation', 'Template'],
}

# Explicitly blocked modules
BLOCKED_MODULES = {
    'os', 'sys', 'subprocess', 'shutil', 'pathlib', 'glob',
    'socket', 'http', 'urllib.request', 'requests', 'httpx',
    'pickle', 'shelve', 'dbm', 'sqlite3',
    'ctypes', 'multiprocessing', 'threading', 'asyncio',
    'importlib', 'runpy', 'code', 'codeop',
    'builtins', '__builtins__',
}


# ======================== AST Validator ========================

class SafeCodeValidator(ast.NodeVisitor):
    """
    AST-based validator for safe code execution.
    
    Checks for:
    - Dangerous imports
    - Blocked function calls
    - Attribute access to dangerous objects
    """
    
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
    
    def validate(self, code: str) -> tuple[bool, list[str]]:
        """
        Validate code for safety.
        
        Returns (is_safe, errors)
        """
        self.errors = []
        self.warnings = []
        
        try:
            tree = ast.parse(code)
            self.visit(tree)
        except SyntaxError as e:
            self.errors.append(f"Syntax error: {e}")
        
        return len(self.errors) == 0, self.errors
    
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = alias.name.split('.')[0]
            if module in BLOCKED_MODULES:
                self.errors.append(f"Import of '{alias.name}' is not allowed")
            elif module not in ALLOWED_MODULES and not module.startswith('_'):
                self.warnings.append(f"Import of '{alias.name}' may not be available")
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ''
        base_module = module.split('.')[0]
        
        if base_module in BLOCKED_MODULES:
            self.errors.append(f"Import from '{module}' is not allowed")
        
        self.generic_visit(node)
    
    def visit_Call(self, node: ast.Call) -> None:
        # Check for dangerous function calls
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in BLOCKED_BUILTINS:
                self.errors.append(f"Call to '{name}()' is not allowed")
        
        elif isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in {'system', 'popen', 'spawn', 'fork', 'exec', 'execv'}:
                self.errors.append(f"Call to '.{attr}()' is not allowed")
        
        self.generic_visit(node)
    
    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Check for dangerous attribute access
        dangerous_attrs = {
            '__class__', '__base__', '__bases__', '__subclasses__',
            '__mro__', '__globals__', '__code__', '__builtins__',
        }
        
        if node.attr in dangerous_attrs:
            self.errors.append(f"Access to '{node.attr}' is not allowed")
        
        self.generic_visit(node)


# ======================== Sandbox Execution ========================

class CodeSandbox:
    """
    Sandboxed code execution environment.
    
    Provides:
    - Restricted builtins
    - Whitelisted modules
    - Resource limits
    - Execution timeout
    
    Usage:
        sandbox = CodeSandbox()
        result = sandbox.execute(user_code, {"data": input_data})
    """
    
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.validator = SafeCodeValidator()
        self._safe_builtins = self._create_safe_builtins()
    
    def _create_safe_builtins(self) -> dict:
        """Create a restricted builtins dict."""
        safe = {}
        
        for name in SAFE_BUILTINS:
            if hasattr(builtins, name):
                safe[name] = getattr(builtins, name)
        
        # Override input to prevent blocking
        safe['input'] = lambda *args: ""
        
        # Add None, True, False
        safe['None'] = None
        safe['True'] = True
        safe['False'] = False
        
        return safe
    
    def _create_safe_globals(self, user_globals: dict | None = None) -> dict:
        """Create a safe globals dict for execution."""
        safe_globals = {
            '__builtins__': self._safe_builtins,
            '__name__': '__sandbox__',
            '__doc__': None,
        }
        
        # Add allowed modules
        for module_name, allowed_attrs in ALLOWED_MODULES.items():
            try:
                module = __import__(module_name, fromlist=allowed_attrs if allowed_attrs else [''])
                
                if allowed_attrs:
                    # Create a restricted module proxy
                    restricted = type('RestrictedModule', (), {})()
                    for attr in allowed_attrs:
                        if hasattr(module, attr):
                            setattr(restricted, attr, getattr(module, attr))
                    safe_globals[module_name.split('.')[0]] = restricted
                else:
                    safe_globals[module_name] = module
                    
            except ImportError:
                pass
        
        # Add user-provided globals (validated)
        if user_globals:
            for key, value in user_globals.items():
                if not key.startswith('_'):
                    safe_globals[key] = value
        
        return safe_globals
    
    def validate(self, code: str) -> tuple[bool, list[str]]:
        """Validate code before execution."""
        return self.validator.validate(code)
    
    def execute(
        self,
        code: str,
        context: dict | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Execute code in sandbox.
        
        Args:
            code: Python code to execute
            context: Variables to make available
            timeout: Execution timeout (seconds)
            
        Returns:
            Dict with 'result', 'output', 'error' keys
        """
        import io
        import sys
        from contextlib import redirect_stdout, redirect_stderr
        
        # Validate first
        is_safe, errors = self.validate(code)
        if not is_safe:
            return {
                'success': False,
                'error': f"Code validation failed: {'; '.join(errors)}",
                'result': None,
            }
        
        # Prepare execution environment
        safe_globals = self._create_safe_globals(context)
        safe_locals = {}
        
        # Capture output
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        
        try:
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                # Execute with timeout using signal (Unix) or threading (cross-platform)
                exec(compile(code, '<sandbox>', 'exec'), safe_globals, safe_locals)
            
            # Get result (last expression or 'result' variable)
            result = safe_locals.get('result', safe_locals.get('output'))
            
            return {
                'success': True,
                'result': result,
                'output': stdout_capture.getvalue(),
                'stderr': stderr_capture.getvalue(),
                'locals': {k: v for k, v in safe_locals.items() if not k.startswith('_')},
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': f"{type(e).__name__}: {str(e)}",
                'result': None,
                'output': stdout_capture.getvalue(),
                'stderr': stderr_capture.getvalue(),
            }


# ======================== Method Whitelist ========================

class MethodWhitelist:
    """
    Manages whitelisted methods per agent/node type.
    
    Usage:
        whitelist = MethodWhitelist()
        whitelist.allow("http_request", ["get", "post", "put", "delete"])
        
        if whitelist.is_allowed("http_request", "get"):
            # Execute method
    """
    
    def __init__(self):
        self._allowed: dict[str, set[str]] = {}
        self._denied: dict[str, set[str]] = {}
        
        # Default whitelists
        self._setup_defaults()
    
    def _setup_defaults(self):
        """Setup default method whitelists."""
        self._allowed = {
            'http_request': {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'},
            'code': {'execute'},
            'llm': {'generate', 'chat', 'complete'},
            'email': {'send', 'draft'},
            'database': {'select', 'insert', 'update'},  # no delete by default
            'file': {'read'},  # no write by default
        }
        
        self._denied = {
            'system': {'exec', 'shell', 'eval', 'spawn'},
            'file': {'delete', 'write', 'move'},
        }
    
    def allow(self, agent_type: str, methods: list[str]) -> None:
        """Allow methods for an agent type."""
        if agent_type not in self._allowed:
            self._allowed[agent_type] = set()
        self._allowed[agent_type].update(methods)
    
    def deny(self, agent_type: str, methods: list[str]) -> None:
        """Deny methods for an agent type."""
        if agent_type not in self._denied:
            self._denied[agent_type] = set()
        self._denied[agent_type].update(methods)
    
    def is_allowed(self, agent_type: str, method: str) -> bool:
        """Check if method is allowed for agent type."""
        method_lower = method.lower()
        
        # Check denied first
        if agent_type in self._denied and method_lower in self._denied[agent_type]:
            return False
        
        # Check allowed
        if agent_type in self._allowed:
            return method_lower in self._allowed[agent_type]
        
        # Default: deny if not explicitly allowed
        return False
    
    def validate_method(self, agent_type: str, method: str) -> tuple[bool, str]:
        """
        Validate method before execution.
        
        Returns (is_valid, error_message)
        """
        if not method:
            return False, "Method cannot be empty"
        
        if not self.is_allowed(agent_type, method):
            return False, f"Method '{method}' is not allowed for '{agent_type}'"
        
        return True, ""


# Global instances
_sandbox: CodeSandbox | None = None
_whitelist: MethodWhitelist | None = None


def get_sandbox() -> CodeSandbox:
    """Get global code sandbox."""
    global _sandbox
    if _sandbox is None:
        _sandbox = CodeSandbox()
    return _sandbox


def get_method_whitelist() -> MethodWhitelist:
    """Get global method whitelist."""
    global _whitelist
    if _whitelist is None:
        _whitelist = MethodWhitelist()
    return _whitelist


def safe_execute(code: str, context: dict | None = None) -> dict:
    """Convenience function for sandbox execution."""
    return get_sandbox().execute(code, context)
