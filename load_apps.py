import os, subprocess  
apps = ['core', 'nodes', 'compiler', 'executor', 'orchestrator', 'credentials', 'inference', 'logs', 'streaming', 'templates', 'mcp_integration', 'skills', 'chat']  
for app in apps:  
    print(f'Loading {app}...')  
    subprocess.run(['venv\\Scripts\\python', 'manage.py', 'loaddata', f'{app}.json'], env=dict(os.environ, PYTHONUTF8='1')) 
