import os
import sys
import subprocess
import traceback

def get_python_modules(start_dir):
    modules = []
    for root, dirs, files in os.walk(start_dir):
        # Skip certain directories
        if any(skip in root for skip in ['venv', '.git', '__pycache__', 'media', 'static', 'templates']):
            continue
            
        for file in files:
            if file.endswith('.py') and file != '__init__.py' and file != 'check_imports.py':
                # Convert path to module name
                relative_path = os.path.relpath(os.path.join(root, file), start_dir)
                base_name = os.path.splitext(relative_path)[0]
                module_name = base_name.replace(os.path.sep, '.')
                # Only include modules that look like they are part of a package (have an __init__.py in their path or are top-level)
                modules.append(module_name)
    return sorted(modules)

def check_module(module_name):
    """Try to import a module in a fresh subprocess."""
    script = f"""
import os
import django
import importlib
import sys

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
try:
    django.setup()
    importlib.import_module('{module_name}')
    sys.exit(0)
except Exception as e:
    import traceback
    print(traceback.format_exc())
    sys.exit(1)
"""
    # Use the same python executable
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stdout + result.stderr

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    modules = get_python_modules(base_dir)
    
    # Filter out script files in the root that aren't meant to be modules
    root_scripts = [f for f in os.listdir(base_dir) if f.endswith('.py') and f != '__init__.py']
    # We'll check everything for now as the user asked for "each file"
    
    print(f"Checking {len(modules)} modules for import errors using subprocesses...\n")
    
    failed_modules = []
    passed = 0
    
    for i, module in enumerate(modules):
        # print(f"[{i+1}/{len(modules)}] {module}...", end="\r")
        success, output = check_module(module)
        if success:
            passed += 1
        else:
            print(f"FAIL: {module}")
            failed_modules.append((module, output))

    print(f"\nSummary: {passed} passed, {len(failed_modules)} failed.")
    
    if failed_modules:
        print("\nErrors Found:")
        for module, error in failed_modules:
            print("-" * 40)
            print(f"Module: {module}")
            # print(error)
            # Only print the last few lines of the traceback to keep it clean
            lines = error.strip().splitlines()
            if len(lines) > 10:
                print("...")
                print("\n".join(lines[-10:]))
            else:
                print("\n".join(lines))
        return False
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
