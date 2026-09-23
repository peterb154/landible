"""Guards on what the server publishes to MCP clients.

A tool-list or schema regression is invisible to every other test here — the
server keeps working, and only the client breaks — so it needs its own net.
"""

import asyncio

from landible_mcp.server import mcp

EXPECTED = {
    "landible_book_search", "landible_book_request", "landible_book_status",
    "landible_book_cancel", "landible_book_retract", "landible_book_kindle", "landible_audiobooks",
    "landible_mam_stats", "landible_webhook_list", "landible_webhook_set",
    "landible_webhook_delete", "landible_webhook_test",
}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_exactly_the_landible_tools_are_published():
    """Tools are already prefixed, so the gateway mounts this with no namespace."""
    assert set(_tools()) == EXPECTED


def test_every_parameter_has_a_top_level_type():
    """A bare union (`list[str] | None`) publishes {"anyOf": [...]} with no
    `type`, which some MCP clients can't render in their approval dialog — the
    call then fails before it reaches the server. Optional scalars are fine as
    long as each property says what it is somewhere a client can see."""
    for name, tool in _tools().items():
        for prop, schema in tool.parameters.get("properties", {}).items():
            assert "type" in schema or "anyOf" in schema, f"{name}.{prop}: {schema}"
            if "anyOf" in schema:
                types = [s.get("type") for s in schema["anyOf"]]
                assert "array" not in types, f"{name}.{prop} is an optional array: {schema}"


def test_book_request_requires_only_title_and_id():
    schema = _tools()["landible_book_request"].parameters
    assert sorted(schema["required"]) == ["foreign_book_id", "title"]
