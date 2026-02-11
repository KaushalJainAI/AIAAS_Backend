"""
Export Standalone Workflow

Django management command that exports a workflow as a self-contained
Flask application that can run independently without Django.

Usage:
    python manage.py export_standalone <workflow_id> [--output-dir ./exports] [--zip]
"""
import os
import re
import json
import shutil
import zipfile
import textwrap
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

# ── Node type → source file mapping ──
NODE_TYPE_TO_FILE = {
    # core_nodes.py
    'code': 'core_nodes.py',
    'set': 'core_nodes.py',
    'if': 'core_nodes.py',
    # logic_nodes.py
    'loop': 'logic_nodes.py',
    'split_in_batches': 'logic_nodes.py',
    # subworkflow_node.py
    'subworkflow': 'subworkflow_node.py',
    # integration_nodes.py
    'gmail': 'integration_nodes.py',
    'slack': 'integration_nodes.py',
    'google_sheets': 'integration_nodes.py',
    'discord': 'integration_nodes.py',
    'notion': 'integration_nodes.py',
    'airtable': 'integration_nodes.py',
    'telegram': 'integration_nodes.py',
    'trello': 'integration_nodes.py',
    'github': 'integration_nodes.py',
    'http_request': 'integration_nodes.py',
    # llm_nodes.py
    'openai': 'llm_nodes.py',
    'gemini': 'llm_nodes.py',
    'ollama': 'llm_nodes.py',
    'perplexity': 'llm_nodes.py',
    'openrouter': 'llm_nodes.py',
    # langchain_nodes.py
    'langchain_tool': 'langchain_nodes.py',
    # triggers.py
    'manual_trigger': 'triggers.py',
    'webhook_trigger': 'triggers.py',
    'schedule_trigger': 'triggers.py',
    'email_trigger': 'triggers.py',
    'form_trigger': 'triggers.py',
    'slack_trigger': 'triggers.py',
    'google_sheets_trigger': 'triggers.py',
    'github_trigger': 'triggers.py',
    'discord_trigger': 'triggers.py',
    'telegram_trigger': 'triggers.py',
    'rss_feed_trigger': 'triggers.py',
    'file_trigger': 'triggers.py',
    'sqs_trigger': 'triggers.py',
}

# ── Node type → class name mapping (for registry generation) ──
NODE_TYPE_TO_CLASS = {
    'code': ('core_nodes', 'CodeNode'),
    'set': ('core_nodes', 'SetNode'),
    'if': ('core_nodes', 'IfNode'),
    'loop': ('logic_nodes', 'LoopNode'),
    'split_in_batches': ('logic_nodes', 'SplitInBatchesNode'),
    'subworkflow': ('subworkflow_node', 'SubworkflowNodeHandler'),
    'gmail': ('integration_nodes', 'GmailNode'),
    'slack': ('integration_nodes', 'SlackNode'),
    'google_sheets': ('integration_nodes', 'GoogleSheetsNode'),
    'discord': ('integration_nodes', 'DiscordNode'),
    'notion': ('integration_nodes', 'NotionNode'),
    'airtable': ('integration_nodes', 'AirtableNode'),
    'telegram': ('integration_nodes', 'TelegramNode'),
    'trello': ('integration_nodes', 'TrelloNode'),
    'github': ('integration_nodes', 'GitHubNode'),
    'http_request': ('integration_nodes', 'HTTPRequestNode'),
    'openai': ('llm_nodes', 'OpenAINode'),
    'gemini': ('llm_nodes', 'GeminiNode'),
    'ollama': ('llm_nodes', 'OllamaNode'),
    'perplexity': ('llm_nodes', 'PerplexityNode'),
    'openrouter': ('llm_nodes', 'OpenRouterNode'),
    'langchain_tool': ('langchain_nodes', 'LangChainToolNode'),
    'manual_trigger': ('triggers', 'ManualTriggerNode'),
    'webhook_trigger': ('triggers', 'WebhookTriggerNode'),
    'schedule_trigger': ('triggers', 'ScheduleTriggerNode'),
    'email_trigger': ('triggers', 'EmailTriggerNode'),
    'form_trigger': ('triggers', 'FormTriggerNode'),
    'slack_trigger': ('triggers', 'SlackTriggerNode'),
    'google_sheets_trigger': ('triggers', 'GoogleSheetsTriggerNode'),
    'github_trigger': ('triggers', 'GitHubTriggerNode'),
    'discord_trigger': ('triggers', 'DiscordTriggerNode'),
    'telegram_trigger': ('triggers', 'TelegramTriggerNode'),
    'rss_feed_trigger': ('triggers', 'RssFeedTriggerNode'),
    'file_trigger': ('triggers', 'FileTriggerNode'),
    'sqs_trigger': ('triggers', 'SQSTriggerNode'),
}

