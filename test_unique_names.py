import os
import django
import sys

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from orchestrator.models import Workflow
from rest_framework.test import APIRequestFactory, force_authenticate
from orchestrator.views import workflow_list

User = get_user_model()

def test_unique_name_creation():
    print("Testing unique name creation...")
    
    # Create test user
    user, created = User.objects.get_or_create(username='testuser_unique_check')
    
    # Clean up existing workflows for this user
    Workflow.objects.filter(user=user).delete()
    
    factory = APIRequestFactory()
    
    # Create first workflow
    data1 = {'name': 'Unified Workflow'}
    request1 = factory.post('/api/orchestrator/workflows/', data1, format='json')
    force_authenticate(request1, user=user)
    response1 = workflow_list(request1)
    
    print(f"Workflow 1 created: {response1.data['name']} (Status: {response1.status_code})")
    assert response1.status_code == 201
    assert response1.data['name'] == 'Unified Workflow'
    
    # Create second workflow with SAME name
    data2 = {'name': 'Unified Workflow'}
    request2 = factory.post('/api/orchestrator/workflows/', data2, format='json')
    force_authenticate(request2, user=user)
    response2 = workflow_list(request2)
    
    print(f"Workflow 2 created: {response2.data['name']} (Status: {response2.status_code})")
    assert response2.status_code == 201
    assert response2.data['name'] == 'Unified Workflow (1)'
    
    # Create third workflow with SAME name
    data3 = {'name': 'Unified Workflow'}
    request3 = factory.post('/api/orchestrator/workflows/', data3, format='json')
    force_authenticate(request3, user=user)
    response3 = workflow_list(request3)
    
    print(f"Workflow 3 created: {response3.data['name']} (Status: {response3.status_code})")
    assert response3.status_code == 201
    assert response3.data['name'] == 'Unified Workflow (2)'
    
    print("SUCCESS: Unique name logic works!")

if __name__ == '__main__':
    try:
        test_unique_name_creation()
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
