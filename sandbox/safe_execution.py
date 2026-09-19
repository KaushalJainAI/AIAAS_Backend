"""
Safe Execution - Sandboxed Code Execution

Provides secure execution environment for user-provided code
with method whitelisting and validation.
"""
import ast
import logging
import builtins
import importlib
import types
import threading
from typing import Any

logger = logging.getLogger(__name__)


# ======================== Allowed Builtins ========================

SAFE_BUILTINS = {
    # Type conversions
    'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple',
    'bytes', 'bytearray',
    
    # Type checking
    'type', 'isinstance', 'issubclass', 'callable', 'hasattr',
    
    # Iteration
    'len', 'range', 'enumerate', 'zip', 'map', 'filter', 'sorted',
    'reversed', 'iter', 'next',
    
    # Math
    'abs', 'max', 'min', 'sum', 'round', 'pow', 'divmod',
    
    # String operations
    'ord', 'chr', 'repr', 'format',
    
    # Boolean operations
    'all', 'any',
    
    # Object operations. `dir`/`vars` are deliberately excluded: they walk an
    # object's internals and are a stepping stone to the class hierarchy an
    # escape needs, and no arithmetic/data task requires them.
    'id', 'hash',

    # Misc safe operations
    'print', 'input',  # Note: input disabled in sandbox

    # Exceptions users commonly raise in code nodes
    'Exception', 'ValueError', 'TypeError', 'KeyError', 'RuntimeError',
}

# Explicitly blocked builtins
BLOCKED_BUILTINS = {
    'eval', 'exec', 'compile', '__import__', 'open', 'file',
    'memoryview', 'globals', 'locals', 'breakpoint',
    'getattr', 'setattr', 'delattr',
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
    'collections': ['Counter', 'defaultdict', 'OrderedDict', 'namedtuple', 'deque'],
    'string': ['ascii_letters', 'digits', 'punctuation', 'Template'],
    # What data work reaches for first. None of these can touch a file or the
    # network without `open`/`io.FileIO`, which stay unavailable; `io` exposes
    # only the in-memory buffers `csv` needs. Added 2026-09-17 because the
    # benchmark's analyst tasks could not parse a CSV in the dev engine at all,
    # while the production sidecar has the full standard library.
    'csv': ['reader', 'writer', 'DictReader', 'DictWriter', 'QUOTE_MINIMAL', 'QUOTE_ALL'],
    'io': ['StringIO'],
    'statistics': ['mean', 'median', 'mode', 'stdev', 'pstdev', 'fmean'],
    'decimal': ['Decimal', 'ROUND_HALF_UP', 'ROUND_HALF_EVEN', 'getcontext'],
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


def _restricted_module(module_name: str):
    """A proxy exposing only the allow-listed attributes of one module."""
    module = importlib.import_module(module_name)
    proxy = types.SimpleNamespace()
    for attr in ALLOWED_MODULES[module_name]:
        if hasattr(module, attr):
            setattr(proxy, attr, getattr(module, attr))
    return proxy


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    """`__import__` for the sandbox: allow-listed modules, as restricted proxies."""
    if level != 0 or name not in ALLOWED_MODULES:
        allowed = ', '.join(sorted(ALLOWED_MODULES))
        raise ImportError(
            f"Module '{name}' is not available in this sandbox. Available: {allowed}."
        )
    proxy = _restricted_module(name)
    if fromlist or '.' not in name:
        return proxy
    # `import urllib.parse` binds the top-level name and reaches the leaf
    # through attributes, so build that chain out of proxies too.
    head, *rest = name.split('.')
    root = node = types.SimpleNamespace()
    for part in rest[:-1]:
        setattr(node, part, types.SimpleNamespace())
        node = getattr(node, part)
    setattr(node, rest[-1], proxy)
    return root


# ======================== AST Validator ========================

class SafeCodeValidator(ast.NodeVisitor):
    """
    AST-based validator for safe code execution.
    
    Checks for:
    - Dangerous imports
    - Blocked function calls
    - Attribute access to dangerous objects
    """
    
    def __init__(self, *, allow_open: bool = False):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # `open` is blocked by default (no filesystem in the sandbox) and
        # allowed only for a run that was handed files: the file bridge gives
        # the snippet one ephemeral directory and a confined `open` that can
        # reach nothing else. See `CodeSandbox.execute(files=...)`.
        self.allow_open = allow_open
    
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
            if name in BLOCKED_BUILTINS and not (name == 'open' and self.allow_open):
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
            '__dict__', '__init__', '__new__', '__getattr__', '__setattr__',
            '__import__', '__loader__', '__spec__',
            '__self__', '__func__', '__closure__', '__module__',
            '__name__', '__qualname__', '__annotations__',
            '__kwdefaults__', '__defaults__', '__getattribute__',
            # `type(()).mro()` reaches `object` without touching a dunder, so
            # the method name itself must be blocked, not just `__mro__`.
            'mro', 'mro_entries', '__subclasshook__', '__init_subclass__',
        }
        
        if node.attr in dangerous_attrs:
            self.errors.append(f"Access to '{node.attr}' is not allowed")
        
        self.generic_visit(node)


