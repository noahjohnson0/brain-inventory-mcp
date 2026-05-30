"""FastMCP server exposing an Obsidian vault's inventory as MCP tools.

Run locally (stdio, for Claude Desktop / Claude Code on this laptop):
    uv run brain-inventory-mcp

Run as an authenticated remote server (Streamable HTTP + self-hosted OAuth 2.1),
for a tunnel + Claude phone connector:
    BRAIN_MCP_TRANSPORT=http \
    BRAIN_MCP_PUBLIC_URL=https://xyz.trycloudflare.com \
    BRAIN_MCP_PASSWORD='something-strong' \
    uv run brain-inventory-mcp

The HTTP transport refuses to start unauthenticated (set BRAIN_MCP_INSECURE=1 only
for loopback testing). Writes edit vault Markdown in place and autocommit+push
unless BRAIN_MCP_AUTOCOMMIT=0.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import vault

_AUTOCOMMIT = os.environ.get("BRAIN_MCP_AUTOCOMMIT", "1") != "0"


def _maybe_commit(message: str) -> str:
    return vault.git_commit(message) if _AUTOCOMMIT else "autocommit disabled"


def _build_server() -> "FastMCP":
    transport = os.environ.get("BRAIN_MCP_TRANSPORT", "stdio").lower()
    http = transport in ("http", "streamable-http")
    insecure = os.environ.get("BRAIN_MCP_INSECURE") == "1"

    if http and not insecure:
        # Authenticated public transport: self-hosted OAuth 2.1.
        from . import auth

        if not os.environ.get("BRAIN_MCP_PASSWORD"):
            raise RuntimeError(
                "Refusing to start public HTTP transport without BRAIN_MCP_PASSWORD. "
                "Set a strong password, or use BRAIN_MCP_INSECURE=1 for loopback-only testing."
            )
        provider = auth.SingleUserOAuthProvider()
        server = FastMCP(
            "brain-inventory",
            auth_server_provider=provider,
            auth=auth.auth_settings(),
            transport_security=auth.transport_security(),
            host=os.environ.get("BRAIN_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("BRAIN_MCP_PORT", "8848")),
        )
        auth.register_login_routes(server, provider)
    else:
        server = FastMCP(
            "brain-inventory",
            host=os.environ.get("BRAIN_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("BRAIN_MCP_PORT", "8848")),
        )
    _register_tools(server)
    return server


def _register_tools(mcp: "FastMCP") -> None:
    @mcp.tool()
    def search_inventory(query: str) -> list[dict]:
        """Search inventory items by name, category, parent, or body text.

        Returns a list of item summaries (name, category, is_container, parent, file).
        Use this first when the user describes an item loosely ("my charger").
        """
        return [i.summary() for i in vault.search_items(query)]

    @mcp.tool()
    def list_inventory(category: str | None = None, parent: str | None = None) -> list[dict]:
        """List inventory items, optionally filtered by category and/or parent container.

        `parent` may be given with or without [[wikilink]] brackets (e.g. "Bedroom Closet").
        With no filters, returns every item.
        """
        return [i.summary() for i in vault.list_items(category=category, parent=parent)]

    @mcp.tool()
    def get_item(name: str) -> dict:
        """Get the full note for one item: its frontmatter and Markdown body."""
        item = vault.find_item(name)
        if not item:
            return {"error": f"No item named {name!r}. Try search_inventory."}
        return {**item.summary(), "meta": item.meta, "body": item.body}

    @mcp.tool()
    def add_item(
        name: str,
        category: str = "Uncategorized",
        parent: str | None = None,
        body: str = "",
        is_container: bool = False,
    ) -> dict:
        """Create a new inventory item.

        `name` is used verbatim as the note title/filename (Proper Case With Spaces;
        brand quirks like "iPhone 13 Pro" preserved). `parent` is the container it lives
        in (room/bag/drawer), with or without [[brackets]]. Set `is_container` true if
        other items will live inside this one.
        """
        try:
            item = vault.create_item(
                name=name, category=category, parent=parent,
                body=body, is_container=is_container,
            )
        except FileExistsError as e:
            return {"error": str(e)}
        git = _maybe_commit(f"inventory: add {item.name}")
        return {**item.summary(), "git": git}

    @mcp.tool()
    def move_item(name: str, new_parent: str) -> dict:
        """Move an item to a different container (updates its `parent` and `updated` date)."""
        try:
            item = vault.move_item(name, new_parent)
        except FileNotFoundError as e:
            return {"error": str(e)}
        git = _maybe_commit(f"inventory: move {item.name} to {item.parent}")
        return {**item.summary(), "git": git}

    @mcp.tool()
    def update_item(
        name: str,
        category: str | None = None,
        append_body: str | None = None,
    ) -> dict:
        """Update an item: change its category and/or append a note to its body."""
        try:
            item = vault.update_item(name, category=category, append_body=append_body)
        except FileNotFoundError as e:
            return {"error": str(e)}
        git = _maybe_commit(f"inventory: update {item.name}")
        return {**item.summary(), "git": git}


mcp = _build_server()


def main() -> None:
    transport = os.environ.get("BRAIN_MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
