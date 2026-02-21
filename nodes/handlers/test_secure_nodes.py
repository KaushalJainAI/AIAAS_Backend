import asyncio
from typing import Any
from unittest.mock import MagicMock
from nodes.handlers.core_nodes import CodeNode, SetNode
from nodes.handlers.base import NodeItem

async def test_sandbox_security():
    print("\n--- Testing Sandbox Security ---")
    node = CodeNode()
    context = MagicMock()
    context.execution_id = "test_exec"
    context.workflow_id = 1
    context.current_input = None

    # Test 1: Blocked Import
    print("Test 1: Attempting blocked import (os)...")
    res = await node.execute(
        input_data={},
        config={"code": "import os\nreturn {'res': 'success'}"},
        context=context
    )
    if not res.success:
        print(f"✅ Blocked import: {res.error}")
    else:
        print("❌ FAILED: Import was not blocked")

    # Test 2: Blocked File Access
    print("Test 2: Attempting blocked file access (open)...")
    res = await node.execute(
        input_data={},
        config={"code": "f = open('test.txt', 'w')\nreturn {'res': 'success'}"},
        context=context
    )
    # This should fail during execution call to user_fn
    if not res.items[0].json.get('success', True) == False:
         print(f"✅ Blocked open: {res.items[0].json.get('error')}")
    else:
        print("❌ FAILED: open() was not blocked")

    # Test 3: Blocked Attribute Crawling
    print("Test 3: Attempting attribute crawling (__subclasses__)...")
    res = await node.execute(
        input_data={},
        config={"code": "classes = ().__class__.__base__.__subclasses__()\nreturn {'classes': str(classes[:1])}"},
        context=context
    )
    if not res.items[0].json.get('success', True) == False:
        print(f"✅ Blocked traversal: {res.items[0].json.get('error')}")
    else:
        print("❌ FAILED: Attribute traversal was not blocked")

    # Test 4: Blocked Print (Console)
    print("Test 4: Attempting blocked print...")
    res = await node.execute(
        input_data={},
        config={"code": "print('hello')\nreturn {'res': 'success'}"},
        context=context
    )
    if not res.items[0].json.get('success', True) == False:
        print(f"✅ Blocked print: {res.items[0].json.get('error')}")
    else:
        print("❌ FAILED: print() was not blocked")

    # Test 5: Standard Return
    print("Test 5: Valid return...")
    res = await node.execute(
        input_data={"a": 1, "b": 2},
        config={"code": "return {'sum': item['a'] + item['b']}"},
        context=context
    )
    if res.success and res.items[0].json.get('sum') == 3:
        print("✅ Valid return worked")
    else:
        print(f"❌ FAILED: Valid return failed: {res.error if not res.success else res.items}")

async def test_set_node():
    print("\n--- Testing Set Node ---")
    node = SetNode()
    context = MagicMock()
    context.current_input = None

    # Test 1: Simple Set
    res = await node.execute(
        input_data={"old": "value"},
        config={"values": {"new": "data"}, "keep_input": True},
        context=context
    )
    if res.success and res.items[0].json == {"old": "value", "new": "data"}:
        print("✅ Set with keep_input worked")
    else:
        print(f"❌ FAILED: Set failed: {res.items}")

    # Test 2: Replace Set
    res = await node.execute(
        input_data={"old": "value"},
        config={"values": {"only": "this"}, "keep_input": False},
        context=context
    )
    if res.success and res.items[0].json == {"only": "this"}:
        print("✅ Set without keep_input worked")
    else:
        print(f"❌ FAILED: Replace Set failed: {res.items}")

if __name__ == "__main__":
    asyncio.run(test_sandbox_security())
    asyncio.run(test_set_node())
