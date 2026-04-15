import os, subprocess  
apps = ['core', 'nodes', 'compiler', 'executor', 'orchestrator', 'credentials', 'inference', 'logs', 'streaming', 'templates', 'mcp_integration', 'skills', 'chat']  
for app in apps:  
    print(f'Dumping {app}...')  
    subprocess.run(['venv\\Scripts\\python', 'manage.py', 'dumpdata', app, '-o', f'{app}.json'], env=dict(os.environ, PYTHONUTF8='1'))  
    with open(f'{app}.json', 'r', encoding='utf-8') as f: data = f.read().replace('\\u0000', '').replace('\u0000', '')  
    with open(f'{app}.json', 'w', encoding='utf-8') as f: f.write(data) 
