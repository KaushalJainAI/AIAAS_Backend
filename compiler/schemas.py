"""
Compiler Pydantic schemas.

Two core types:

    CompileError        — structured error/warning record returned by validators.
    ExecutionContext    — runtime state bag passed to each node handler.

ExecutionContext is constructed fresh per node invocation by the compiled
closure. Mutable dicts (node_outputs, credentials) are shared by reference
with the graph state; variables/loop_stats are copy-back (see compiler.py).
"""
from __future__ import annotations

import copy
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CompileError(BaseModel):
    """
    A single compilation diagnostic.

    Used for both errors and warnings — the `type` field discriminates
    ("error" | "warning" | "info"). A separate CompileWarning type existed
    historically but was never used consistently; consolidate here.
    """
    type: str = Field(default="error", description="error | warning | info")
    node_id: str | None = Field(default=None, serialization_alias="nodeId")
    error_type: str = Field(..., serialization_alias="code")
    message: str
    field: str | None = Field(default=None, description="Specific config field, if any")


# Regex for the `$node[...]` / `$node.foo` prefix of an expression.
# Quotes inside the bracket form must match (single or double).
_NODE_EXPR_RE = re.compile(
    r"\$node"
    r"(?:\[\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)')\s*\]"
    r"|\.(?P<dot>[A-Za-z0-9_\-]+))"
    r"(?:\.(?P<path>.*))?"
)

# Token pattern for nested path navigation: `word`, `[123]`, `["key"]`, `['key']`.
_PATH_TOKEN_RE = re.compile(r"(\w+)|\[\s*(?:(\d+)|['\"](.+?)['\"])\s*\]")

