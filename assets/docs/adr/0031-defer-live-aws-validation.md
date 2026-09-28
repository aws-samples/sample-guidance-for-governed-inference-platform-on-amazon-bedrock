# ADR-0031: Defer live AWS validation (V1a–V6) and ledger the gap

Status: Accepted (recorded retroactively 2026-07-29; owner decision made 2026-07-13)

Progress note (2026-08-14): the deferred plan has now been substantially
executed in a fresh Cognito OIDC sandbox, including all-stack deployment and
multiple positive and resolved-negative runtime findings. This ADR is not
superseded: LV-1 billing readback remains payer-blocked, LV-3 passed
independently, LV-13 is superseded by exact upstream contract ownership, Okta coverage remains open, and the LV-17
terminal sweep is pending. The current ledger is
[LIVE_VALIDATION.md](../LIVE_VALIDATION.md).

## Context

The Wave 3 release-hardening worker produced a live-validation plan covering the
runtime claims that only real AWS behavior can prove
(an internal review record). On 2026-07-13 the owner
chose **"Defer and ledger"**: the plan became an execution backlog, not
evidence (`rh-live-validation-plan.md:7-10`). The decision was recorded only in
that plan and the closeout archive (an archived internal ledger)
— no ADR, a breach of axiom A6 identified in the internal decision track record
(P3, section 8 item 2). This ADR repairs the record.

## Decision

Defer the live validations and ship on local gates (pytest, go test, cfn-lint,
ruff, mkdocs) plus review. Deferred items (an archived internal ledger):

- **V1a** — AIP cost-allocation tag flow through CUR/Cost Explorer
- **V2** — Mantle tripwire alarm fire/recover
- **V3** — cache-token metering fidelity (blocks metering strict mode)
- **V1e** — packaged `claude -p` end-to-end inference
- **V4** — collector-kill chaos (central mode)
- **V5** — quota-API-kill chaos (fail-closed issuance)
- **V6** — terminal sandbox sweep

V1b (CloudWatch `ModelId` dimension for AIPs) was completed read-only after
closeout on 2026-07-14 (an archived internal ledger).

## Alternatives considered

1. **Execute the plan before closeout** — not taken; estimated ~1 elapsed day
   over 2–3 calendar days and ~$5–10, with several R2 (owner-confirmation)
   mutations in a shared sandbox (`rh-live-validation-plan.md:231-260`).

## Consequences

- **All runtime claims rest on local gates.** Every "enforced / metered /
  alarmed" statement is grade B (locally gated) or C (assumption-bearing), not
  A (live-proven) — the ranked meta-weakness in
  the internal decision track record and the reason its section 7
  unverified-assumption register exists.
- The backlog remains open for the residual items listed in the progress note.

## Evidence

- Plan + disposition: an internal review record.
- Ledger: an archived internal ledger,
  an archived internal ledger (A7 pending, ordered_checks V1a–V6).
- Breach identification: the internal decision track record P3, section 8 item 2.
