# 0015 — Opt-in `sts:RoleSessionName` trust-policy binding for Direct STS

Status: Accepted · 2026-07-08 · Wave 3 lane A2 · Fixes REVIEW.md finding #2
Research: an internal research memo

## Context

In Direct STS mode (`FederationType=direct`) the client chooses the
`RoleSessionName` sent to `AssumeRoleWithWebIdentity`
(`source/go/internal/federation/sts.go`). CUR 2.0 per-user cost attribution
reads exactly that string (`line_item_iam_principal` =
`…assumed-role/Role/<session-name>`), so a modified client could attribute its
usage to any string. The repo previously documented this as unfixable:
"OIDC trust-policy condition keys cannot reference the email claim" — wrong
per current IAM docs: the **Default** OIDC claim mapping exposes `email`
(trust-policy-only) and `sub` for any IdP.

## Decision

Add an opt-in `SessionNameBinding` parameter (`none`|`email`|`sub`, default
`none` — AXIOMS A7) to all six OIDC auth templates. When set, the
`DirectIAMRole` trust policy gains
`StringEquals {"sts:RoleSessionName": "${<provider-host>:email|sub}"}` — an
IAM policy variable resolved from the IdP-signed, STS-validated JWT. STS then
rejects any assume call whose session name is not exactly the claim; a
malicious insider can only attribute usage to themselves. Default `none`
renders today's trust policy unchanged (CFN `!If`/`AWS::NoValue`), pinned by
`source/tests/test_auth_template_parity.py`. The Go client mirrors the mode
via `session_name_binding` in config.json: bound modes send the **raw** claim
(no sanitize/truncate/prefix — any rewrite would guarantee AccessDenied) and
fail closed with an actionable error on missing/invalid claims.

## Per-provider matrix

| Provider | Condition-key prefix | `email` | `sub` |
|---|---|---|---|
| Okta | `<OktaDomain>` | ✅ | ✅ |
| Azure | `login.microsoftonline.com/<tenant>/v2.0` | ⚠️ claim absent for some Entra configs/guests → fail-closed; prefer `sub` | ✅ |
| Auth0 | `<Auth0Domain>` | ✅ | ❌ `auth0\|…` contains `\|`, illegal in session names — AllowedValues restricted to `none\|email` |
| Google | `accounts.google.com` | ✅ (documented AWS example) | ✅ |
| Generic | issuer URL minus scheme | ⚠️ verify token carries claim | ⚠️ verify charset |
| Cognito pool (direct-IAM branch) | `cognito-idp.<pool-region>.amazonaws.com/<pool-id>` | ✅ live-verified 2026-08-14: legitimate email-bound session accepted, forged session name denied | ✅ template-supported; not live-tested in this run |

## Alternatives considered

- **`sts:SourceIdentity` — rejected.** (1) CUR carries no source identity;
  `line_item_iam_principal` is role + session name, so it fixes the wrong
  channel for a *cost-attribution* finding. (2) `AssumeRoleWithWebIdentity`
  has **no `SourceIdentity` request parameter** — it can only come from a
  custom JWT claim (`https://aws.amazon.com/source_identity`), i.e.
  per-customer IdP configuration this guidance cannot ship (Google public
  OIDC cannot add it at all). (3) Adds `sts:SetSourceIdentity` trust-policy
  churn for no CUR benefit. (R7 §3.)
- **Default-on binding — rejected.** Locks out email-less users and old
  clients on stack update (violates A7 / backwards-compat rule).
- **Unconditional `aud` pin for okta/azure/auth0/generic — deferred.** Real
  hardening (only google pins `aud` today) but can break IdPs that set
  `azp` ≠ client ID; ship separately (R7 §6 bonus, risk R4).

## Consequences

- `email` mode: old clients keep working for typical enterprise emails
  (legacy sanitizer is a no-op); `sub` mode requires the new client fleet-wide
  before flipping the parameter (fail-closed lockout otherwise). Migration
  order documented in COST_ATTRIBUTION.md.
- Case-sensitive `StringEquals` and email churn can split CUR rows (no
  lockout); `sub` mode is immune.
- Auth0 trailing-slash provider URL (R7 risk R5): the condition-key prefix
  assumes the stored IAM provider name is the domain without trailing slash —
  verify against `aws iam list-open-id-connect-providers` on first live
  deploy.
- The 2026-08-14 live result covers Cognito `email` binding only. Okta and the
  other provider rows remain untested against live STS.

## Evidence

- Templates: `deployment/infrastructure/bedrock-auth-*.yaml` (SessionNameBinding
  parameter, `BindSession*` conditions, DirectIAMRole trust-policy `!If`).
- Parity: `source/tests/test_auth_template_parity.py` (EXCEPTIONS assert the
  exact per-provider policy-variable expressions).
- Client: `source/go/internal/federation/sts.go` (`buildSessionName` modes),
  `sts_test.go`; config plumbing `internal/config/config.go`,
  `governed_inference_platform/config.py`, `deploy.py`, `package.py`,
  `init_answers.py`; tests `tests/cli/commands/test_session_name_binding.py`.
