import re
import json
import logging

logger = logging.getLogger(__name__)

# Core patterns for tool call detection
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
    'xml_tool_block': r'<(?:tool_call|invoke|tool|FunctionCall|call:[a-z0-9_]+)[\s>].*?</(?:tool_call|invoke|tool|FunctionCall|call:[a-z0-9_]+)>'
}

def clean_json_string(s: str) -> str:
    """Removes common LLM artifacts from JSON strings."""
    if not s:
        return ""
    # Remove markdown code blockers
    s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.MULTILINE)
    s = re.sub(r'\s*```$', '', s, flags=re.MULTILINE)
    return s.strip()

def fuzzy_json_loads(s: str) -> any:
    """Attempts to parse JSON-like strings, including those with single quotes."""
    s = clean_json_string(s)
    if not s: return None
    
    # Try standard JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Try Python-literal style dict/list first (single quotes, True/False/None)
    try:
        parsed = ast.literal_eval(s)
        return parsed
    except Exception:
        pass

    # Try converting Python-style dict (single quotes) to JSON
    try:
        # Very simple replacement for common cases. 
        # For more complex cases, we'd need a proper tokenizer.
        fixed = re.sub(r"(^|[\{\}\[\],:])\s*'(.*?)'\s*(?=[\{\}\[\],:])", r'\1"\2"', s)
        # Fallback to simple replace if regex fails to find matches
        if fixed == s:
            fixed = s.replace("'", '"')
        return json.loads(fixed)
    except:
        pass
        
    return None


def parse_tool_arguments(raw_args):
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

    s = clean_json_string(str(raw_args))
    if not s:
        return {}

    parsed = fuzzy_json_loads(s)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"items": parsed}
    if parsed is not None:
        return {"query": str(parsed)}

    # Last fallback: keep raw text as query for search-like tools.
    return {"query": s}

