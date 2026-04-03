import os
import json
import time
import tempfile
from typing import Any, Dict

class WasmCodeSandbox:
    """
    Secure WebAssembly-based Python Sandboxing.
    Gives explicit memory and CPU (fuel) limits.
    Requires python.wasm binaries.
    """
    def __init__(self, memory_limit_mb: int = 50, cpu_fuel: int = 200_000_000):
        self.memory_limit_mb = memory_limit_mb
        self.cpu_fuel = cpu_fuel
        
        base_dir = os.path.dirname(__file__)
        self.wasm_base = os.path.join(base_dir, "wasm", "Python-3.11.0-wasm32-wasi-16")
        self.wasm_file = os.path.join(self.wasm_base, "python.wasm")

    def execute(self, code: str, context: dict | None = None) -> Dict[str, Any]:
        """Execute Python code completely isolated in WebAssembly."""
        try:
            from wasmtime import Engine, Store, Module, Linker, WasiConfig, Config
        except ImportError:
            return {"success": False, "error": "wasmtime is not installed."}

        if not os.path.exists(self.wasm_file):
            return {"success": False, "error": f"WASM binary missing at {self.wasm_file}. Please download it."}

        start_time = time.time()
        
        # We need to bridge 'context' (like variables) into the sandbox via JSON
        # We'll inject a preamble into the user's code.
        preamble = ""
        if context and "input_json" in context:
            preamble = f"import json\ninput_data = json.loads('''{json.dumps(context['input_json'])}''')\n"

        full_code = preamble + code

        # Wasmtime setup
        config = Config()
        config.consume_fuel = True
        engine = Engine(config)
        store = Store(engine)
        store.set_fuel(self.cpu_fuel)
        store.set_limits(memory_size=self.memory_limit_mb * 1024 * 1024)

        wasi = WasiConfig()
        out_fd, out_path = tempfile.mkstemp()
        err_fd, err_path = tempfile.mkstemp()
        os.close(out_fd)
        os.close(err_fd)

        wasi.stdout_file = out_path
        wasi.stderr_file = err_path
        
        wasi.preopen_dir(self.wasm_base, "/")
        wasi.env = [
            ["PYTHONHOME", "/"],
            ["PYTHONPATH", "/lib/python3.11"]
        ]
        wasi.argv = ["python", "-c", full_code]

        store.set_wasi(wasi)
        
        success = False
        error_msg = ""
        try:
            linker = Linker(engine)
            linker.define_wasi()
            module = Module.from_file(engine, self.wasm_file)
            instance = linker.instantiate(store, module)
            
            _start = instance.exports(store)["_start"]
            _start(store)
            success = True
        except Exception as e:
            error_msg = str(e)

        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            stdout_str = f.read()
        with open(err_path, "r", encoding="utf-8", errors="replace") as f:
            stderr_str = f.read()

        os.remove(out_path)
        os.remove(err_path)
        
        # Format user error messages cleanly
        if stderr_str:
            success = False

        if "Trap" in error_msg:
            if "out of fuel" in error_msg.lower():
                error_msg = "Execution Timeout / Out of CPU Time"
            elif "memory" in error_msg.lower() or "out of bounds" in error_msg.lower():
                error_msg = "Out of Memory (Exceeded Sandbox Limit)"
            else:
                error_msg = "Sandbox Trap (Process Killed)"

        final_err = (error_msg + "\n" + stderr_str).strip()

        return {
            "success": success,
            "error": final_err,
            "result": None, # Complex objects don't return from WASM natively, rely on stdout
            "output": stdout_str,
            "stderr": stderr_str,
            "duration": time.time() - start_time
        }
