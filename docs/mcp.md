# MCP server wrapping

> Implements the "MCP-server wrapping into tool modules" item from
> [`../agents/design.md`](../agents/design.md) §6. Read that section first; this
> doc explains the built result.

## The one idea

An MCP server is just **another source of tools**. pyharness connects to it,
asks what tools it has, and **generates a plain Python module** — one function
per remote tool — that it drops into the normal `Registry`. From that moment the
MCP tools are indistinguishable from built-ins: the agent finds them with
`search_tools()` and calls them with `use_tool()`, writing ordinary Python.

**MCP never becomes an interface the agent sees.** The agent does not learn a
"call an MCP tool" verb, does not see JSON-RPC, does not see a transport, and
does not see tool schemas as JSON. It sees `weather.get_current(city="NYC")` —
a function with a real signature and docstring. MCP is plumbing behind the
registry.

```
 local server (subprocess)        remote server (HTTPS)
   stdio JSON-RPC                   Streamable HTTP
        │                                │
   StdioTransport                   HttpTransport
        └────────────┬───────────────────┘
                MCPClient            ← parent-side; the agent never holds it
                     │  initialize / tools/list / tools/call
              build_module()         ← schema → typed Python functions
                     │
              types.ModuleType "weather"
                     │  registry.register(module, source="installed")
                     ▼
                 Registry  ──►  search_tools("weather")   # agent sees the interface
                          ──►  use_tool("weather")         # agent gets the module
                                  weather.get_current(city="NYC")
```

## Local vs cloud servers

The only difference is the **transport**; everything above it is identical.

| | Local | Cloud / remote |
|---|---|---|
| Transport | `StdioTransport` | `HttpTransport` (MCP *Streamable HTTP*) |
| How it's reached | run as a subprocess; newline-delimited JSON-RPC over stdin/stdout | HTTP POST to one endpoint; reply is JSON or an SSE stream |
| Declared by | `command` (+ `args`) | `url` |
| Credentials | `env` passed to the process | `headers` (e.g. `Authorization`) |

A `command` means local, a `url` means remote — you never name a transport
directly. Both produce the same generated tool module and both flow through the
same broker gating (below).

> **Reaching a cloud server that only offers stdio.** Many hosted servers ship a
> local stdio *bridge* (e.g. `npx -y mcp-remote https://…`). That's just a local
> `command`, so it works today even though pyharness only speaks Streamable HTTP
> natively. The legacy HTTP+SSE two-endpoint transport is **not** implemented.

## Adding a server

### A. In code

```python
# local
session.registry.add_mcp_server(
    "weather", "npx", ("-y", "@modelcontextprotocol/server-weather"),
    env={"WEATHER_API_KEY": "..."},
)

# remote
session.registry.add_mcp_server(
    "github", url="https://mcp.example.com/mcp",
    headers={"Authorization": "Bearer ..."},
)
```

`add_mcp_server` connects, lists the tools, generates the module, registers it
with `source="installed"`, and remembers the client so `Registry.close()` (called
by `Session.close()`) shuts the connection/subprocess down.

### B. From a config file (recommended)

Declare servers in the standard `mcpServers` shape — the same one Claude Desktop
and `.mcp.json` use:

```json
{
  "mcpServers": {
    "weather": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-weather"],
      "env": {"WEATHER_API_KEY": "secret:weather_key"}
    },
    "github": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer secret:github_token"}
    }
  }
}
```

Mount it on a session:

```python
session = Session(root, mcp_config="mcp.json")   # path or a dict
```

The **CLI auto-loads `.mcp.json`** from the working directory (override with
`PYHARNESS_MCP_CONFIG`).

#### Lazy, tolerant mounting

Config mounting is **lazy by default**: each server is *registered* but not
contacted until the agent first searches or uses it. Two consequences:

- **Startup never blocks or fails on a server.** A slow or down server can't
  delay session creation, and one that's misconfigured can't abort it — you only
  pay the connection cost for servers the agent actually reaches.
