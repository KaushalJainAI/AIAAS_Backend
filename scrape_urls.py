import os
import sys
import django
from django.urls import get_resolver, URLPattern, URLResolver

def list_urls(patterns, prefix=''):
    urls = []
    for entry in patterns:
        if isinstance(entry, URLPattern):
            urls.append(prefix + str(entry.pattern))
        elif isinstance(entry, URLResolver):
            urls.extend(list_urls(entry.url_patterns, prefix + str(entry.pattern)))
    return urls

if __name__ == "__main__":
    # Add the current directory to sys.path
    sys.path.append(os.getcwd())
    
    # Set up Django environment
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
    try:
        django.setup()
        
        resolver = get_resolver()
        all_urls = list_urls(resolver.url_patterns)
        
        print("\nRegistered Endpoints:")
        print("=====================")
        for url in sorted(set(all_urls)):
            print(f"- /{url}")
    except Exception as e:
        print(f"Error setting up Django: {e}")
        print("\nFallback: Searching urls.py files manually...")
        # Fallback to manual parsing if django.setup() fails
        import re
        for root, dirs, files in os.walk('.'):
            if 'urls.py' in files:
                path = os.path.join(root, 'urls.py')
                print(f"\nIn {path}:")
                with open(path, 'r') as f:
                    content = f.read()
                    patterns = re.findall(r"path\(['\"]([^'\"]*)['\"]", content)
                    for p in patterns:
                        print(f"  - {p}")