# Full-match and replace patterns for `{{ expression }}` templating.
_FULL_EXPR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class ExecutionContext(BaseModel):
    """
    Runtime context passed to each node during execution.

    Fields are grouped: identity, live state, execution tracking, nesting,
    configuration. Keep groups together so future additions land in the
    right section.
    """
    # Identity
    execution_id: UUID
    user_id: int
    workflow_id: int
    workflow_version_id: int | None = None

    # Live state
    node_outputs: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict] = Field(default_factory=list)
    warnings: list[CompileError] = Field(default_factory=list)

    # Execution tracking
    loop_stats: dict[str, int] = Field(default_factory=dict)
    executed_nodes: list[str] = Field(default_factory=list)
    current_node_id: str | None = None
    node_label_to_id: dict[str, str] = Field(default_factory=dict)
    current_input: list[dict] = Field(default_factory=list)

    # Subworkflow nesting
    nesting_depth: int = 0
    max_nesting_depth: int = 3
    workflow_chain: list[int] = Field(default_factory=list)
    parent_execution_id: UUID | None = None
    timeout_budget_ms: int | None = None

    # Config
    timeout_seconds: int = 300

    model_config = {"arbitrary_types_allowed": True}

    # Pydantic coerces None → empty for mutable fields so callers can pass
    # state dicts directly without pre-normalising.
    @field_validator(
        "node_outputs", "credentials", "variables", "loop_stats", mode="before"
    )
    @classmethod
    def _none_to_empty_dict(cls, v: Any) -> dict:
        return v if v is not None else {}

    @field_validator(
        "skills", "executed_nodes", "current_input", mode="before"
    )
    @classmethod
    def _none_to_empty_list(cls, v: Any) -> list:
        return v if v is not None else []

    # ---------- warnings / outputs ----------

    def add_warning(self, message: str, node_id: str | None = None) -> None:
        self.warnings.append(CompileError(
            node_id=node_id or self.current_node_id,
            error_type="runtime_warning",
            type="warning",
            message=message,
        ))

    def get_node_output(self, node_id: str) -> Any:
        return self.node_outputs.get(node_id)

    def set_node_output(self, node_id: str, output: Any) -> None:
        """Store a node's output and mark it as executed."""
        self.node_outputs[node_id] = output
        if node_id not in self.executed_nodes:
            self.executed_nodes.append(node_id)

    def has_executed(self, node_id: str) -> bool:
        return node_id in self.executed_nodes

    # ---------- expression resolution ----------

    def resolve_all_expressions(self, config: Any) -> Any:
        """
        Recursively resolve all `{{ }}` expressions. Used when pre-analyzed
        paths are not available (e.g. in tests or ad-hoc invocations).
        """
        if isinstance(config, str):
            if "{{" in config and "}}" in config:
                return self._resolve_string_expression(config)
            return config
        if isinstance(config, dict):
            return {k: self.resolve_all_expressions(v) for k, v in config.items()}
        if isinstance(config, list):
            return [self.resolve_all_expressions(v) for v in config]
        return config

    def resolve_expressions(self, config: dict, expression_paths: list[list]) -> dict:
        """
        Resolve only the paths pre-identified by the compiler. Faster than
        resolve_all_expressions for large configs with few templated fields.
        """
        if not expression_paths:
            return config
        resolved = copy.deepcopy(config)
        for path in expression_paths:
            value = self._get_nested_value(resolved, path)
            if isinstance(value, str):
                self._set_nested_value(resolved, path, self._resolve_string_expression(value))
        return resolved

    @staticmethod
    def _get_nested_value(data: Any, path: list) -> Any:
        for key in path:
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and isinstance(key, int):
                if 0 <= key < len(data):
                    data = data[key]
                else:
                    return None
            else:
                return None
        return data

    @staticmethod
    def _set_nested_value(data: Any, path: list, value: Any) -> None:
        if not path:
            return
        for key in path[:-1]:
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and isinstance(key, int):
                data = data[key] if 0 <= key < len(data) else None
            if data is None:
                return
        last = path[-1]
        if isinstance(data, dict):
            data[last] = value
        elif isinstance(data, list) and isinstance(last, int) and 0 <= last < len(data):
            data[last] = value

    def _resolve_string_expression(self, text: str) -> Any:
        # Whole-string expression → return evaluated value (preserving type).
        full = _FULL_EXPR_RE.fullmatch(text)
        if full:
            return self._evaluate_expression(full.group(1))

        # Interpolated string → stringify each embedded expression.
        def _sub(m: re.Match) -> str:
            val = self._evaluate_expression(m.group(1))
            return str(val) if val is not None else ""

        return _FULL_EXPR_RE.sub(_sub, text)

    def _evaluate_expression(self, expr: str) -> Any:
        expr = expr.strip()

        # $node["label"].path / $node.label.path
        m = _NODE_EXPR_RE.match(expr)
        if m:
            label = m.group("dq") or m.group("sq") or m.group("dot")
            path = m.group("path") or ""
            return self._resolve_node_ref(label, path)

        # $json / $input refer to the current node's input.
        # $json  → first item's json dict
        # $json.a.b  → path navigation starting from first item's json
        # $json["a"]  → same (bracket form)
        if expr == "$json" or expr == "$input":
            if self.current_input and isinstance(self.current_input[0], dict):
                first = self.current_input[0]
                return first.get("json", first)
            return {}

        if expr.startswith("$json.") or expr.startswith("$json["):
            path = expr[len("$json"):]
            return self._resolve_input_ref(path)

        if expr.startswith("$input.") or expr.startswith("$input["):
            path = expr[len("$input"):]
            return self._resolve_input_ref(path)

        if expr.startswith("$vars."):
            return self.get_variable(expr[len("$vars."):])

        # `event` / `event.path` — global trigger payload, with fallback into
        # common wrapper keys (body, payload) for webhook normalizations.
        if expr == "event" or expr.startswith("event."):
            path = expr[len("event."):] if expr.startswith("event.") else ""
            global_input = self.node_outputs.get("_input_global", {})
            val = self._get_value_by_path(global_input, path)
            if val is None and path:
                for wrapper in ("body", "payload"):
                    if isinstance(global_input, dict) and wrapper in global_input:
                        val = self._get_value_by_path(global_input[wrapper], path)
                        if val is not None:
                            break
            return val

        return None

    def _resolve_node_ref(self, label: str, path: str) -> Any:
        """Resolve a $node[...] reference; emit a warning if path missing."""
        node_id = self.node_label_to_id.get(label)
        if not node_id:
            # Direct-ID lookup.
            if label in self.node_outputs:
                node_id = label
            else:
                # Case-insensitive label fallback.
                label_lower = label.lower()
                for lbl, nid in self.node_label_to_id.items():
                    if lbl.lower() == label_lower:
                        node_id = nid
                        break

        if not node_id or node_id not in self.node_outputs:
            return None

        val = self._get_value_by_path(self.get_node_output(node_id), path)
        if val is None and path:
            self.add_warning(
                f"Path '{path}' not found in node '{label}' output.",
            )
        return val

    def _resolve_input_ref(self, path: str) -> Any:
        """
        Resolve a $json.xyz or $input.xyz reference.

        Leading "." is stripped so both `$json.field` and `$json[0]` land on
        the same path-parser. Navigation starts from the *current_input list*
        and auto-unwraps the first item's `.json` when the path is a field.
        """
        # Strip a leading dot (dotted form). Bracket form keeps its `[`.
        if path.startswith("."):
            path = path[1:]
        if not path:
            if self.current_input and isinstance(self.current_input[0], dict):
                first = self.current_input[0]
                return first.get("json", first)
            return {}
        return self._get_value_by_path(self.current_input, path)

    @staticmethod
    def _get_value_by_path(obj: Any, path: str) -> Any:
        """
        Walk a path string (e.g. `json.data[0].id` or `data["score"]`) through
        a nested structure. Auto-unwraps n8n-style items lists when appropriate.
        """
        if obj is None:
            return None
        if not path:
            return obj

        current = obj
        for match in _PATH_TOKEN_RE.finditer(path):
            word, index, bracket_key = match.groups()
            token = word or index or bracket_key
            if current is None:
                return None

            # Unwrap `{items: [...]}` when user didn't ask for the items key.
            if isinstance(current, dict) and "items" in current and "json" not in current:
                current = current["items"]

            # Lists: auto-dive into first item's `.json` when accessing a field.
            if isinstance(current, list) and index is None:
                if not current:
                    return None
                first = current[0]
                if isinstance(first, dict) and "json" in first:
                    if token == "json":
                        current = first["json"]
                        continue
                    inner = first["json"]
                    current = inner.get(token) if isinstance(inner, dict) else None
                    continue
                return None

            if index is not None:
                idx = int(index)
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                if isinstance(current, dict):
                    current = current.get(token)
                else:
                    return None

        return current

    # ---------- items / inputs ----------

    def get_input_for_node(self, node_id: str, edges: list[dict]) -> list[dict]:
        """
        Collect input items for a target node from all upstream edges.

        Output format: n8n-style `[{"json": {...}}, ...]`.
        """
        items: list[dict] = []
        for edge in edges:
            if edge.get("target") != node_id:
                continue
            source_output = self.get_node_output(edge.get("source"))
            if source_output is None:
                continue
            items.extend(_coerce_to_items(source_output))
        return items

    # ---------- credentials ----------

    async def get_credential(self, credential_id: str | int | None) -> Any:
        """Fetch a credential, preferring pre-injected cache."""
        if not credential_id:
            return None
        sid = str(credential_id)
        if sid in self.credentials:
            return self.credentials[sid]

        # Lazy fallback for dynamically-created references.
        try:
            from credentials.manager import get_credential_manager
            manager = get_credential_manager()
            data = await manager.get_credential(credential_id, user_id=self.user_id)
            if data:
                self.credentials[sid] = data
                return data
        except Exception as e:
            # Log at warning — missing creds are a common, recoverable user error.
            from logs.logger import logger
            logger.error(f"Failed to fetch credential {credential_id}: {e}")
        return None

    # ---------- variables ----------

    def set_variable(self, name: str, value: Any) -> None:
        self.variables[name] = value

    def get_variable(self, name: str, default: Any = None) -> Any:
        return self.variables.get(name, default)

    # ---------- loop state ----------

    def get_loop_count(self, node_id: str) -> int:
        return self.loop_stats.get(node_id, 0)

    def increment_loop(self, node_id: str) -> int:
        self.loop_stats[node_id] = self.loop_stats.get(node_id, 0) + 1
        return self.loop_stats[node_id]

    def get_batch_cursor(self, node_id: str) -> int:
        return self.variables.get(f"_cursor_{node_id}", 0)

    def set_batch_cursor(self, node_id: str, cursor: int) -> None:
        self.variables[f"_cursor_{node_id}"] = cursor

    def get_loop_items(self, node_id: str) -> list:
        return self.variables.get(f"_items_{node_id}", [])

    def set_loop_items(self, node_id: str, items: list) -> None:
        self.variables[f"_items_{node_id}"] = items

    def accumulate_loop_result(self, node_id: str, result: Any) -> None:
        key = f"_accumulated_{node_id}"
        if key not in self.variables:
            self.variables[key] = []
        self.variables[key].append(result)

    def get_accumulated_results(self, node_id: str) -> list:
        return self.variables.get(f"_accumulated_{node_id}", [])


def _coerce_to_items(source_output: Any) -> list[dict]:
    """
    Normalise any node output into the canonical items-list shape.
    """
    if isinstance(source_output, list):
        out = []
        for item in source_output:
            if isinstance(item, dict):
                out.append(item if "json" in item else {"json": item})
        return out
    if isinstance(source_output, dict):
        if "json" in source_output:
            return [source_output]
        if "items" in source_output:
            return [
                item if isinstance(item, dict) and "json" in item else {"json": item}
                for item in source_output.get("items", [])
            ]
        return [{"json": source_output}]
    return [{"json": {"value": source_output}}]


class NodeExecutionPlan(BaseModel):
    """
    Per-node execution metadata. Retained only so existing imports still
    resolve; the current compiler uses raw dicts for speed. New code should
    not depend on this class.
    """
    node_id: str
    node_type: str
    config: dict[str, Any]
    dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
