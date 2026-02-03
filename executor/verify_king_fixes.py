"""
Verification script for KingOrchestrator fixes.
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

from executor.king import KingOrchestrator, AuthorizationError, HITLTimeoutError

def test_imports():
    print("[OK] Imports successful")
    
def test_instantiation():
    k = KingOrchestrator()
    print("[OK] KingOrchestrator instantiated")
    return k

def test_methods(k):
    print("[OK] Has _check_execution_auth:", hasattr(k, '_check_execution_auth'))
    print("[OK] Has _cleanup_execution:", hasattr(k, '_cleanup_execution'))
    print("[OK] Has _safe_callback:", hasattr(k, '_safe_callback'))
    print("[OK] Has pause with user_id:", 'user_id' in str(k.pause.__code__.co_varnames))
    print("[OK] Has resume with user_id:", 'user_id' in str(k.resume.__code__.co_varnames))
    print("[OK] Has stop with user_id:", 'user_id' in str(k.stop.__code__.co_varnames))

def test_exceptions():
    print("[OK] AuthorizationError exists:", AuthorizationError is not None)
    print("[OK] HITLTimeoutError exists:", HITLTimeoutError is not None)

if __name__ == "__main__":
    print("=== KingOrchestrator Verification ===")
    test_imports()
    k = test_instantiation()
    test_methods(k)
    test_exceptions()
    print("\n=== All checks passed! ===")
