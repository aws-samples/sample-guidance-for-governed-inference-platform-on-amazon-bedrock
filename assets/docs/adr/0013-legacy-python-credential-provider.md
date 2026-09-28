# ADR-0013: Legacy Python credential provider — deprecate in place, do not remove

Status: Proposed · Date: 2026-07-08

## Context

Two credential-process implementations coexist: the Go default since June 2026
(`source/go/cmd/credential-process/`) and the original Python module
(`source/credential_provider/__main__.py`, ~119KB). Wave 3 lane E-L1 audited
every consumer to decide the deprecation path.

## Evidence: still shipped, still load-bearing

1. `gip package --legacy` builds from the Python module via PyInstaller/Nuitka
   (`source/governed_inference_platform/cli/commands/package.py:335-337,1488,1582,1681,1777`).
2. Missing/old Go (<1.24) silently falls back to the legacy build — no flag
   needed (`package.py:343-358`). Admin machines without Go ship Python today.
3. Poetry packages it and declares the `credential-provider` entry point
   (`source/pyproject.toml:22,29`).
4. CodeBuild compiles it for Windows (Nuitka) and Linux x64/arm64 (PyInstaller)
   (`deployment/infrastructure/codebuild-windows.yaml:176,257,333`).
5. ~15 test files import or inspect it; parity contracts pin Go and Python to
   the same behavior (`source/tests/test_credential_process_contract.py`,
   `test_mcp_auth_header.py`, `test_quota_warning_parity.py`). Go code cites it
   as the behavioral reference (`source/go/internal/oidc/confidential.go:42`,
   `source/go/internal/config/config.go:85`).
6. Upstream `origin/beta` committed to it on 2026-07-08 (`5541818`, #762) and
   2026-06-26 (`7410e92`, #651) — actively maintained, so deletion guarantees
   merge conflicts on every sync (violates axiom A1, upstream-diffable).

## Decision

Deprecate in place. No removal, no file moves, no behavior change.
Docstring deprecation note + ARCHITECTURE.md/CHANGELOG mentions only.
Feature freeze: new credential-process features land in Go first; the Python
module receives only security/upstream-synced fixes and parity-test upkeep.
Parity obligation: while both ship, contract suites run against both; breaking
Go/Python parity is a regression even if the Go side alone is correct.

## Removal conditions (upstream proposal, not a fork action)

`source/credential_provider/` is upstream-owned and upstream-active; removal is
proposable only as an upstream PR, and only when all of: (1) upstream drops the
`--legacy` flag and the Go-missing fallback in `package.py`; (2) Go covers every
auth mode (OIDC secret + certificate, IDC, passthrough) for two consecutive
releases with no parity divergence; (3) parity tests are re-anchored to golden
fixtures so the suite survives deletion. Until then the module ships as-is.

## Alternatives considered

- **Delete now** — rejected: shipped on two paths, upstream-active same day as
  this audit, ~15 importing test files; violates A1.
- **Stop packaging, keep source** — rejected: structural change to
  upstream-owned build paths; the fallback exists for admins without Go.
- **Runtime stderr deprecation warning** — rejected: SDKs surface
  credential_process stderr on failure paths; changes observable behavior.