# ======================== File bridge (dev) ========================
# Mirrors `sandbox_service/executor.py`'s contract so the two engines stay
# interchangeable: inputs are written into an ephemeral cwd before the run,
# only declared outputs are read back, names are bare file names. Weaker
# isolation by construction (same process), dev fallback only.

import os as _os
import re as _re
import shutil as _shutil
import stat as _stat
import tempfile as _tempfile

#: Keep in step with the sidecar: 10 files each way, 16 MB each.
_INPROC_MAX_FILES = 10
_INPROC_MAX_FILE_BYTES = 16 * 1024 * 1024
_NAME_RE = _re.compile(r'^(?!\.)[^/\\\x00]{1,128}$')


class FileSpecError(ValueError):
    """An input or output name the dev engine refuses before running."""


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name) or name in ('.', '..'):
        raise FileSpecError(f'{name!r} is not a plain file name.')
    return name


def _confined_open(workdir: str):
    """An `open` that can reach only bare names inside `workdir`.

    No chdir (which would move the whole process), no absolute paths, no
    `..`, no hidden files: the snippet names a file and gets that file in its
    own directory. Anything else raises rather than escaping. `pandas` and
    `csv` both go through builtins `open`, so confining this one function
    confines them too.
    """
    real_open = open

    def _open(file, mode='r', *args, **kwargs):
        name = _os.fspath(file) if not isinstance(file, str) else file
        if not isinstance(name, str):
            raise FileNotFoundError(f'{file!r} is not a plain file name.')
        # Normalise separators the way `vfs.segments` does, then refuse
        # anything that is not one bare segment.
        candidate = name.replace('\\', '/')
        if '/' in candidate or candidate in ('.', '..') or not _NAME_RE.match(candidate):
            raise FileNotFoundError(
                f'{name!r} is not available here. Use a plain file name '
                f'from the inputs or outputs.'
            )
        return real_open(_os.path.join(workdir, candidate), mode, *args, **kwargs)

    return _open


