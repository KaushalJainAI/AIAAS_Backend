# Secure Code Execution Sandbox

This document outlines the dual-engine architecture for the code execution environment utilized by the AIAAS backend. It guarantees that untrusted Python code can be executed safely, quickly, and with strictly enforced resource limitations.

## The Dual-Engine Architecture

Because the platform demands both **blazing fast speeds** (for iterating workflow nodes thousands of times) and **hard RAM/CPU limits** (for executing potentially untrusted single-shot logic from an LLM), the system employs two interchangeable sandboxing engines. Both engines are unified under a single entry point: `executor.safe_execution.safe_execute()`.

### 1. In-Process Validation Engine (`engine="in_process"`)
* **Speed:** ~0.001s per execution
* **Limits:** Syntax restrictions, basic script timeouts, explicit module blocklists.

**How It Works:** Expected as the foundational workflow driver. It uses Python's `ast` (Abstract Syntax Tree) module to aggressively parse and filter user code before passing it to Python's internal `exec()`. It strips out dangerous methods (`eval`, OS imports, subprocesses) and aggressively filters the `__builtins__` list. Because it runs inside the main server's process memory, it is blisteringly fast, but lacks an absolute RAM cutoff switch.

### 2. WebAssembly (WASI) Engine (`engine="wasm"`)
* **Speed:** ~50ms - 150ms per execution
* **Limits:** Absolute Byte-Level Memory Limits (e.g. exactly 50MB) and Exact CPU Cycles (e.g. 200,000,000 Instructions).

**How It Works:** The heavy-duty out-of-process engine. It spins loops code entirely outside of the host operating system by executing a pre-compiled `python.wasm` binary inside the bounds of the `wasmtime` runtime. Since the VM operates entirely via the WASI (WebAssembly System Interface), the scripts literally have zero capability of accessing the true host file system, environment variables, or network sockets without being explicitly piped in. If the script hits an infinite loop (`while True`) or attempts a massive memory spike, it violently runs out of "CPU Fuel" or Memory Pages and safely traps the execution without bogging down the main server.

---

## Technical File Breakdown

Here is a summary of the specific files added and modified to support this dual capability:

### 1. `executor/wasm/Python-3.11.0-wasm32-wasi-16/python.wasm` (NEW BUNDLE)
**The WebAssembly Sandbox Core.**
This is the pre-compiled CPython WebAssembly execution binary downloaded from the core maintainers. It serves as the literal core engine. When the `wasmtime` library instantiates, it boots this binary and injects standard python strings into it. It relies on the local `/lib` directory (downloaded alongside it) for standard Python libraries (like `math` or `json`).

### 2. `executor/wasm_sandbox.py` (NEW FILE)
**The WebAssembly Interpreter Bridge.**
This file defines the `WasmCodeSandbox` class. It manages all the messy C-level interactions with the WebAssembly WASI interface implicitly.
* **Responsibilities:** It translates your explicit Python strings into byte paths, dynamically generates temporary logs for capturing the WASM sandboxes `stdout/stderr` streams, strictly locks down the RAM using `store.set_limits(memory_size=50MB)`, and restricts CPU cycles via `store.set_fuel(...)`. It translates WASM Trap errors into human-readable LLM errors (e.g., "Out of Memory").

### 3. `executor/safe_execution.py` (MODIFIED)
**The Unified Route and Primary Security Gatekeeper.**
* **Responsibilities:** Exposes the globally available `safe_execute(code, engine="in_process" | "wasm")` convenience wrapper. Retains all the AST parsing and whitelists (like `ALLOW_MODULES`) used by the in-process execution. 
* **Design Purpose:** Ensures that regardless of what backend feature you use, as long as it imports `safe_execute`, it will automatically inherit the global security policy.

### 4. `chat/tools.py` (MODIFIED)
**The LLM Tool Interface.**
* **Responsibilities:** Exposes the new `execute_python_code` tool JSON schema to the Chat Agent. 
* **Design Purpose:** We strategically expose the `engine` parameter to the LLM (`"in_process"` or `"wasm"`). This allows the Advanced Agent to make an autonomous judgment: if the script is basic logic, it can request the fast in-process engine. If it is uncertain or dealing with a heavy recursive request, it enforces the WebAssembly CPU/RAM locks.

### 5. `nodes/handlers/core_nodes.py` (MODIFIED)
**The Workflow Code Pipeline.**
* **Responsibilities:** Modifies the standard `CodeNode` logic to utilize `safe_execution.py`.
* **Design Purpose:** The node previously used a hard-coded sandbox dictionary that blocked powerful libraries like `re` (Regex) and `itertools` natively. It now correctly taps into the global sandbox environment, ensuring workflows have standardized and highly secure access to core python transform modules.

---

## Modifying Server Engine Limits

If you ever identify a need to tune the resource limits a WASM execution relies on (for example, parsing enormous JSON files in memory):
1. Navigate to `executor/wasm_sandbox.py`.
2. Locate the constructor method: `__init__(self, memory_limit_mb: int = 50, cpu_fuel: int = 200_000_000)`.
3. Adjust the megabyte assignment integer and reboot the backend.