# ── Service identifier → env var name mapping ──
SERVICE_TO_ENV_KEY = {
    'openai': 'OPENAI_API_KEY',
    'gemini': 'GEMINI_API_KEY',
    'ollama': 'OLLAMA_BASE_URL',
    'perplexity': 'PERPLEXITY_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'gmail': 'GMAIL_CREDENTIALS',
    'slack': 'SLACK_BOT_TOKEN',
    'discord': 'DISCORD_BOT_TOKEN',
    'telegram': 'TELEGRAM_BOT_TOKEN',
    'github': 'GITHUB_TOKEN',
    'notion': 'NOTION_API_KEY',
    'airtable': 'AIRTABLE_API_KEY',
    'google_sheets': 'GOOGLE_SHEETS_CREDENTIALS',
    'trello': 'TRELLO_API_KEY',
}


class Command(BaseCommand):
    help = 'Export a workflow as a standalone Flask application'

    def add_arguments(self, parser):
        parser.add_argument('workflow_id', type=int, help='ID of the workflow to export')
        parser.add_argument(
            '--output-dir', type=str, default='./exports',
            help='Directory to write the export to (default: ./exports)'
        )
        parser.add_argument(
            '--zip', action='store_true', default=False,
            help='Also create a ZIP archive'
        )

    def handle(self, *args, **options):
        workflow_id = options['workflow_id']
        output_base = Path(options['output_dir'])
        create_zip = options['zip']

        # 1. Fetch workflow
        from orchestrator.models import Workflow
        try:
            workflow = Workflow.objects.get(id=workflow_id)
        except Workflow.DoesNotExist:
            raise CommandError(f"Workflow {workflow_id} does not exist")

        self.stdout.write(f"Exporting workflow: {workflow.name} (ID: {workflow_id})")

        # 2. Determine used node types
        used_types = self._get_used_node_types(workflow.nodes)
        self.stdout.write(f"  Node types used: {', '.join(sorted(used_types))}")

        # 3. Determine required handler files
        required_files = set()
        for nt in used_types:
            if nt in NODE_TYPE_TO_FILE:
                required_files.add(NODE_TYPE_TO_FILE[nt])
        self.stdout.write(f"  Handler files needed: {', '.join(sorted(required_files))}")

        # 4. Fetch & decrypt credentials
        credentials, cred_env_map = self._get_credentials(workflow)

        # 5. Generate output
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', workflow.name.lower()).strip('_') or 'workflow'
        output_dir = output_base / safe_name
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

        self._generate_output(
            output_dir=output_dir,
            workflow=workflow,
            used_types=used_types,
            required_files=required_files,
            credentials=credentials,
            cred_env_map=cred_env_map,
        )

        self.stdout.write(self.style.SUCCESS(f"\n✅ Exported to: {output_dir.resolve()}"))

        # 6. Optional ZIP
        if create_zip:
            zip_path = output_base / f"{safe_name}.zip"
            self._create_zip(output_dir, zip_path)
            self.stdout.write(self.style.SUCCESS(f"📦 ZIP created: {zip_path.resolve()}"))

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _get_used_node_types(self, nodes: list) -> set:
        """Extract unique node types from workflow nodes."""
        types = set()
        for node in nodes:
            nt = (
                node.get('nodeType') or
                node.get('data', {}).get('nodeType') or
                node.get('type', '')
            )
            if nt:
                types.add(nt)
        return types

    def _get_credentials(self, workflow):
        """Fetch and decrypt credentials used by the workflow."""
        from credentials.models import Credential

        credentials = {}      # {service_or_id: decrypted_data}
        cred_env_map = {}      # {service_or_id: ENV_VAR_NAME}

        # Collect credential IDs referenced in node configs
        cred_ids = set()
        for node in workflow.nodes:
            config = node.get('data', {}).get('config', {})
            cred_ref = config.get('credential')
            if cred_ref:
                cred_ids.add(str(cred_ref))

        if not cred_ids:
            return credentials, cred_env_map

        # Fetch from DB
        user_creds = Credential.objects.filter(
            user=workflow.user,
            is_active=True
        ).select_related('credential_type')

        for cred in user_creds:
            cred_id_str = str(cred.id)
            # Check if this credential is used by the workflow
            service_id = cred.credential_type.service_identifier if cred.credential_type else ''

            is_used = (
                cred_id_str in cred_ids or
                f"cred_{cred.id}" in cred_ids or
                service_id in cred_ids
            )
            if not is_used:
                continue

            try:
                data = cred.get_credential_data()

                # Map by ID
                credentials[cred_id_str] = data
                credentials[f"cred_{cred.id}"] = data

                # Map by service identifier
                if service_id:
                    credentials[service_id] = data

                # Determine env var name
                env_key = SERVICE_TO_ENV_KEY.get(service_id, f"CRED_{service_id.upper()}" if service_id else f"CRED_{cred.id}")
                cred_env_map[cred_id_str] = env_key
                if service_id:
                    cred_env_map[service_id] = env_key

            except Exception as e:
                self.stderr.write(f"  ⚠ Failed to decrypt credential {cred.id}: {e}")

        return credentials, cred_env_map

    def _generate_output(self, output_dir, workflow, used_types, required_files, credentials, cred_env_map):
        """Generate the full standalone app folder."""
        backend_dir = Path(__file__).resolve().parent.parent.parent.parent  # Backend/

        # Create subdirectories
        runtime_dir = output_dir / 'runtime'
        nodes_dir = output_dir / 'nodes'
        runtime_dir.mkdir()
        nodes_dir.mkdir()

        # ── workflow.json ──
        workflow_data = {
            'nodes': workflow.nodes,
            'edges': workflow.edges,
            'settings': workflow.workflow_settings or {},
        }
        (output_dir / 'workflow.json').write_text(
            json.dumps(workflow_data, indent=2), encoding='utf-8'
        )

        # ── .env (decrypted credentials) ──
        env_lines = ['# Auto-generated credentials for standalone workflow\n']
        env_example_lines = ['# Required credentials for this workflow\n']
        for key, data in credentials.items():
            env_key = cred_env_map.get(key)
            if not env_key:
                continue
            # Serialize credential data
            if isinstance(data, dict):
                # For dict credentials, find the "api_key" or serialize as JSON
                api_key = data.get('api_key') or data.get('token') or data.get('access_token')
                if api_key:
                    value = str(api_key)
                else:
                    value = json.dumps(data)
            else:
                value = str(data)

            if f"{env_key}=" not in ''.join(env_lines):  # Avoid duplicates
                env_lines.append(f'{env_key}={value}\n')
                env_example_lines.append(f'{env_key}=<your-value-here>\n')

        (output_dir / '.env').write_text(''.join(env_lines), encoding='utf-8')
        (output_dir / '.env.example').write_text(''.join(env_example_lines), encoding='utf-8')

        # ── runtime/__init__.py ──
        (runtime_dir / '__init__.py').write_text('', encoding='utf-8')

        # ── runtime/utils.py (copy as-is) ──
        shutil.copy2(backend_dir / 'compiler' / 'utils.py', runtime_dir / 'utils.py')

        # ── runtime/schemas.py (patched imports) ──
        self._copy_and_patch(
            backend_dir / 'compiler' / 'schemas.py',
            runtime_dir / 'schemas.py',
            []  # schemas.py has no Django imports
        )

        # ── runtime/validators.py (patched) ──
        validators_src = (backend_dir / 'compiler' / 'validators.py').read_text(encoding='utf-8')
        validators_src = validators_src.replace(
            'from .schemas import CompileError',
            'from runtime.schemas import CompileError'
        )
        validators_src = validators_src.replace(
            'from .utils import get_node_type',
            'from runtime.utils import get_node_type'
        )
        validators_src = validators_src.replace(
            'from nodes.handlers.registry import get_registry',
            'from nodes.registry import get_registry'
        )
        # Remove the validate_nesting_depth function (uses Django ORM)
        validators_src = re.sub(
            r'\ndef validate_nesting_depth\(.*?\n(?=\ndef |\nNODE_OUTPUT_TYPES|\Z)',
            '\n',
            validators_src,
            flags=re.DOTALL
        )
        (runtime_dir / 'validators.py').write_text(validators_src, encoding='utf-8')

        # ── runtime/interface_stubs.py ──
        shutil.copy2(backend_dir / 'orchestrator' / 'interface.py', runtime_dir / 'interface_stubs.py')

        # ── runtime/logger.py (console-based stub) ──
        (runtime_dir / 'logger.py').write_text(LOGGER_STUB, encoding='utf-8')

        # ── runtime/compiler.py (patched) ──
        compiler_src = (backend_dir / 'compiler' / 'compiler.py').read_text(encoding='utf-8')
        compiler_src = compiler_src.replace(
            'from .schemas import (\n    NodeExecutionPlan, # Keeping struct for internal use if needed, or we can use dicts\n    ExecutionContext,\n)',
            'from runtime.schemas import ExecutionContext'
        )
        # Handle Windows line endings variant
        compiler_src = compiler_src.replace(
            'from .schemas import (\r\n    NodeExecutionPlan, # Keeping struct for internal use if needed, or we can use dicts\r\n    ExecutionContext,\r\n)',
            'from runtime.schemas import ExecutionContext'
        )
        compiler_src = compiler_src.replace(
            'from .utils import get_node_type',
            'from runtime.utils import get_node_type'
        )
        compiler_src = compiler_src.replace(
            'from .validators import (\n    validate_dag,\n    validate_credentials,\n    validate_node_configs,\n    validate_type_compatibility,\n    topological_sort,\n)',
            'from runtime.validators import (\n    validate_dag,\n    validate_credentials,\n    validate_node_configs,\n    validate_type_compatibility,\n    topological_sort,\n)'
        )
        compiler_src = compiler_src.replace(
            'from .validators import (\r\n    validate_dag,\r\n    validate_credentials,\r\n    validate_node_configs,\r\n    validate_type_compatibility,\r\n    topological_sort,\r\n)',
            'from runtime.validators import (\n    validate_dag,\n    validate_credentials,\n    validate_node_configs,\n    validate_type_compatibility,\n    topological_sort,\n)'
        )
        compiler_src = compiler_src.replace(
            'from nodes.handlers.registry import get_registry',
            'from nodes.registry import get_registry'
        )
        compiler_src = compiler_src.replace(
            'from logs.logger import get_execution_logger',
            'from runtime.logger import get_execution_logger'
        )
        compiler_src = compiler_src.replace(
            'from orchestrator.interface import AbortDecision, PauseDecision',
            'from runtime.interface_stubs import AbortDecision, PauseDecision'
        )
        compiler_src = compiler_src.replace(
            'from orchestrator.interface import SupervisionLevel',
            'from runtime.interface_stubs import SupervisionLevel'
        )
        (runtime_dir / 'compiler.py').write_text(compiler_src, encoding='utf-8')

        # ── nodes/__init__.py ──
        (nodes_dir / '__init__.py').write_text('', encoding='utf-8')

        # ── nodes/base.py (copy, patch TYPE_CHECKING import) ──
        base_src = (backend_dir / 'nodes' / 'handlers' / 'base.py').read_text(encoding='utf-8')
        base_src = base_src.replace(
            'from compiler.schemas import ExecutionContext',
            'from runtime.schemas import ExecutionContext'
        )
        (nodes_dir / 'base.py').write_text(base_src, encoding='utf-8')

        # ── Copy only required node handler files ──
        for handler_file in required_files:
            src = backend_dir / 'nodes' / 'handlers' / handler_file
            if src.exists():
                handler_src = src.read_text(encoding='utf-8')
                # Patch imports
                handler_src = handler_src.replace('from .base import', 'from nodes.base import')
                handler_src = handler_src.replace('from nodes.handlers.base import', 'from nodes.base import')
                handler_src = handler_src.replace('from compiler.schemas import', 'from runtime.schemas import')
                (nodes_dir / handler_file).write_text(handler_src, encoding='utf-8')
            else:
                self.stderr.write(f"  ⚠ Handler file not found: {handler_file}")

        # ── nodes/registry.py (generated, only used types) ──
        registry_src = self._generate_registry(used_types)
        (nodes_dir / 'registry.py').write_text(registry_src, encoding='utf-8')

        # ── app.py ──
        (output_dir / 'app.py').write_text(
            self._generate_app_py(credentials, cred_env_map), encoding='utf-8'
        )

        # ── requirements.txt ──
        (output_dir / 'requirements.txt').write_text(REQUIREMENTS_TXT, encoding='utf-8')

        # ── Dockerfile ──
        (output_dir / 'Dockerfile').write_text(DOCKERFILE, encoding='utf-8')

        # ── README.md ──
        (output_dir / 'README.md').write_text(
            self._generate_readme(workflow.name, cred_env_map), encoding='utf-8'
        )

    def _copy_and_patch(self, src: Path, dest: Path, replacements: list):
        """Copy a file with import replacements."""
        content = src.read_text(encoding='utf-8')
        for old, new in replacements:
            content = content.replace(old, new)
        dest.write_text(content, encoding='utf-8')

    def _generate_registry(self, used_types: set) -> str:
        """Generate a minimal registry that only registers used node types."""
        # Group imports by module
        imports_by_module = {}
        register_lines = []

        for nt in sorted(used_types):
            if nt not in NODE_TYPE_TO_CLASS:
                continue
            module, class_name = NODE_TYPE_TO_CLASS[nt]
            imports_by_module.setdefault(module, []).append(class_name)
            register_lines.append(f"    registry.register({class_name})")

        import_lines = []
        for module, classes in sorted(imports_by_module.items()):
            class_list = ', '.join(sorted(classes))
            import_lines.append(f"from nodes.{module} import {class_list}")

        return textwrap.dedent(f'''\
"""
Auto-generated Node Registry (standalone export)
Only registers node types used by this workflow.
"""
from typing import Type
from nodes.base import BaseNodeHandler, NodeSchema

{chr(10).join(import_lines)}


class NodeRegistry:
    """Singleton registry for node handlers."""
    _instance = None
    _handlers = {{}}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, handler_class):
        self._handlers[handler_class.node_type] = handler_class

    def get_handler(self, node_type):
        if node_type not in self._handlers:
            raise KeyError(f"Unknown node type: {{node_type}}")
        return self._handlers[node_type]()

    def get_handler_class(self, node_type):
        if node_type not in self._handlers:
            raise KeyError(f"Unknown node type: {{node_type}}")
        return self._handlers[node_type]

    def has_handler(self, node_type):
        return node_type in self._handlers

    def get_all_schemas(self):
        return [cls().get_schema().model_dump(by_alias=True) for cls in self._handlers.values()]

    def get_node_types(self):
        return list(self._handlers.keys())

    def clear(self):
        self._handlers.clear()

    def __len__(self):
        return len(self._handlers)

    def __contains__(self, node_type):
        return node_type in self._handlers


def get_registry():
    """Get the global NodeRegistry instance with used nodes registered."""
    registry = NodeRegistry.get_instance()
    if not registry._handlers:
{chr(10).join("        " + line.strip() for line in register_lines)}
    return registry
''')

    def _generate_app_py(self, credentials: dict, cred_env_map: dict) -> str:
        """Generate the Flask app.py."""
        # Build credential loading logic
        env_loads = []
        seen_keys = set()
        for service_key, env_var in cred_env_map.items():
            if env_var in seen_keys:
                continue
            seen_keys.add(env_var)
            env_loads.append(
                f'    val = os.environ.get("{env_var}", "")\n'
                f'    if val:\n'
                f'        credentials["{service_key}"] = {{"api_key": val}} if not val.startswith("{{") else json.loads(val)'
            )

        cred_loading = '\n'.join(env_loads) if env_loads else '    pass  # No credentials needed'

        return textwrap.dedent(f'''\
"""
Standalone Workflow Runner
Auto-generated by AIAAS export. Run with: python app.py
"""
import os
import sys
import json
import asyncio
import logging
from uuid import uuid4
from pathlib import Path

# Load environment variables from .env
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from runtime.compiler import WorkflowCompiler, WorkflowCompilationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("standalone")

app = Flask(__name__)

# Load workflow definition
WORKFLOW_PATH = Path(__file__).parent / "workflow.json"
with open(WORKFLOW_PATH, "r") as f:
    WORKFLOW_DATA = json.load(f)


def load_credentials() -> dict:
    """Load credentials from environment variables."""
    credentials = {{}}
{cred_loading}
    return credentials


@app.route("/health", methods=["GET"])
def health():
    return jsonify({{"status": "ok", "workflow_nodes": len(WORKFLOW_DATA.get("nodes", []))}})


@app.route("/run", methods=["POST"])
def run_workflow():
    """Execute the workflow with optional input data."""
    input_data = request.get_json(silent=True) or {{}}
    credentials = load_credentials()

    try:
        # Compile
        used_creds = set(credentials.keys())
        compiler = WorkflowCompiler(WORKFLOW_DATA, user=None, user_credentials=used_creds)
        graph = compiler.compile(orchestrator=None, supervision_level=None)

        # Prepare initial state
        execution_id = str(uuid4())
        initial_state = {{
            "execution_id": execution_id,
            "user_id": 0,
            "workflow_id": 0,
            "current_node": "",
            "node_outputs": {{"_input_global": input_data}},
            "variables": {{}},
            "credentials": credentials,
            "loop_stats": {{}},
            "error": None,
            "status": "running",
            "nesting_depth": 0,
            "workflow_chain": [],
            "parent_execution_id": None,
            "timeout_budget_ms": None,
        }}

        # Execute
        final_state = asyncio.run(graph.ainvoke(initial_state))

        status = final_state.get("status", "completed")
        if status == "running":
            status = "completed"

        return jsonify({{
            "success": status != "failed",
            "status": status,
            "execution_id": execution_id,
            "outputs": final_state.get("node_outputs", {{}}),
            "error": final_state.get("error"),
        }})

    except WorkflowCompilationError as e:
        return jsonify({{
            "success": False,
            "status": "compilation_error",
            "error": str(e),
        }}), 400
    except Exception as e:
        logger.exception("Workflow execution failed")
        return jsonify({{
            "success": False,
            "status": "error",
            "error": str(e),
        }}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting standalone workflow on port {{port}}")
    app.run(host="0.0.0.0", port=port, debug=False)
''')

    def _generate_readme(self, workflow_name: str, cred_env_map: dict) -> str:
        """Generate README.md for the exported workflow."""
        env_vars_section = ""
        if cred_env_map:
            seen = set()
            lines = []
            for _, env_var in cred_env_map.items():
                if env_var not in seen:
                    lines.append(f"- `{env_var}`")
                    seen.add(env_var)
            env_vars_section = "## Required Environment Variables\n\n" + "\n".join(lines) + "\n"

        return textwrap.dedent(f"""\
# {workflow_name} — Standalone Workflow

This is a self-contained workflow exported from AIAAS. It runs as an independent
Flask application with zero Django dependencies.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up credentials (edit .env or set environment variables)
#    The .env file has been pre-filled with your credentials.
#    ⚠️  Keep this file secure! It contains decrypted API keys.

# 3. Run
python app.py
```

The server starts on `http://localhost:5000`.

## API Endpoints

### `GET /health`
Health check. Returns workflow node count.

### `POST /run`
Execute the workflow.

**Request body** (optional JSON):
```json
{{"input_key": "input_value"}}
```

**Response:**
```json
{{
  "success": true,
  "status": "completed",
  "execution_id": "...",
  "outputs": {{}},
  "error": null
}}
```

{env_vars_section}
## Docker

```bash
docker build -t {workflow_name.lower().replace(' ', '-')} .
docker run -p 5000:5000 --env-file .env {workflow_name.lower().replace(' ', '-')}
```

## Limitations

- No human-in-the-loop (HITL) support
- No version history or rollback
- No live execution logs (logs go to stdout)
- No Django ORM — credentials come from environment variables
- Subworkflow nodes may not work if the child workflow is not exported

## Generated By

AIAAS Standalone Export
""")

    def _create_zip(self, source_dir: Path, zip_path: Path):
        """Create a ZIP archive of the exported folder."""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file in source_dir.rglob('*'):
                if file.is_file():
                    arcname = file.relative_to(source_dir.parent)
                    zf.write(file, arcname)