def _collect_inproc(workdir: str, collect: list[str], inputs: set[str]) -> dict:
    out: dict[str, bytes] = {}
    missing: list[str] = []
    for name in collect:
        path = _os.path.join(workdir, name)
        try:
            info = _os.lstat(path)
        except FileNotFoundError:
            missing.append(name)
            continue
        if not _stat.S_ISREG(info.st_mode) or info.st_size > _INPROC_MAX_FILE_BYTES:
            missing.append(name)
            continue
        with open(path, 'rb') as handle:
            out[name] = handle.read(_INPROC_MAX_FILE_BYTES + 1)
    try:
        entries = _os.listdir(workdir)
    except OSError:
        entries = []
    unsaved = sorted(
        e for e in entries if e not in out and e not in inputs and e not in missing
    )
    return {"files_out": out, "missing": missing, "unsaved": unsaved[:20]}


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

        # An `import` statement compiles to a call to `__import__`, so leaving
        # it out of the builtins made *every* import fail -- including the
        # allow-listed ones (`from datetime import datetime` raised
        # "ImportError: __import__ not found"), which meant only code that
        # happened to import nothing ever ran. Found by the benchmark
        # (data-analysis suite). This hands back the same restricted proxies
        # `_create_safe_globals` exposes, and refuses everything else.
        safe['__import__'] = _restricted_import
        
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
    
    def validate(self, code: str, *, allow_open: bool = False) -> tuple[bool, list[str]]:
        """Validate code before execution."""
        return SafeCodeValidator(allow_open=allow_open).validate(code)
    
    def execute(
        self,
        code: str,
        context: dict | None = None,
        timeout: int | None = None,
        *,
        files: dict[str, bytes] | None = None,
        collect: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        """
        Execute code in sandbox.
        
        Args:
            code: Python code to execute
            context: Variables to make available
            timeout: Execution timeout (seconds)
            files: inputs written into the run's own directory (`{name: bytes}`)
            collect: output names to read back from that directory
            
        Returns:
            Dict with 'result', 'output', 'error' keys — plus `files_out`,
            `missing` and `unsaved` when `collect` is given.
        """
        import io
        from contextlib import redirect_stdout, redirect_stderr

        files = dict(files or {})
        collect = list(dict.fromkeys(collect or ()))
        wants_files = bool(files) or bool(collect)
        if len(files) > _INPROC_MAX_FILES or len(collect) > _INPROC_MAX_FILES:
            return {
                'success': False,
                'error': f'At most {_INPROC_MAX_FILES} input and {_INPROC_MAX_FILES} output files per run.',
                'result': None,
            }
        try:
            for name, data in files.items():
                _check_name(name)
                if len(data) > _INPROC_MAX_FILE_BYTES:
                    raise FileSpecError(f'{name} is over the file limit.')
            for name in collect:
                _check_name(name)
        except FileSpecError as exc:
            return {'success': False, 'error': str(exc), 'result': None}
        
        # Validate first (allow `open` only when files are in play).
        is_safe, errors = self.validate(code, allow_open=wants_files)
        if not is_safe:
            return {
                'success': False,
                'error': f"Code validation failed: {'; '.join(errors)}",
                'result': None,
            }

        workdir = _tempfile.mkdtemp(prefix="sbx-") if wants_files else None
        # Capture output
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        try:
            if workdir is not None:
                for name, data in files.items():
                    with open(_os.path.join(workdir, name), 'wb') as handle:
                        handle.write(data)

            # Prepare execution environment
            safe_globals = self._create_safe_globals(context)
            if workdir is not None:
                # The one filesystem the snippet may touch: its own directory.
                safe_globals['__builtins__'] = {
                    **self._safe_builtins, 'open': _confined_open(workdir),
                }
            safe_locals = {}

            class SandboxThread(threading.Thread):
                def __init__(self, code_obj, glbs, lcls):
                    super().__init__(daemon=True)
                    self.code_obj = code_obj
                    self.glbs = glbs
                    self.lcls = lcls
                    self.success = False
                    self.error = None
                    self.timed_out = False
                
                def run(self):
                    try:
                        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                            exec(self.code_obj, self.glbs, self.lcls)
                        self.success = True
                    except SystemExit:
                        # `_stop_thread` asked us to stop, so this is the
                        # interrupt working, not a fault. Swallowed quietly:
                        # the caller already knows it timed out, and letting it
                        # escape puts a traceback in the logs for every
                        # successful kill.
                        self.timed_out = True
                    except Exception as e:
                        self.error = e

            compiled_code = compile(code, '<sandbox>', 'exec')
            sandbox_thread = SandboxThread(compiled_code, safe_globals, safe_locals)
            
            limit = timeout or self.timeout
            sandbox_thread.start()
            sandbox_thread.join(timeout=limit)
            
            if sandbox_thread.is_alive():
                # `join` returning is not the code stopping. Before this, a
                # timed-out execution was reported as timed out and then kept
                # running for the life of the process — a `while True` burned a
                # core and grew its allocations behind an answer the model had
                # already been given. See `_stop_thread`.
                stopped = _stop_thread(sandbox_thread)
                note = '' if stopped else (
                    ' It could not be interrupted and is still running in the '
                    'background — do not re-run the same code.'
                )
                if not stopped:
                    logger.error(
                        'Sandbox thread %s ignored the interrupt and is still '
                        'running. This leaks a thread for the life of the '
                        'process.', sandbox_thread.ident,
                    )
                return {
                    'success': False,
                    'error': f"Execution Timeout: Code did not complete within {limit} seconds.{note}",
                    'result': None,
                    'output': stdout_capture.getvalue(),
                    'stderr': stderr_capture.getvalue(),
                }
            
            if not sandbox_thread.success and sandbox_thread.error:
                raise sandbox_thread.error
            
            # Get result (last expression or 'result' variable)
            result = safe_locals.get('result', safe_locals.get('output'))
            
            out = {
                'success': True,
                'result': result,
                'output': stdout_capture.getvalue(),
                'stderr': stderr_capture.getvalue(),
                'locals': {k: v for k, v in safe_locals.items() if not k.startswith('_')},
            }
            if collect and workdir is not None:
                out.update(_collect_inproc(workdir, collect, set(files)))
            return out
            
        except Exception as e:
            return {
                'success': False,
                'error': f"{type(e).__name__}: {str(e)}",
                'result': None,
                'output': stdout_capture.getvalue(),
                'stderr': stderr_capture.getvalue(),
            }
        finally:
            if workdir is not None:
                _shutil.rmtree(workdir, ignore_errors=True)


def _stop_thread(thread, grace: float = 1.0) -> bool:
    """Raise `SystemExit` inside `thread` and wait briefly for it to unwind.

    Python has no `Thread.kill`, and a daemon thread only dies with the
    process — so a sandbox execution that overran its timeout used to keep
    running for ever. `join(timeout=...)` *returns*; it does not stop anything.
    The caller had already told the model "Execution Timeout", so the runaway
    was invisible: a `while True: buf.append(...)` would quietly consume the
    box behind a turn that looked like it had failed cleanly.

    `PyThreadState_SetAsyncExc` is CPython's own mechanism for this. The
    exception is delivered at the next bytecode boundary, which covers the case
    that actually happens — a loop that will not end. It deliberately cannot
    interrupt a single long C-level call (`[0] * 10**10` allocates in one step,
    and either finishes or raises `MemoryError` by itself), so this reports
    whether the thread really died instead of assuming it did. The honest
    ceiling on this approach is why the in-process engine is a dev-only
    fallback: the sidecar container (`sandbox_service/`) kills the whole process
    group and caps memory at the kernel, which is the real fix.

    `SystemExit` rather than a custom exception: sandboxed code wrapped in a
    bare `except Exception` cannot swallow a `BaseException`.
    """
    import ctypes

    ident = thread.ident
    if ident is None:
        return True

    # Three guards, all closing the same hole: `ident` identifies a *slot*, not
    # a thread for ever. CPython recycles thread ids once a thread has finished,
    # so between the caller's `is_alive()` and this call the sandbox thread can
    # exit and its id be handed to something else -- and `SetAsyncExc` would
    # then deliver `SystemExit` into a stranger.
    #
    # This was not theoretical. Delivered into the *main* thread it starts
    # interpreter shutdown, which runs `threading._shutdown` and with it
    # `concurrent.futures.thread._python_exit`; that sets a module-global
    # `_shutdown` flag, after which every `ThreadPoolExecutor.submit` raises
    # `RuntimeError: cannot schedule new futures after interpreter shutdown`.
    # asgiref's `sync_to_async` runs on such a pool, so every ORM call from
    # then on fails -- while Daphne's loop survives and keeps accepting
    # requests. The observed result is a server that stays up and answers 500
    # to everything, including `/api/health/`, with no traceback at the point
    # of damage.
    #
    # Never the main thread, whatever the id says: no sandbox ever runs there,
    # so a match means the id was recycled, and the cost of being wrong is the
    # whole process.
    if ident == threading.main_thread().ident:
        logger.error(
            'Refusing to interrupt the main thread: sandbox thread id %s was '
            'recycled before the kill could be delivered.', ident,
        )
        return False

    # Finished on its own between the caller's check and here -- nothing to
    # kill, and raising now would hit whoever inherits the id next.
    if not thread.is_alive():
        return True

    # And the direct test: `threading._active` maps a live id to its Thread
    # object, so if the slot no longer holds *our* thread the id has already
    # been reused. Checked immediately before the call to keep the window as
    # small as CPython allows.
    active = getattr(threading, '_active', None)
    if isinstance(active, dict) and active.get(ident) is not thread:
        logger.warning(
            'Sandbox thread id %s no longer maps to the sandbox thread; '
            'skipping the interrupt rather than targeting another thread.', ident,
        )
        return not thread.is_alive()

    raised = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(ident), ctypes.py_object(SystemExit),
    )
    if raised == 0:
        return not thread.is_alive()   # already gone between the check and here
    if raised > 1:
        # Asked more than one thread to die: undo rather than take the process
        # down with us. Should be unreachable; a wrong id is worse than a leak.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)
        return False

    thread.join(timeout=grace)
    return not thread.is_alive()


# Global instance
_sandbox: CodeSandbox | None = None


def get_sandbox() -> CodeSandbox:
    """Get the global in-process code sandbox (the dev-only fallback engine)."""
    global _sandbox
    if _sandbox is None:
        _sandbox = CodeSandbox()
    return _sandbox


def safe_execute(
    code: str,
    context: dict | None = None,
    *,
    files: dict[str, bytes] | None = None,
    collect: tuple[str, ...] | list[str] = (),
) -> dict:
    """Convenience wrapper around the in-process engine.

    Production code should go through `sandbox.engine.arun_code`, which selects
    the hardened sidecar when configured. This stays for the in-process path and
    any synchronous caller.
    """
    return get_sandbox().execute(code, context, files=files, collect=collect)