- **Failures are graceful and local.** If a server can't be reached when first
  used, `search_tools()` lists it as `(unavailable: <reason>)` instead of
  breaking the search, and `use_tool()` raises a clear error the agent sees in
  its cell traceback and can route around — other tools are unaffected. A later
  call retries, so a server that recovers starts working without a restart.

`secret:NAME` credentials are still resolved through the vault **at mount time**
(in the parent), so a missing secret is reported up front as a config error even
though the connection itself is deferred.

Pass `lazy=False` to `mount_config(...)` to connect eagerly and fail fast
instead — useful when you want startup to verify every server is reachable.
Programmatic `registry.add_mcp_server(...)` is always eager (it connects now).

#### Credentials: `secret:NAME` → Vault

Any `env` or `headers` value of the form `secret:NAME` is resolved through the
session `Vault` **in the parent**, at mount time. So no cleartext credential
lives in the config file, and the resolved secret is attached to the server's
process env / HTTP headers parent-side — it never enters the agent's address
space (design §5). A `secret:` reference with no vault available is an error, not
a silent empty value.

## Why it also works out-of-process, for free

The generated functions hold the live `MCPClient`, which lives in the **parent**.
So when the agent runs in the restricted child (`out_of_process=True`):

- `use_tool("weather")` returns a `RemoteToolSpec` over the wire (a live module
  can't be pickled), and the child rebuilds a proxy module — the existing
  mechanism, no MCP-specific code.
- Each `weather.get_current(...)` in the child routes back to the parent as
  `tools.invoke("weather", "get_current", ...)`, which runs the generated
  function parent-side — i.e. the actual MCP call (subprocess or HTTPS) happens
  in the parent, **gated by the same policy → audit → budget chokepoint** as
  every other capability.

So an MCP tool call is policed, audited, and metered like any other side effect,
and the server (and any credentials it uses) never enters the agent's address
space — true for both local and cloud servers.

## How a tool becomes a function

For each tool descriptor from `tools/list`, `build_module` synthesizes a Python
function:

- **Signature** from the JSON Schema `inputSchema`: required properties become
  required parameters, optional ones get their schema `default` (or are omitted
  from the call entirely if unset), JSON types map to Python annotations
  (`string→str`, `integer→int`, …). It carries a real `__signature__`, so
  `Registry.search()` prints a true interface.
- **Docstring** = the tool's description plus a `:param:` line per property.
- **Body** binds the arguments and forwards them to `client.call_tool(name, args)`,
  which returns the tool's `structuredContent` if present, else the joined text
  of its content blocks (raising `MCPError` on a tool error).

## Files

```
pyharness/tools/mcp/
  transport.py   # Transport protocol; StdioTransport (local), HttpTransport (cloud); MCPError
  client.py      # MCPClient (transport-agnostic), connect(), wrap_mcp_server()
  module.py      # schema -> typed Python functions (build_module)
  config.py      # load_config(), mount_config() (lazy by default), secret:NAME -> Vault
pyharness/tools/registry.py   # register()/register_lazy()/add_mcp_server()/close(); source on ToolInfo
pyharness/core/session.py     # Session(mcp_config=...); closes connections on teardown
tests/test_mcp.py             # stdio round trip, generated signatures, remote-seal
tests/test_mcp_remote.py      # HTTP transport, remote registry mount, config + secret resolution
tests/mcp_server_fake.py      # a minimal in-repo MCP stdio server for tests
```

## Limits (V1)

- Transports: local **stdio** and remote **Streamable HTTP** only. The legacy
  HTTP+SSE transport is not implemented.
- The client is synchronous; the HTTP transport has a request timeout, the stdio
  transport does not — a wedged local server can hang the cell.
- Only MCP **tools** are surfaced (not resources or prompts), which is what maps
  onto pyharness's tool model.
