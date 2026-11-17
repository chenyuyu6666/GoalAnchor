# GoalAnchor MCP server

GoalAnchor exposes its deterministic gates over the **Model Context Protocol**, so
a host can call them without installing a plugin. MCP is a published protocol;
a plugin binds to a host's private extension API, which PI-Desktop 0.22.0 already
broke three times (per-turn runtime rebuild, composer slash routing, palette
`RUNTIME_NOT_FOUND`).

## What the server offers

| Tool | Purpose |
|---|---|
| `goalanchor_gate` | Validate one record with one gate (`plan`, `verify`, `execute`, `plan-change`, `evidence`, `conclusion`, `experiment`, `design`, `artifact`, `artifact-file`, `provenance`, `closure`, `review`, `review-cycle`, `graphify`) |
| `goalanchor_capabilities` | Report version, protocol versions, gate list and the honest boundary |

Both are read-only. **Neither grants execution permission, reviews with a model,
nor intercepts host tools.** A gate PASS means the record satisfies that gate's
rules and nothing more.

## Transport

Newline-delimited JSON-RPC 2.0 over stdio. Zero third-party dependencies — the
server only uses the Python standard library, because it has to run inside
whatever interpreter the host already ships.

Protocol versions accepted: `2025-06-18` (default), `2025-11-25`, `2025-03-26`,
`2024-11-05`. An unknown offer falls back to the default instead of failing the
handshake.

## Register with a host

### ZCode (project scope)

ZCode reads project config from `<workspace>/.zcode/config.json` (or
`zcode.json`), discovered upward to the git root. Copy
[zcode-project-config.json](zcode-project-config.json) and merge its `mcp` block
into that file:

```json
{
  "mcp": {
    "servers": {
      "goalanchor": {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "goalanchor.mcp_server"],
        "env": {}
      }
    }
  }
}
```

ZCode also honours `<workspace>/.agents/mcp.json` when the project config declares
no MCP servers, using the `mcpServers` shape below.

### Any MCP host (generic)

Copy [goalanchor.mcp.json](goalanchor.mcp.json), whose shape is the common one:

```json
{
  "mcpServers": {
    "goalanchor": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "goalanchor.mcp_server"],
      "env": {}
    }
  }
}
```

Use an absolute interpreter path when the host does not inherit your PATH.

## Verify by hand

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python -m goalanchor.mcp_server
```

A working server answers `id:1` with `serverInfo.name = "goalanchor"` and `id:2`
with the two tool specs.

## Boundaries

- **No tool interception.** MCP lets a host call *this* server; it does not let
  this server veto the host's own tool calls. Blocking an unapproved write still
  needs a host-side gate (PI-Desktop plugin) or a host that routes its approval
  decision through MCP.
- **No model review.** `goalanchor_review`/`review-cycle` need a model transport
  the MCP surface does not carry; use the CLI (`python -m goalanchor
  review-cycle`) or the host's own completion API.
- **No persistence.** Every call is stateless; the server writes only a temporary
  file to hand a record to the gate and removes it.