def extract_tool_calls(content: str):
    """
    Identifies and extracts tool calls from LLM content.
    Returns a list of dictionaries: {'tool': str, 'args': dict, 'raw': str}
    """
    tool_calls = []
    
    # 1. Standard [TOOL_CALL]
    for match in re.finditer(PATTERNS['standard'], content, re.DOTALL):
        raw = match.group(0)
        data = fuzzy_json_loads(match.group(1))
        if isinstance(data, dict):
            tool = data.get('tool') or data.get('action')
            args = data.get('args') or data.get('parameters') or {}
            if tool:
                tool_calls.append({'tool': tool, 'args': args, 'raw': raw})

    # 2. Anthropic/Standard <invoke> or <tool>
    for match in re.finditer(PATTERNS['anthropic_tool'], content, re.DOTALL):
        name = match.group(1)
        args_str = match.group(2)
        raw = match.group(0)
        try:
            # Anthropic args are often XML tags too, but sometimes JSON
            if '<parameter' in args_str or '<param' in args_str:
                # Custom XML arg parsing (parameter or param)
                args = {}
                for p_match in re.finditer(r'<(?:parameter|param)\s+name=["\'](.*?)["\']\s*>(.*?)</(?:parameter|param)>', args_str, re.DOTALL):
                    args[p_match.group(1)] = p_match.group(2).strip()
            else:
                args = (fuzzy_json_loads(args_str) or {"query": args_str.strip()}) if args_str else {}
            
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})
        except:
            # Fallback to raw string if json fails
            tool_calls.append({'tool': name, 'args': {'query': args_str.strip()}, 'raw': raw})

    # 3. Nested Tags
    for match in re.finditer(PATTERNS['nested_tags'], content, re.DOTALL):
        name = match.group(1).strip()
        args_str = match.group(2).strip()
        raw = match.group(0)
        try:
            args = json.loads(clean_json_string(args_str))
        except:
            args = {'query': args_str}
        tool_calls.append({'tool': name, 'args': args, 'raw': raw})

    # 4. Unified Tags (e.g. <web_search>query</web_search>)
    for match in re.finditer(PATTERNS['unified_tag'], content, re.DOTALL):
        name = match.group(1).strip()
        args_str = match.group(2).strip()
        raw = match.group(0)
        # Avoid duplicate detection if it was already caught by standard patterns
        if any(tc['raw'] == raw for tc in tool_calls):
            continue
            
        try:
            args = json.loads(clean_json_string(args_str))
        except:
            args = {'query': args_str}
        tool_calls.append({'tool': name, 'args': args, 'raw': raw})

    # 5. Prefix Style
    for match in re.finditer(PATTERNS['prefix_style'], content):
        provider = match.group(1)
        args_str = match.group(2).strip()
        raw = match.group(0)
        try:
            data = json.loads(clean_json_string(args_str))
            if isinstance(data, dict):
                tool = data.get('tool', 'web_search')
                args = data.get('args', data)
                tool_calls.append({'tool': tool, 'args': args, 'raw': raw})
        except:
            pass

    # 6. Llama Tag
    for match in re.finditer(PATTERNS['llama_tag'], content, re.DOTALL):
        inner = match.group(1).strip()
        raw = match.group(0)
        if '(' in inner and inner.endswith(')'):
            # name(args) style
            name = inner.split('(')[0].strip()
            args_str = inner[len(name)+1:-1]
            try:
                args = json.loads(f"{{{args_str}}}") # Simple attempt at k=v to json
            except:
                args = {"query": args_str}
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})
        else:
            try:
                data = json.loads(clean_json_string(inner))
                tool_calls.append({'tool': data.get('tool', 'web_search'), 'args': data.get('args', data), 'raw': raw})
            except:
                pass

    # 7. Call Tag
    for match in re.finditer(PATTERNS['call_tag'], content, re.DOTALL):
        name = match.group(1).strip()
        args_str = match.group(2).strip()
        raw = match.group(0)
        try:
            args = json.loads(clean_json_string(args_str))
        except:
            args = {"query": args_str}
        tool_calls.append({'tool': name, 'args': args, 'raw': raw})

    # 8. ReAct Style
    for match in re.finditer(PATTERNS['react_style'], content, re.IGNORECASE):
        name = match.group(1).strip()
        args_str = match.group(2).strip()
        raw = match.group(0)
        try:
            args = json.loads(clean_json_string(args_str))
        except:
            args = {"query": args_str}
        tool_calls.append({'tool': name, 'args': args, 'raw': raw})

    # 10. Minimax Hybrid
    for match in re.finditer(PATTERNS['minimax'], content, re.DOTALL):
        inner_invoke = match.group(1)
        raw = match.group(0)
        # Use existing anthropic-style parsing on the inner invoke
        sub_match = re.search(PATTERNS['anthropic_tool'], inner_invoke, re.DOTALL)
        if sub_match:
            name = sub_match.group(1)
            args_str = sub_match.group(2)
            try:
                if '<parameter' in args_str or '<param' in args_str:
                    # Custom XML arg parsing
                    args = {}
                    for p_match in re.finditer(r'<(?:parameter|param)\s+name=["\'](.*?)["\']\s*>(.*?)</(?:parameter|param)>', args_str, re.DOTALL):
                        args[p_match.group(1)] = p_match.group(2).strip()
                else:
                    args = fuzzy_json_loads(args_str) or {"query": args_str.strip()}
                tool_calls.append({'tool': name, 'args': args if isinstance(args, dict) else {"query": str(args)}, 'raw': raw})
            except:
                tool_calls.append({'tool': name, 'args': {'query': args_str.strip()}, 'raw': raw})

    # 13. FunctionCall Tag (Resilient XML/JSON hybrid)
    for match in re.finditer(PATTERNS['function_call'], content, re.DOTALL):
        inner = match.group(1).strip()
        raw = match.group(0)
        # Try fuzzy JSON first
        data = fuzzy_json_loads(inner)
        if isinstance(data, dict):
            tool = data.get('tool') or data.get('name') or data.get('action')
            args = data.get('args') or data.get('parameters') or data.get('arguments')
            if tool:
                args = parse_tool_arguments(args)
                tool_calls.append({'tool': tool, 'args': args or {}, 'raw': raw})
        else:
            # If not JSON, try to extract key-value pairs manually
            tool_match = re.search(r'["\']?tool["\']?\s*:\s*["\'](.*?)["\']', inner)
            if tool_match:
                tool = tool_match.group(1)
                args_match = re.search(r'["\']?args["\']?\s*:\s*["\'](.*?)(?:["\']\s*[,}]|$)', inner, re.DOTALL)
                args = {"query": args_match.group(1).strip()} if args_match else {}
                tool_calls.append({'tool': tool, 'args': args, 'raw': raw})

    # 13b. Generic JSON-ish tool object (single or double quotes)
    for match in re.finditer(PATTERNS['generic_json_tool'], content, re.DOTALL):
        raw = match.group(0)
        if any(tc['raw'] == raw for tc in tool_calls):
            continue
        name = (match.group(1) or "").strip()
        args_raw = (match.group(2) or "").strip()
        if not name:
            continue
        args = parse_tool_arguments(args_raw)
        tool_calls.append({'tool': name, 'args': args, 'raw': raw})

    # 14. Tool Code Block (e.g. <tool_code>web_search<query>...</query></tool_code>)
    for match in re.finditer(PATTERNS['code_block'], content, re.DOTALL):
        inner = match.group(1).strip()
        raw = match.group(0)
        
        # Look for name and tag-based args
        # Format 1: tool_name<arg_name>value</arg_name>
        name_match = re.match(r'^([a-z0-9_]+)', inner)
        if name_match:
            name = name_match.group(1)
            remaining = inner[len(name):].strip()
            args = {}
            # Extract any <tag>value</tag> patterns as args
            for p_match in re.finditer(r'<([a-z0-9_]+)>(.*?)</\1>', remaining, re.DOTALL):
                args[p_match.group(1)] = p_match.group(2).strip()
            
            # If no tags found, treat remainder as 'query' if not empty
            if not args and remaining:
                args = {"query": remaining}
            
            tool_calls.append({'tool': name, 'args': args, 'raw': raw})
        else:
            # Fallback: maybe it's just a JSON object inside?
            data = fuzzy_json_loads(inner)
            if isinstance(data, dict) and (data.get('tool') or data.get('action')):
                tool_calls.append({
                    'tool': data.get('tool') or data.get('action'),
                    'args': data.get('args') or data.get('parameters') or {},
                    'raw': raw
                })

    return tool_calls