# ──────────────────────────────────────────────
# Template strings for generated files
# ──────────────────────────────────────────────

LOGGER_STUB = '''\
"""
Console-based Execution Logger (standalone replacement for Django ORM logger)
"""
import logging
from uuid import UUID
from typing import Any
from datetime import datetime

logger = logging.getLogger("workflow.execution")


class ExecutionLogger:
    """Logs workflow execution to stdout instead of database."""

    async def log_node_start(
        self,
        execution_id: UUID,
        node_id: str,
        node_type: str,
        node_name: str = '',
        input_data: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None
    ):
        logger.info(f"[{execution_id}] Node START: {node_name or node_id} ({node_type})")

    async def log_node_complete(
        self,
        execution_id: UUID,
        node_id: str,
        success: bool,
        output_data: dict[str, Any] | None = None,
        error_message: str = '',
        duration_ms: int = 0,
        warnings: list[Any] | None = None
    ):
        status = "OK" if success else "FAIL"
        logger.info(f"[{execution_id}] Node {status}: {node_id} ({duration_ms}ms)")
        if error_message:
            logger.error(f"[{execution_id}] Error: {error_message}")

    async def complete_execution(
        self,
        execution_id: UUID,
        status: str = 'completed',
        output_data: dict[str, Any] | None = None,
        error_message: str = ''
    ):
        logger.info(f"[{execution_id}] Execution {status}")
        if error_message:
            logger.error(f"[{execution_id}] Error: {error_message}")


_logger_instance = None

def get_execution_logger() -> ExecutionLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = ExecutionLogger()
    return _logger_instance
'''

REQUIREMENTS_TXT = """\
# Standalone Workflow Runtime Dependencies
flask>=3.0
python-dotenv>=1.0
langgraph>=0.0.40
langchain>=0.1
langchain-openai>=0.0.5
langchain-google-genai>=0.0.6
langchain-community>=0.0.20
httpx>=0.26
aiohttp>=3.9
requests>=2.31
pydantic>=2.5
RestrictedPython>=7.0
cryptography>=41.0
"""

DOCKERFILE = """\
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "app.py"]
"""
