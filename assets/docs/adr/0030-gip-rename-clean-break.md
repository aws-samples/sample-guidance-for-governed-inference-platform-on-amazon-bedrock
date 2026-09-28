# ADR-0030: Wave 6 rebrand — clean-break rename `ccwb` → `gip`, no compat shims

Status: Accepted (recorded retroactively 2026-07-29; decision executed in Wave 6, 2026-07-27)
Supersedes: ADR-0011's "identifiers unchanged" decision, and the no-rename clause of
axiom A1 (the internal project operating axioms), per the axiom-supersession rule in
the internal project operating axioms.

## Context

ADR-0011 repositioned the project editorially while deciding that the repository
slug, package names, CLI name (`ccwb`), and stack names stay unchanged
(`0011-platform-first-positioning.md:26-28`). Wave 6 reversed that: the project
was rebranded "Claude Code with Bedrock"/`ccwb` → "Governed Inference Platform
on Amazon Bedrock"/`gip` under the contract in the internal rename contract, executed
across nine lanes (the internal delivery ledger, wave6 W6-GO through W6-GUI). No
superseding ADR was written at the time — a breach of axiom A6 flagged in
the internal decision track record (F4, P4, section 8 item 6). This ADR repairs
the record; it does not relitigate the decision.

## Decision

Rename every project-owned identifier per the internal rename contract (authoritative
contract): CLI/acronym `gip`, Python package `governed_inference_platform`, Go
module `gip-go`, repo slug `guidance-for-governed-inference-platform-on-amazon-bedrock`,
plus runtime state (`~/.gip/`, keyring service, env vars) and AWS resource
defaults (Lambda names, namespaces, log groups, tags). Clean break: no
backwards-compat shims and no dual-read of old paths, env vars, or keyring
entries (the internal rename contract).

Anthropic product contracts are explicitly excluded via the DO-NOT-RENAME
allowlist (the internal rename contract): `claude_code.*` OTEL metrics and event
schema, `CLAUDE_CODE_*`/`ANTHROPIC_*` env vars the products read,
`service.name: claude-code`, Cowork MDM/ADMX artifacts, product names in prose,
and product-scoped template filenames such as `claude-apps-gateway.yaml`.

## Alternatives considered

1. **Keep identifiers unchanged (the ADR-0011 status quo)** — rejected by the
   Wave 6 owner contract: the project's own branding changes while the platform
   continues to support Claude Code/Desktop as harnesses (internal delivery record).
2. **Rename with compat shims / dual-read of old paths** — rejected; the
   contract mandates a clean break, with legacy `.ccwb-config` migration code
   deleted or retargeted to the new path only (internal delivery record).

## Consequences (accepted costs)

- **Prebuilt-binary download path broken by construction** until releases are
  published under the new repo slug; the release repo became an overridable
  constant (`GIP_RELEASE_REPO`) defaulting to the new slug (the internal rename contract).
- **Existing deployments, keyring entries, and client paths orphaned by
  design** — no dual-read means prior installs are not migrated (internal delivery record).
- **Upstream diffability reduced**, reversing the rationale that ADR-0011 and
  axiom A1 were built on (the internal decision track record).
- Follow-up: the A1 axiom text in the internal project operating axioms still reads "no renames
  of the `ccwb` CLI" and needs amendment to reflect this supersession (out of
  scope for this record-repair lane).

## Evidence

- Contract of record: the internal rename contract; ledger: the internal delivery ledger wave6.
- Breach identification: the internal decision track record F4, P4, section 8 item 6.
- Superseded decision: `assets/docs/adr/0011-platform-first-positioning.md:26-28`.