def strip_tool_calls(content: str) -> str:
    """
    Removes all detected tool calls and internal model signals from the content.
    Iterates to handle nested or overlapping patterns.
    """
    if not content:
        return ""
        
    cleaned = content
    
    # Priority 1: Specific block patterns (aggressive)
    for key in ['xml_tool_block', 'code_block', 'json_block', 'standard', 'anthropic_tool', 'nested_tags', 'unified_tag', 'minimax']:
        if key in PATTERNS:
            cleaned = re.sub(PATTERNS[key], '', cleaned, flags=re.DOTALL)
            
    # Priority 2: Shard patterns (generic tags and signatures)
    for pattern in PATTERNS.values():
        cleaned = re.sub(pattern, '', cleaned, flags=re.DOTALL | re.MULTILINE)
    
    # Priority 3: Thinking blocks
    cleaned = re.sub(r'<(?:thought|think)>.*?</(?:thought|think)>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    
    # Final cleanup: orphaned tool tags that might be missed by blocks
    lingering_tags = [
        r'</?tool_call>', r'</?tool>', r'</?invoke>', r'</?parameter>', 
        r'</?param>', r'</?FunctionCall>', r'</?tool_code>', r'</?minimax:tool_call>'
    ]
    for tag in lingering_tags:
        cleaned = re.sub(tag, '', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()

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
        r'</tool_code>'
    ]

