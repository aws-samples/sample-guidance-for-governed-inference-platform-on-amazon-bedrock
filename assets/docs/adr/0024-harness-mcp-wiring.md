# ADR-0024 — Gateway MCP for every harness: native headersHelper where it exists, a Go stdio proxy where it doesn't

Status: Accepted · Wave 3, lane E-H1 · 2026-07-08 · Research: an internal research memo

## Context

The AgentCore gateway exposes platform tools (web search, memory) over
streamable-HTTP MCP with a `Bearer <id_token>` header, but only Claude
Desktop/CoWork consumed it (`managedMcpServers` + `headersHelper`,
`cowork_3p.py`). The id_token expires in ~1 h, so any harness holding a
static header loses the server mid-session. R10 surveyed the five packaged
harnesses: only Claude Code has command-based header refresh
(`headersHelper`, re-run per connection and on 401/403); OpenCode and Codex
support only static headers; Pi and Aider have no MCP client at all.

## Decision

1. **Claude Code — wire natively, config only.** `gip package` writes
   `gip-settings/mcp.json` (`{type: http, url, headersHelper}` pointing at
   the already-installed `websearch-headers` wrapper); installers register it
   idempotently via `claude mcp remove` + `add-json -s user`, guarded on the
   `claude` CLI being present.
2. **OpenCode and Codex — stdio shim.** A `--mcp-proxy <gateway-url>` mode on
   the existing Go credential-process binary speaks MCP stdio to the harness
   and forwards each request as streamable-HTTP, fetching a fresh auth header
   per outbound request through the same browserless path as
   `--get-mcp-auth-header`. Generated `opencode.json` (`type: local`) and
   `config.toml` (`[mcp_servers.agentcore-websearch]`) spawn it.
3. **Pi and Aider — skipped**, with honest one-liners in the generated
   instructions: Pi excludes MCP by design; Aider has no MCP support
   (Aider-AI/aider#2525 open since 2024).
4. **Gating mirrors CoWork everywhere:** entries are emitted only when
   `web_search_enabled`, auth is not IDC (the gateway authorizer is
   CUSTOM_JWT; SigV4 MCP auth exists in no harness), and the gateway URL
   resolves.

## Alternatives considered

- **`mcp-remote` npm bridge — REJECTED** (R10 §4.1): headers substituted once
  at process start (same 1 h expiry), refresh story is browser OAuth, and it
  adds a Node runtime prerequisite to a solution that ships self-contained Go
  binaries.
- **Static-header remote entries for OpenCode/Codex — REJECTED:** silent
  401s after token expiry; OpenCode additionally auto-launches a browser
  OAuth flow against a gateway that is not an OAuth AS.
- **Generating configs for Pi's community MCP adapter — REJECTED:** no
  dependency on third-party extensions in generated enterprise artifacts.

## Consequences

- Every MCP-capable packaged harness reaches gateway tools with per-request
  token freshness and zero new runtime dependencies; future stdio-only
  harnesses reuse the same shim.
- The proxy is deliberately request/response-only (tools/list, tools/call) —
  a future gateway tool needing server push requires extending it.
- IDC deployments get no gateway MCP in any harness; documented, not hidden.

## Evidence

`1201660` (mcp.json + installers + harness configs, +35 tests), `a700d6b`
(Go `--mcp-proxy`); matrix and setup in `assets/docs/HARNESSES.md`
§"Platform tools (MCP)"; verdicts and dated sources in R10 §§1–4.

Live validation on 2026-08-14 covered OpenCode 1.18.18 inference plus the
web-search MCP proxy, Aider 0.86.2 and Pi 0.73.1 with Sonnet 4.5, and the
expected governed Mantle deny in Codex 0.147.0. Sonnet 5 payloads were
incompatible with Aider and Pi, so generated configs use the selected model.
