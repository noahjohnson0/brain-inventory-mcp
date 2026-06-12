<table border="0" cellspacing="0" cellpadding="0">
<tr>
<td width="220" valign="middle">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.png">
<img src="docs/assets/logo-light.png" alt="brain-inventory-mcp" width="200">
</picture>
</td>
<td valign="middle"><h1>B&nbsp;R&nbsp;A&nbsp;I&nbsp;N &nbsp; I&nbsp;N&nbsp;V&nbsp;E&nbsp;N&nbsp;T&nbsp;O&nbsp;R&nbsp;Y &nbsp; M&nbsp;C&nbsp;P</h1></td>
</tr>
</table>

Talk to your **physical inventory** through Claude. This is a small [MCP](https://modelcontextprotocol.io)
server that turns the `inventory/` folder of an [Obsidian](https://obsidian.md) vault
into a set of tools Claude can call, so you can ask *"where's my passport?"* or say
*"add a USB-C cable to the under-desk drawer"* and have it read and edit the actual
Markdown notes, by text or by **voice from the Claude phone app**.

Your vault stays the source of truth. Every change edits the real notes (and can
auto-commit them to git). No database, no cloud copy of your data, no third-party
auth provider: the server hosts its **own single-user OAuth 2.1**.

```
  Claude (phone / web / desktop, voice or text)
        │  remote MCP connector (OAuth 2.1 + PKCE)
        ▼
  public HTTPS (Cloudflare / Tailscale tunnel)
        ▼
  brain-inventory-mcp  ──edits──▶  Obsidian vault /inventory/*.md  ──▶ git
```

## Why

Note-taking apps are great at *capturing* where your stuff is, and terrible at
*conversational* retrieval and updates. An LLM is the opposite. MCP bridges the two:
the model gets typed tools over your notes, and you get to manage a hundred physical
items by just talking. Because the backend is plain Markdown in your own vault, you
keep full ownership and can still browse/edit everything in Obsidian.

## Tools

| Tool | What it does |
|---|---|
| `search_inventory(query)` | Fuzzy search across name, category, parent, and body |
| `list_inventory(category?, parent?)` | List items, optionally filtered |
| `get_item(name)` | Full frontmatter + body for one item |
| `add_item(name, category?, parent?, body?, is_container?)` | Create a new item |
| `move_item(name, new_parent)` | Re-parent an item into another container |
| `update_item(name, category?, append_body?)` | Change category / append a note |

### Vault conventions

Each item is a Markdown note with YAML frontmatter and a body:

```markdown
---
name: "Anker Nano 6-Port Charger"
category: Electronics
is_container: false
parent: "[[Desk]]"
created: 2026-05-15
updated: 2026-05-15
tags: [inventory]
---

# Anker Nano 6-Port Charger

GaN desk charger. Lives on the [[Desk]].
```

Containment is `[[wikilink]]`-based: a child note points at its container via
`parent`, and a container sets `is_container: true`. Writes preserve this shape and
stamp a fresh `updated:` date. The inventory folder name defaults to `inventory`
(override with `BRAIN_INVENTORY_DIR`).

## Quickstart (local, no auth)

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone https://github.com/<you>/brain-inventory-mcp
cd brain-inventory-mcp
export BRAIN_VAULT="~/Documents/Obsidian/my-vault"   # your vault root
uv run brain-inventory-mcp                            # stdio transport
```

Register it with Claude Code:

```bash
claude mcp add brain-inventory \
  -e BRAIN_VAULT="$HOME/Documents/Obsidian/my-vault" \
  -- uv run --directory "$PWD" brain-inventory-mcp
```

Or in Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "brain-inventory": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/brain-inventory-mcp", "brain-inventory-mcp"],
      "env": { "BRAIN_VAULT": "/path/to/your/vault" }
    }
  }
}
```

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `BRAIN_VAULT` | — | **Required.** Obsidian vault root |
| `BRAIN_INVENTORY_DIR` | `inventory` | Subfolder holding item notes |
| `BRAIN_MCP_AUTOCOMMIT` | `1` | `0` to skip `git commit`/`push` on writes |
| `BRAIN_MCP_TRANSPORT` | `stdio` | `stdio` (local) or `http` (remote/phone) |
| `BRAIN_MCP_HOST` | `127.0.0.1` | HTTP bind host |
| `BRAIN_MCP_PORT` | `8848` | HTTP port |
| `BRAIN_MCP_PUBLIC_URL` | — | Public HTTPS base, e.g. `https://xyz.example.com`. **Required in HTTP mode** |
| `BRAIN_MCP_PASSWORD` | — | Shared password for the OAuth login page. **Required in HTTP mode** |
| `BRAIN_MCP_INSECURE` | — | `1` disables OAuth (loopback testing only) |
| `BRAIN_MCP_STATE` | `~/.config/brain-inventory-mcp/oauth.json` | Where OAuth clients/tokens persist (0600) |

See [`.env.example`](.env.example).

## Remote access + the Claude phone app

To use it from the Claude app, the server must be reachable over the public internet,
because Claude calls custom connectors **from Anthropic's cloud, not from your phone**.
And Claude custom connectors authenticate **only via OAuth 2.1**, with no static API
keys or bearer headers ([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)).
So this server is its own authorization server.

### Security model (self-hosted OAuth 2.1)

- Claude registers via **Dynamic Client Registration**, then runs **authorization-code
  + PKCE (S256)**. The MCP SDK verifies PKCE.
- `/authorize` bounces the browser to a **password page** (`/login`); the password is
  `BRAIN_MCP_PASSWORD`, compared in constant time. No password, no token.
- Access tokens (1h) and refresh tokens (30d, rotated on every use) are opaque random
  strings persisted to a `0600` file. Unauthenticated `/mcp` calls return `401`.
- The HTTP transport **refuses to start** without a password (override only with
  `BRAIN_MCP_INSECURE=1` for loopback testing).
- DNS-rebinding protection stays on; the configured `BRAIN_MCP_PUBLIC_URL` host is
  added to the allowlist (see [Troubleshooting](#troubleshooting)).

### Run it

```bash
BRAIN_VAULT="$HOME/Documents/Obsidian/my-vault" \
BRAIN_MCP_TRANSPORT=http \
BRAIN_MCP_PUBLIC_URL=https://your-host.example.com \
BRAIN_MCP_PASSWORD='something-strong' \
uv run brain-inventory-mcp
# MCP endpoint: https://your-host.example.com/mcp
```

### Expose a public HTTPS URL

OAuth needs a **stable** hostname. A [Cloudflare](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
quick tunnel is fine to try it out:

```bash
cloudflared tunnel --url http://127.0.0.1:8848   # prints a https://*.trycloudflare.com URL
```

but its hostname changes on every restart, which invalidates OAuth registrations. For
anything permanent, use a **named Cloudflare tunnel on a domain you control** (fixed
hostname), or Tailscale Funnel, and set `BRAIN_MCP_PUBLIC_URL` to it.

### Add it in Claude

1. On **claude.ai** (web): Settings → Connectors → **`+` → Add custom connector**.
   The "add custom connector" button is web/desktop only; once added it appears in the
   phone app too. (Requires a Pro/Max/Team/Enterprise plan.)
2. Paste `https://your-host.example.com/mcp`, leave the OAuth fields blank (DCR handles
   them), click **Add**.
3. Claude opens the `/login` page → enter your password → the handshake completes.
4. In a chat, enable the connector and use **voice mode**. Set the read-only tools
   (`search_inventory`, `list_inventory`, `get_item`) to *Always allow* for smooth
   voice; keep the write tools on *Needs approval*.

### Keep it running (macOS launchd)

A LaunchAgent restarts the server on login/reboot. Example
`~/Library/LaunchAgents/com.example.brain-inventory-mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.brain-inventory-mcp</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string><string>--directory</string>
    <string>/path/to/brain-inventory-mcp</string>
    <string>brain-inventory-mcp</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BRAIN_VAULT</key><string>/path/to/your/vault</string>
    <key>BRAIN_MCP_TRANSPORT</key><string>http</string>
    <key>BRAIN_MCP_PUBLIC_URL</key><string>https://your-host.example.com</string>
    <key>BRAIN_MCP_PASSWORD</key><string>REPLACE_WITH_STRONG_PASSWORD</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

The plist holds the password in plaintext, so keep it private:

```bash
chmod 600 ~/Library/LaunchAgents/com.example.brain-inventory-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.brain-inventory-mcp.plist
```

## Testing

```bash
# Full OAuth flow: DCR -> authorize -> login -> token -> authed call, plus
# negative cases (no token, wrong password, bad PKCE) and refresh rotation.
# Start a loopback server first:
BRAIN_VAULT="$HOME/Documents/Obsidian/my-vault" \
BRAIN_MCP_TRANSPORT=http BRAIN_MCP_PORT=8861 \
BRAIN_MCP_PUBLIC_URL=http://127.0.0.1:8861 \
BRAIN_MCP_PASSWORD='hunter2-test' BRAIN_MCP_AUTOCOMMIT=0 \
uv run brain-inventory-mcp &

uv run --group dev python test_oauth_flow.py
```

## Troubleshooting

- **`421 Misdirected Request` after a successful login.** The MCP SDK auto-enables
  DNS-rebinding protection when bound to localhost and only trusts loopback hosts, so a
  tunnel's `Host` header is rejected. This server adds `BRAIN_MCP_PUBLIC_URL`'s host to
  the allowlist; make sure that variable exactly matches the public URL Claude uses.
- **"Authorization with the MCP server failed."** Usually a stale connector from an
  earlier attempt. Delete the connector in Claude and re-add it.
- **No "Add custom connector" button.** It is web/desktop only and gated to paid plans.
- **Voice mode doesn't trigger tools.** Confirm the tool works in *text* mode first; if
  text works and voice doesn't, it's a Claude voice-mode limitation, not the server.

## How it works

`vault.py` is the storage layer: parse/format YAML frontmatter, locate the vault, and
read/search/create/update notes with targeted edits that preserve human formatting.
`server.py` exposes those as MCP tools via the SDK's `FastMCP`. `auth.py` implements
the SDK's `OAuthAuthorizationServerProvider` protocol (DCR, authorization-code + PKCE,
opaque tokens with refresh rotation, persisted to a 0600 file) plus the `/login` page,
so the whole authorization server lives in-process. The SDK mounts the standard OAuth
endpoints and verifies PKCE.

## Roadmap

- Optional read-only mode / per-tool scopes.
- A fully local voice front-end ([Pipecat](https://github.com/pipecat-ai/pipecat):
  Deepgram STT + Claude + TTS) that reuses these tools, for hands-free use at the
  machine without the phone.
- Photo capture → item note (vision) for faster intake.

## License

MIT. See [LICENSE](LICENSE).
