# Tool discovery

How the agent finds, inspects, and loads tools. This is the discovery half of
the tool registry (design [§6](../agents/design.md)); the builtins-vs-tools split
that frames *when* the agent reaches for a tool at all is also in §6.

## The problem

A tool is a Python module of functions, and they all share one registry —
built-ins, installed integrations, MCP servers, and learned skills. At scale that
catalog is large (one MCP server alone can expose dozens of functions), so naive
discovery has four failure modes:

- **Wrong keyword → zero results.** A pure substring match has no synonyms, so
  "send a message" never finds a tool summarized *"Slack integration"* — and a
  silent empty result reads as "this capability doesn't exist."
- **Too many results → context flood.** Dumping every matching tool's full
  function list defeats the whole point of keeping the orchestrator's context
  small.
- **Overlapping tools.** `github` / `gitlab` / `gitea` all match "git" with
  nothing to say which to prefer.
- **No common-first tiering.** Everything is flat; the long tail buries the few
  tools actually used day to day.

## The design (built)

Discovery is **two levels**, so the unit you search over (compact headers) is
separate from the unit you read in full (one tool's interface).

### 1. `search_tools(query="", include_all=False) → headers`

Returns **ranked headers only** — never function signatures:

```
# slack — Slack integration  [installed · chat · featured]
# github — GitHub API  [installed · vcs · not loaded]
# calc — Safe arithmetic evaluation.  [core · math · featured · 1 fn]

+4 more — narrow the query or call search_tools("git", include_all=True).
describe_tool(name) shows a tool's functions; use_tool(name) loads it.
```

- **Ranked & capped.** Matches are scored (exact-name > name substring > keyword
  hit > summary hit) and capped at a small limit, with a `+N more` hint. Featured
  tools break ties so common tools sort to the top.
- **Common-first, defer the tail.** An empty query lists only `featured` tools —
  the common set. `include_all=True` (or the query `"*"`) lifts both the
  featured-only default and the cap, surfacing the long tail on demand.
- **Keyword/alias matching.** Matching runs over name + summary + a curated
  `keywords` tuple + `category`, so intent words ("message", "im") find a tool
  even when they appear in neither its name nor its summary. Tools declare these
  at registration, or on the module as `__keywords__` / `__category__` /
  `__featured__`.
- **No dead ends.** A query that matches nothing falls back to the featured set
  with a note, rather than an empty `(no matching tools)`.
- **Side-effect-free.** Headers come entirely from registration metadata, so
  **search never connects a lazy tool.** Browsing the catalog can't be slowed or
  broken by a down MCP server; an unconnected tool simply shows `not loaded`.

### 2. `describe_tool(name) → one interface`

Expands the single chosen tool to its full interface — each public function's
signature and first docstring line:

```
# slack — Slack integration  [installed · chat]
    send_message(channel: str, text: str)  # Post a message to a channel.
    list_channels()  # List channels the bot can see.
```

This is where a lazy tool is **resolved** (an MCP server is connected); a server
that is down is reported as `unavailable: …` rather than raising. `use_tool(name)`
then returns the live module to call.

### Why two levels

The agent scans cheap headers, picks one, and pays for the full interface of only
that one. Discovery cost is bounded by the *number of tools* (one line each), not
their *total surface area* (every function of every match). It also moves the one
expensive, fallible step — connecting a server — from "browse" to "commit," where
it belongs.

## Planned (D): ranking by readiness, and semantic search

Two improvements deferred until the catalog is large enough to need them. Both
slot behind the existing `search()` seam without changing the agent-facing
`search_tools → describe_tool → use_tool` flow.

### D1 — Vault/policy-aware ranking and grouping

Lexical relevance ignores whether a tool is actually *usable right now*. The
registry already sits alongside the vault (§5) and policy (§9), so search can
boost tools that are **ready** and group **substitutes**:

- **Readiness boost.** A tool whose required secret is present in the vault, and
  whose calls don't require an approval, ranks above one that needs setup. If only
  the Slack token is in the vault, Slack wins the "send a message" query — the
  single most useful disambiguator for overlapping vendors.
- **Category grouping.** Substitutes that share a `category` collapse into one
  group so the agent sees them as alternatives, not as three unrelated hits:
  `chat: slack ✓configured · discord ⚠needs-auth · teams ⚠needs-auth`.

This needs the registry to read (never expose) vault key *names* and to ask policy
for a tool's would-be decision. Both are read-only seams that already exist.

### D2 — Semantic routing as an optional backend

Lexical matching still misses intent that shares no tokens with a tool's metadata.
Add a semantic backend behind the same `search()` interface (mirroring how the
vault swaps backends), opt-in, with lexical as the default:

- **Embeddings index.** Embed each tool's name + summary + keywords once; rank by
  vector similarity to the query. Best recall; adds a dependency and an index to
  keep in sync as tools are mounted.
- **Cheap-LLM router** *(more native to this harness).* The harness already
  delegates bulk work to the cheap tier. Feed the query plus the one-line catalog
  of *all* tools (small even at hundreds of entries) to `llm(tier="cheap")` and
  get back the top-k tool names. It handles synonyms, intent, dedup, and ranking
  in a single call. Cost: one cheap call per search, and nondeterminism — hence
  opt-in, not the default.

Vault/policy-aware ranking (D1) is the higher-value, lower-cost step and should
come first; semantic routing (D2) follows when lexical recall is the bottleneck.
