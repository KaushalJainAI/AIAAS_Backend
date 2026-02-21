
import subprocess
import sys

def run():
    cmd = [r"venv\Scripts\python.exe", "manage.py", "test", "orchestrator.tests_partial", "--no-input"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
    
    for line in process.stdout:
        print(line, end='')
    
    process.wait()
    sys.exit(process.returncode)

if __name__ == "__main__":
    run()
