
import os
import django
import json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from credentials.models import Credential, CredentialType

def inspect_latest_credential():
    print("--- Inspecting Latest Credential ---")
    cred = Credential.objects.last()
    if not cred:
        print("No credentials found.")
        return

    print(f"Credential ID: {cred.id}")
    print(f"Name: {cred.name}")
    print(f"Type: {cred.credential_type.name}")

    # Decrypt data
    try:
        data = cred.get_credential_data()
        print(f"Decrypted Data: {json.dumps(data, indent=2)}")
    except Exception as e:
        print(f"Error decrypting data: {e}")

    # Inspect Schema
    print("\n--- Inspecting Type Schema ---")
    schema = cred.credential_type.fields_schema
    print(f"Schema: {json.dumps(schema, indent=2)}")

    # Check for mismatch
    schema_keys = [f['name'] for f in schema]
    data_keys = list(data.keys())
    
    print("\n--- Analysis ---")
    print(f"Schema Keys: {schema_keys}")
    print(f"Data Keys: {data_keys}")
    
    missing_in_data = [k for k in schema_keys if k not in data_keys]
    extra_in_data = [k for k in data_keys if k not in schema_keys]
    
    if missing_in_data:
        print(f"WARNING: Fields defined in schema but missing in data: {missing_in_data}")
    else:
        print("All schema fields are present in data (or at least keys exist).")
        
    if extra_in_data:
        print(f"WARNING: Fields in data but NOT in schema (won't be shown in UI): {extra_in_data}")

if __name__ == "__main__":
    inspect_latest_credential()
