import os
import django
import sys
import json

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from credentials.models import Credential

def inspect_latest():
    try:
        cred = Credential.objects.latest('created_at')
        print(f"make: {cred.name}")
        print(f"Type: {cred.credential_type.name}")
        print(f"Encrypted Data (bytes): {len(cred.encrypted_data)}")
        
        try:
            data = cred.get_credential_data()
            print("Decrypted Data:", json.dumps(data, indent=2))
        except Exception as e:
            print(f"Decryption Error: {e}")
            
    except Credential.DoesNotExist:
        print("No credentials found.")

if __name__ == '__main__':
    inspect_latest()
