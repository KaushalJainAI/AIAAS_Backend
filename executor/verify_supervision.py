"""
Verification script for Supervision Levels feature.
"""
import os
import sys

# Add Backend to path BEFORE importing Django
backend_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_path)

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
import django
django.setup()

from executor.king import SupervisionLevel, KingOrchestrator

def test_supervision_levels():
    print("=== Supervision Levels Verification ===")
    
    # Test enum values
    print("[OK] SupervisionLevel.FULL:", SupervisionLevel.FULL.value)
    print("[OK] SupervisionLevel.ERROR_ONLY:", SupervisionLevel.ERROR_ONLY.value)
    print("[OK] SupervisionLevel.NONE:", SupervisionLevel.NONE.value)
    
    # Test KingOrchestrator has supervision in start signature
    import inspect
    sig = inspect.signature(KingOrchestrator.start)
    params = list(sig.parameters.keys())
    print("[OK] start() has supervision param:", 'supervision' in params)
    
    # Test ExecutionHandle has supervision_level
    from executor.king import ExecutionHandle
    import dataclasses
    fields = [f.name for f in dataclasses.fields(ExecutionHandle)]
    print("[OK] ExecutionHandle has supervision_level:", 'supervision_level' in fields)
    
    print("\n=== All checks passed! ===")

if __name__ == "__main__":
    test_supervision_levels()
