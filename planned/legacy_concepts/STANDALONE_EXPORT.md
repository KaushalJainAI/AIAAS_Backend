# Standalone Workflow Export

Export any workflow as a **self-contained Flask application** that runs
independently — zero Django dependencies.

## How It Works

The export script:

1. **Reads** the workflow definition (nodes, edges, settings) from the database
2. **Identifies** which node types are used (e.g. `http_request`, `openai`, `if`)
3. **Copies only the required** handler files — no bloat
4. **Decrypts credentials** and writes them into a `.env` file
5. **Patches imports** to remove all Django dependencies
6. **Generates** a Flask app with `/run` and `/health` endpoints

### What Gets Exported

```
my_workflow/
├── app.py               ← Flask entrypoint
├── workflow.json         ← Baked-in workflow definition
├── runtime/
│   ├── compiler.py       ← Workflow compiler (patched)
│   ├── schemas.py        ← Execution context & models
│   ├── validators.py     ← DAG/config validators (patched)
│   ├── utils.py          ← Node type utilities
│   ├── logger.py         ← Console logger (replaces Django ORM logger)
│   └── interface_stubs.py← SupervisionLevel stubs
├── nodes/
│   ├── base.py           ← Base handler classes
│   ├── registry.py       ← Auto-generated, only used nodes
│   └── <used_files>.py   ← Only the handler files you need
├── .env                  ← Decrypted credentials (keep secure!)
├── .env.example          ← Template showing required keys
├── requirements.txt
├── Dockerfile
└── README.md
```

### What Gets Removed

- Django ORM, authentication, user model
- Execution log database writes (replaced with stdout)
- HITL (human-in-the-loop) support
- Version history and workflow editor
- Streaming/WebSocket broadcasts

---

## Usage

### Option 1: Management Command

```bash
cd Backend
python manage.py export_standalone <workflow_id>

# Custom output directory
python manage.py export_standalone <workflow_id> --output-dir ./my_exports

# Also create a ZIP archive
python manage.py export_standalone <workflow_id> --zip
```

### Option 2: API Endpoint (ZIP Download)

```
POST /api/workflows/{workflow_id}/export/
Authorization: Bearer <token>
```

Returns a downloadable `.zip` file containing the standalone app.

---

## Running the Exported App

```bash
cd exports/my_workflow/

# 1. Install dependencies
pip install -r requirements.txt

# 2. Review/edit credentials in .env
#    ⚠️ The .env has been pre-filled with decrypted API keys.
#    Keep this file secure!

# 3. Start
python app.py
```

Server starts at `http://localhost:5000`.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check, returns node count |
| `POST` | `/run` | Execute the workflow |

### Example Request

```bash
curl -X POST http://localhost:5000/run \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello world"}'
```

### Example Response

```json
{
  "success": true,
  "status": "completed",
  "execution_id": "abc123...",
  "outputs": {
    "node_1": [{"json": {"result": "..."}}]
  },
  "error": null
}
```

---

## Docker Deployment

```bash
cd exports/my_workflow/

docker build -t my-workflow .
docker run -p 5000:5000 --env-file .env my-workflow
```

---

## Smart Node Selection

The export only includes handler files for node types your workflow actually uses.

| Node Type | Source File |
|-----------|------------|
| `code`, `set`, `if` | `core_nodes.py` |
| `loop`, `split_in_batches` | `logic_nodes.py` |
| `gmail`, `slack`, `telegram`, ... | `integration_nodes.py` |
| `openai`, `gemini`, `ollama`, ... | `llm_nodes.py` |
| `manual_trigger`, `webhook_trigger`, ... | `triggers.py` |
| `langchain_tool` | `langchain_nodes.py` |
| `subworkflow` | `subworkflow_node.py` |

If your workflow uses `openai` + `http_request` + `manual_trigger`, only
`llm_nodes.py`, `integration_nodes.py`, and `triggers.py` are copied.

---

## Credential Handling

Credentials are extracted from the database, **decrypted**, and written into `.env`:

```env
OPENAI_API_KEY=sk-abc123...
TELEGRAM_BOT_TOKEN=123456:ABC...
```

The Flask app loads these at startup. The generated registry maps them back to
the service identifiers that node handlers expect.

> ⚠️ **Security**: The `.env` file contains real API keys. Never commit it to
> version control. The `.env.example` file shows what's needed without values.

---

## Limitations

- **No HITL**: No human-in-the-loop — the container runs autonomously
- **No live logs**: Execution logs go to stdout, not the database
- **No version history**: The exported workflow is a snapshot
- **No subworkflow chains**: Subworkflow nodes won't work unless the child
  workflow is also exported and embedded
- **No MCP tools**: MCP integration nodes are excluded from standalone exports
