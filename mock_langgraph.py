import os

def create_mock():
    os.makedirs('langgraph/graph/state', exist_ok=True)
    
    with open('langgraph/__init__.py', 'w') as f:
        f.write('class StateGraph: pass\n')
        
    with open('langgraph/graph/__init__.py', 'w') as f:
        f.write('class StateGraph: pass\nEND = "END"\nSTART = "START"\n')
        
    with open('langgraph/graph/state.py', 'w') as f:
        f.write('class CompiledStateGraph: pass\n')

if __name__ == "__main__":
    create_mock()
