import re
import json
import logging
import ast

logger = logging.getLogger(__name__)


class ToolCallParser:
    PATTERNS = {
        'standard': r'\[TOOL_CALL\](.*?)(?:\[/TOOL_CALL\]|$)',
        'anthropic_tool': r'<(?:invoke|tool|tool_call)\s+name=["\'](.*?)["\']\s*>(.*?)(?:</(?:invoke|tool|tool_call)>|$)',
        'nested_tags': r'<tool_call>\s*<tool_name>(.*?)</tool_name>\s*<tool_arg>(.*?)</tool_arg>\s*</tool_call>',
        'unified_tag': r'<(?!tool_call)([a-z0-9_]+(?:_search|_tool|_gen|tool_call))>(.*?)</\1>',
        'tool_call_wrapper': r'</?tool_call>',
        'prefix_style': r'(?:^|\n)([a-z0-9_]+):tool_call\s+(.*)',
        'code_block': r'<tool_code>(.*?)</tool_code>',
        'llama_tag': r'<\|python_tag\|>(.*?)(?:<\||$)',
        'call_tag': r'<call:([a-z0-9_]+)>(.*?)</call:\1>',
        'react_style': r'(?:Action|Selected Tool|Tool):\s*([a-z0-9_]+)\s*(?:\n|:)\s*(?:Action Input|Arguments|Args):\s*(.*)',
        'json_block': r'```json\s*\n?(\{\s*"tool"\s*:\s*".*?"\s*,.*?\})\s*\n?```',
        'minimax': r'(?:<|)minimax:tool_call(?:>|)\s*(<invoke\s+name=["\'](.*?)["\']\s*>.*?</invoke>)\s*(?:</minimax:tool_call>|$)',
        'function_call': r'<FunctionCall>\s*(.*?)\s*(?:</FunctionCall>|$)',
        'thought_block': r'<(?:thought|think)>\s*(.*?)\s*</(?:thought|think)>',
        'generic_json_tool': r'\{\s*["\'](?:tool|action)["\']\s*:\s*["\'](.*?)["\']\s*,\s*["\'](?:args|parameters|arguments)["\']\s*:\s*(.*)\s*\}',
        'xml_tool_block': r'<(?:tool_call|invoke|tool|FunctionCall|call:[a-z0-9_]+)[\s>].*?</(?:tool_call|invoke|tool|FunctionCall|call:[a-z0-9_]+)>',
        # Arrow hash: {tool => "name", args => ...} - keys may be quoted or bare
        'arrow_hash': r"\{\s*['\"]?tool['\"]?\s*=>\s*['\"]([a-z0-9_]+)['\"]\s*,\s*['\"]?args['\"]?\s*=>\s*((?:\{[^}]*\}|[^,}]+))",
        'python_markdown': r'```(?:python|py)\s*\n(.*?)\n```',
    }

    @staticmethod
    def clean_json_string(s: str) -> str:
        """Removes common LLM artifacts from JSON strings."""
        if not s:
            return ""
        s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.MULTILINE)
        s = re.sub(r'\s*```$', '', s, flags=re.MULTILINE)
        return s.strip()

    @classmethod
    def fuzzy_json_loads(cls, s: str):
        """Attempts to parse JSON-like strings, including those with single quotes."""
        s = cls.clean_json_string(s)
        if not s:
            return None

        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

        try:
            parsed = ast.literal_eval(s)
            return parsed
        except Exception:
            pass

        try:
            fixed = re.sub(r"(^|[\{\}\[\],:])\s*'(.*?)'\s*(?=[\{\}\[\],:])", r'\1"\2"', s)
            if fixed == s:
                fixed = s.replace("'", '"')
            return json.loads(fixed)
        except Exception:
            pass

        try:
            arrow_fixed = re.sub(r'(?<!["\w])(\b[a-z_][a-z0-9_]*)\b\s*=>', r'"\1":', s)
            arrow_fixed = re.sub(r"([\"'])\s*=>\s*", r'\1:', arrow_fixed)
            arrow_fixed = re.sub(r"(?<![\\])'", '"', arrow_fixed)
            arrow_fixed = re.sub(r'\{\s*--([a-z_][a-z0-9_]*)\s+"([^"]*)"\s*\}', r'{"\1": "\2"}', arrow_fixed)
            arrow_fixed = re.sub(r"\{\s*--([a-z_][a-z0-9_]*)\s+'([^']*)'\s*\}", r'{"\1": "\2"}', arrow_fixed)
            arrow_fixed = re.sub(r'\{\s*--([a-z_][a-z0-9_]*)\s+([^}"\s][^}\s]*)\s*\}', r'{"\1": "\2"}', arrow_fixed)
            return json.loads(arrow_fixed)
        except Exception:
            pass

        return None

    @classmethod
    def parse_tool_arguments(cls, raw_args):
        """
        Parse tool arguments emitted by heterogeneous models.
        Returns a dict whenever possible.
        """
        if isinstance(raw_args, dict):
            return raw_args
        if raw_args is None:
            return {}
        if isinstance(raw_args, (int, float, bool)):
            return {"query": str(raw_args)}

        s = cls.clean_json_string(str(raw_args))
        if not s:
            return {}

        parsed = cls.fuzzy_json_loads(s)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        if parsed is not None:
            return {"query": str(parsed)}

        try:
            cli_args = {}
            for flag_match in re.finditer(r'--([a-z][a-z0-9_]*)(?:\s+|=)(?:"([^"]*?)"|\'([^\']*?)\'|([^\s\-][^\s]*))', s):
                key = flag_match.group(1)
                val = flag_match.group(2) or flag_match.group(3) or flag_match.group(4) or ''
                cli_args[key] = val.strip()
            if cli_args:
                return cli_args
        except Exception:
            pass

        return {"query": s}

    @classmethod
    def extract_tool_calls(cls, content: str):
        """
        Identifies and extracts tool calls from LLM content.
        Returns a list of dictionaries: {'tool': str, 'args': dict, 'raw': str}
        """
        tool_calls = []

        # 1. Standard [TOOL_CALL]
        for match in re.finditer(cls.PATTERNS['standard'], content, re.DOTALL):
            raw = match.group(0)
            data = cls.fuzzy_json_loads(match.group(1))
            if isinstance(data, dict):
                tool = data.get('tool') or data.get('action')
                args = data.get('args') or data.get('parameters') or {}
                if tool:
                    tool_calls.append({'tool': tool, 'args': args, 'raw': raw})

        # 2. Anthropic/Standard <invoke> or <tool>
        for match in re.finditer(cls.PATTERNS['anthropic_tool'], content, re.DOTALL):
            name = match.group(1)
            args_str = match.group(2)
            raw = match.group(0)
            try:
                if '<parameter' in args_str or '<param' in args_str:
                    args = {}
                    for p_match in re.finditer(r'<(?:parameter|param)\s+name=["\'](.*?)["\']\s*>(.*?)</(?:parameter|param)>', args_str, re.DOTALL):
                        args[p_match.group(1)] = p_match.group(2).strip()
                else:
                    args = (cls.fuzzy_json_loads(args_str) or {"query": args_str.strip()}) if args_str else {}
                tool_calls.append({'tool': name, 'args': args, 'raw': raw})
            except Exception:
                tool_calls.append({'tool': name, 'args': {'query': args_str.strip()}, 'raw': raw})

        # 3. Nested Tags
        for match in re.finditer(cls.PATTERNS['nested_tags'], content, re.DOTALL):
            name = match.group(1).strip()
            args_str = match.group(2).strip()
            raw = match.group(0)
            try:
                args = json.loads(cls.clean_json_string(args_str))
            except Exception:
                args = {'query': args_str}
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 4. Unified Tags (e.g. <web_search>query</web_search>)
        for match in re.finditer(cls.PATTERNS['unified_tag'], content, re.DOTALL):
            name = match.group(1).strip()
            args_str = match.group(2).strip()
            raw = match.group(0)
            if any(tc['raw'] == raw for tc in tool_calls):
                continue
            try:
                args = json.loads(cls.clean_json_string(args_str))
            except Exception:
                args = {'query': args_str}
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 5. Prefix Style
        for match in re.finditer(cls.PATTERNS['prefix_style'], content):
            args_str = match.group(2).strip()
            raw = match.group(0)
            try:
                data = json.loads(cls.clean_json_string(args_str))
                if isinstance(data, dict):
                    tool = data.get('tool', 'web_search')
                    args = data.get('args', data)
                    tool_calls.append({'tool': tool, 'args': args, 'raw': raw})
            except Exception:
                pass

        # 6. Llama Tag
        for match in re.finditer(cls.PATTERNS['llama_tag'], content, re.DOTALL):
            inner = match.group(1).strip()
            raw = match.group(0)
            if '(' in inner and inner.endswith(')'):
                name = inner.split('(')[0].strip()
                args_str = inner[len(name)+1:-1]
                try:
                    args = json.loads(f"{{{args_str}}}")
                except Exception:
                    args = {"query": args_str}
                tool_calls.append({'tool': name, 'args': args, 'raw': raw})
            else:
                try:
                    data = json.loads(cls.clean_json_string(inner))
                    tool_calls.append({'tool': data.get('tool', 'web_search'), 'args': data.get('args', data), 'raw': raw})
                except Exception:
                    pass

        # 7. Call Tag
        for match in re.finditer(cls.PATTERNS['call_tag'], content, re.DOTALL):
            name = match.group(1).strip()
            args_str = match.group(2).strip()
            raw = match.group(0)
            try:
                args = json.loads(cls.clean_json_string(args_str))
            except Exception:
                args = {"query": args_str}
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 8. ReAct Style
        for match in re.finditer(cls.PATTERNS['react_style'], content, re.IGNORECASE):
            name = match.group(1).strip()
            args_str = match.group(2).strip()
            raw = match.group(0)
            try:
                args = json.loads(cls.clean_json_string(args_str))
            except Exception:
                args = {"query": args_str}
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 10. Minimax Hybrid
        for match in re.finditer(cls.PATTERNS['minimax'], content, re.DOTALL):
            inner_invoke = match.group(1)
            raw = match.group(0)
            sub_match = re.search(cls.PATTERNS['anthropic_tool'], inner_invoke, re.DOTALL)
            if sub_match:
                name = sub_match.group(1)
                args_str = sub_match.group(2)
                try:
                    if '<parameter' in args_str or '<param' in args_str:
                        args = {}
                        for p_match in re.finditer(r'<(?:parameter|param)\s+name=["\'](.*?)["\']\s*>(.*?)</(?:parameter|param)>', args_str, re.DOTALL):
                            args[p_match.group(1)] = p_match.group(2).strip()
                    else:
                        args = cls.fuzzy_json_loads(args_str) or {"query": args_str.strip()}
                    tool_calls.append({'tool': name, 'args': args if isinstance(args, dict) else {"query": str(args)}, 'raw': raw})
                except Exception:
                    tool_calls.append({'tool': name, 'args': {'query': args_str.strip()}, 'raw': raw})

        # 13. FunctionCall Tag (Resilient XML/JSON hybrid)
        for match in re.finditer(cls.PATTERNS['function_call'], content, re.DOTALL):
            inner = match.group(1).strip()
            raw = match.group(0)
            data = cls.fuzzy_json_loads(inner)
            if isinstance(data, dict):
                tool = data.get('tool') or data.get('name') or data.get('action')
                args = data.get('args') or data.get('parameters') or data.get('arguments')
                if tool:
                    args = cls.parse_tool_arguments(args)
                    tool_calls.append({'tool': tool, 'args': args or {}, 'raw': raw})
            else:
                tool_match = re.search(r'["\']?tool["\']?\s*:\s*["\'](.*?)["\']', inner)
                if tool_match:
                    tool = tool_match.group(1)
                    args_match = re.search(r'["\']?args["\']?\s*:\s*["\'](.*?)(?:["\']\s*[,}]|$)', inner, re.DOTALL)
                    args = {"query": args_match.group(1).strip()} if args_match else {}
                    tool_calls.append({'tool': tool, 'args': args, 'raw': raw})

        # 13b. Generic JSON-ish tool object (single or double quotes)
        for match in re.finditer(cls.PATTERNS['generic_json_tool'], content, re.DOTALL):
            raw = match.group(0)
            if any(tc['raw'] == raw for tc in tool_calls):
                continue
            name = (match.group(1) or "").strip()
            args_raw = (match.group(2) or "").strip()
            if not name:
                continue
            args = cls.parse_tool_arguments(args_raw)
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 14. Tool Code Block (e.g. <tool_code>web_search<query>...</query></tool_code>)
        for match in re.finditer(cls.PATTERNS['code_block'], content, re.DOTALL):
            inner = match.group(1).strip()
            raw = match.group(0)
            name_match = re.match(r'^([a-z0-9_]+)', inner)
            if name_match:
                name = name_match.group(1)
                remaining = inner[len(name):].strip()
                args = {}
                for p_match in re.finditer(r'<([a-z0-9_]+)>(.*?)</\1>', remaining, re.DOTALL):
                    args[p_match.group(1)] = p_match.group(2).strip()
                if not args and remaining:
                    args = {"query": remaining}
                tool_calls.append({'tool': name, 'args': args, 'raw': raw})
            else:
                data = cls.fuzzy_json_loads(inner)
                if isinstance(data, dict) and (data.get('tool') or data.get('action')):
                    tool_calls.append({
                        'tool': data.get('tool') or data.get('action'),
                        'args': data.get('args') or data.get('parameters') or {},
                        'raw': raw
                    })

        # 15. Arrow-hash style: {'tool' => 'name', 'args' => '...'}
        for match in re.finditer(cls.PATTERNS['arrow_hash'], content, re.DOTALL):
            raw = match.group(0)
            if any(raw in tc['raw'] or tc['raw'] in raw for tc in tool_calls):
                continue
            name = match.group(1).strip()
            args_raw = match.group(2).strip()
            if (args_raw.startswith("'") and args_raw.endswith("'")) or \
               (args_raw.startswith('"') and args_raw.endswith('"')):
                args_raw = args_raw[1:-1]
            args = cls.parse_tool_arguments(args_raw)
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})

        # 16. Pure Python Markdown Block fallback (assumes execute_python_code tool)
        for match in re.finditer(cls.PATTERNS['python_markdown'], content, re.DOTALL):
            raw = match.group(0)
            code = match.group(1).strip()
            if code and not any(raw in tc['raw'] or tc['raw'] in raw for tc in tool_calls):
                try:
                    data = cls.fuzzy_json_loads(code)
                    if isinstance(data, dict) and 'tool' in data:
                        tool_calls.append({'tool': data.get('tool'), 'args': data.get('args', {}), 'raw': raw})
                        continue
                except Exception:
                    pass
                tool_calls.append({'tool': 'execute_python_code', 'args': {'code': code}, 'raw': raw})

        return tool_calls

    @classmethod
    def strip_tool_calls(cls, content: str) -> str:
        """
        Removes all detected tool calls and internal model signals from the content.
        Iterates to handle nested or overlapping patterns.
        """
        if not content:
            return ""

        cleaned = content

        for key in ['xml_tool_block', 'code_block', 'json_block', 'python_markdown', 'standard', 'anthropic_tool', 'nested_tags', 'unified_tag', 'minimax']:
            if key in cls.PATTERNS:
                cleaned = re.sub(cls.PATTERNS[key], '', cleaned, flags=re.DOTALL)

        for pattern in cls.PATTERNS.values():
            cleaned = re.sub(pattern, '', cleaned, flags=re.DOTALL | re.MULTILINE)

        cleaned = re.sub(r'<(?:thought|think)>.*?</(?:thought|think)>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

        lingering_tags = [
            r'</?tool_call>', r'</?tool>', r'</?invoke>', r'</?parameter>',
            r'</?param>', r'</?FunctionCall>', r'</?tool_code>', r'</?minimax:tool_call>'
        ]
        for tag in lingering_tags:
            cleaned = re.sub(tag, '', cleaned, flags=re.IGNORECASE)

        return cleaned.strip()

    @staticmethod
    def get_block_signatures():
        """Returns a list of regex patterns/strings that should be blocked in 'thinking' streams."""
        return [
            r'\[TOOL_CALL\]',
            r'\[/TOOL_CALL\]',
            r'<invoke',
            r'</invoke>',
            r'<tool name=',
            r'</tool>',
            r'<parameter',
            r'</parameter>',
            r'<tool_call>',
            r'</tool_call>',
            r'\{"tool":',
            r'\{"action":',
            r'"tool_call"',
            r':tool_call',
            r'minimax:tool_call',
            r'</minimax:tool_call>',
            r'<FunctionCall>',
            r'</FunctionCall>',
            r'<\|python_tag\|>',
            r'<call:',
            r'</call:',
            r'Action:',
            r'Action Input:',
            r'Selected Tool:',
            r'```json',
            r'<(?:thought|think)>',
            r'</(?:thought|think)>',
            r'<tool_code>',
            r'</tool_code>',
            r"'tool'\s*=>",
            r"'args'\s*=>",
        ]


# Module-level aliases for backward compatibility
clean_json_string = ToolCallParser.clean_json_string
fuzzy_json_loads = ToolCallParser.fuzzy_json_loads
parse_tool_arguments = ToolCallParser.parse_tool_arguments
extract_tool_calls = ToolCallParser.extract_tool_calls
strip_tool_calls = ToolCallParser.strip_tool_calls
get_block_signatures = ToolCallParser.get_block_signatures
