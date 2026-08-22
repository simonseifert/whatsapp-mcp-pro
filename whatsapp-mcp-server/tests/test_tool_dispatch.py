"""Invoke read-only tools through the real MCP dispatch layer.

The rest of the suite asserts tool *registration* (names, annotations) but never
calls a tool the way a client does. That gap let four core tools ship returning
dataclasses against a `dict` schema — registration was fine, every real call
raised a validation error. These tests call through `mcp.call_tool`, which runs
the same structured-output validation an MCP client triggers, so a return-shape
regression fails here instead of in production.
"""

import importlib

import pytest

# The read tools that return store data and are safe to call with no bridge.
# Each maps to arguments that resolve against the temp DB fixtures.
READ_TOOLS = [
    ("list_chats", {}),
    ("list_messages", {"chat_jid": "123456789@s.whatsapp.net"}),
    ("get_chat", {"chat_jid": "123456789@s.whatsapp.net"}),
    ("get_chat", {"chat_jid": "does-not-exist@s.whatsapp.net"}),  # miss → null, not error
    ("get_message_context", {"message_id": "msg1"}),
    ("search_contacts", {"query": "John"}),
    ("list_all_contacts", {}),
    ("get_contact_context", {"identifier": "123456789@s.whatsapp.net"}),
    ("get_direct_chat_by_contact", {"phone_number": "123456789"}),
    ("get_contact_context", {"identifier": "123456789", "include_chats": True}),
]


@pytest.fixture
def dispatch_main(monkeypatch, temp_messages_db, temp_whatsapp_db):
    """main module with every toolset on and DB paths pointed at temp fixtures."""
    monkeypatch.setenv("WHATSAPP_MCP_TOOLSETS", "all")
    monkeypatch.delenv("WHATSAPP_MCP_TOOLS", raising=False)

    import lib.identity as identity
    import whatsapp

    # The query functions read these module globals at call time.
    for mod in (whatsapp, identity):
        monkeypatch.setattr(mod, "MESSAGES_DB_PATH", temp_messages_db, raising=False)
        monkeypatch.setattr(mod, "WHATSAPP_DB_PATH", temp_whatsapp_db, raising=False)

    import main

    # main binds `from whatsapp import <fn>` at import; those functions close over
    # the (now-patched) whatsapp module globals, so a reload picks up the paths.
    importlib.reload(main)
    return main


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,args", READ_TOOLS)
async def test_read_tool_dispatches_without_validation_error(dispatch_main, tool_name, args):
    """Every read tool returns a schema-valid payload through the MCP layer.

    call_tool raises on a structured-output mismatch, so reaching a result at
    all is the assertion; we additionally check the shape is a real container.
    """
    result = await dispatch_main.mcp.call_tool(tool_name, args)

    # FastMCP returns (content_blocks, structured_result). The structured half is
    # what gets schema-validated; None is only valid for the deliberate miss.
    structured = result[1] if isinstance(result, tuple) else result
    if tool_name == "get_chat" and args["chat_jid"].startswith("does-not-exist"):
        assert structured is None or structured == {} or structured.get("result") is None
    else:
        # FastMCP wraps a bare None return as {"result": None}; a plain
        # `is not None` check would wave that through and give false confidence
        # that the tool returned data. Require a real payload for hit cases.
        assert structured is not None
        unwrapped = structured.get("result", structured) if isinstance(structured, dict) else structured
        assert unwrapped not in (None, {}, []), f"{tool_name} returned an empty result: {structured!r}"
