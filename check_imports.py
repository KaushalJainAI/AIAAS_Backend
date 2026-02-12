"""
Import Checker — Verifies every Python module in the project can be imported.

Runs each import in a subprocess with a timeout so one bad module
can't hang the entire check.

Usage:
    python check_imports.py          # check all modules
    python check_imports.py core     # check only modules starting with 'core'
"""
import os
import sys
import subprocess
import time

# Directories to skip entirely
SKIP_DIRS = {
    'venv', '.git', '__pycache__', 'media', 'static',
    'templates', 'docs', 'migrations', '.gemini',
}

# Files to skip (not real importable modules)
SKIP_FILES = {
    'check_imports.py', 'extract_chat_id.py', 'populate_credentials.py',
    'manage.py', 'conftest.py',
}

# Per-subprocess timeout in seconds
TIMEOUT_SECONDS = 15


def get_python_modules(start_dir, prefix_filter=None):
    """Walk the project and find all importable .py modules."""
    modules = []
    for root, dirs, files in os.walk(start_dir):
        # Prune skipped directories in-place so os.walk doesn't descend
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            if not fname.endswith('.py') or fname == '__init__.py':
                continue
            if fname in SKIP_FILES:
                continue

            relative = os.path.relpath(os.path.join(root, fname), start_dir)
            module = os.path.splitext(relative)[0].replace(os.sep, '.')

            # Skip root-level scripts that aren't inside a package
            if '.' not in module:
                continue

            if prefix_filter and not module.startswith(prefix_filter):
                continue

            modules.append(module)
    return sorted(modules)


def check_module(module_name):
    """Import a module in a fresh subprocess with a timeout."""
    script = (
        "import os, django, importlib, sys;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','workflow_backend.settings');"
        "django.setup();"
        f"importlib.import_module('{module_name}')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {TIMEOUT_SECONDS}s"


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Optional filter: python check_imports.py core
    prefix = sys.argv[1] if len(sys.argv) > 1 else None

    modules = get_python_modules(base_dir, prefix_filter=prefix)
    total = len(modules)
    print(f"Checking {total} modules{f' (filter: {prefix}*)' if prefix else ''}...\n")

    failed = []
    passed = 0
    t0 = time.time()

    for i, module in enumerate(modules, 1):
        print(f"  [{i}/{total}] {module}...", end=" ", flush=True)
        ok, output = check_module(module)
        if ok:
            print("OK")
            passed += 1
        else:
            print("FAIL")
            failed.append((module, output))

    elapsed = time.time() - t0
    print(f"\n{'='*50}")
    print(f"Done in {elapsed:.1f}s — {passed} passed, {len(failed)} failed")

    if failed:
        print(f"\n{'='*50}")
        print("FAILURES:\n")
        for module, error in failed:
            print(f"--- {module} ---")
            # Show last 8 lines of traceback to keep output readable
            lines = error.splitlines()
            if len(lines) > 8:
                print("  ...")
                for line in lines[-8:]:
                    print(f"  {line}")
            else:
                for line in lines:
                    print(f"  {line}")
            print()
        return False

    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
