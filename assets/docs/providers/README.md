# Identity Provider Setup Guides and Requirements Contract

This folder contains the per-IdP setup guides and, below, the **IdP Requirements
Contract**: the single-page specification of everything the platform requires
from an identity provider. If your IdP is not listed, follow the
[Generic OIDC guide](generic-oidc-setup.md) plus this contract — any
OIDC-compliant IdP (PingFederate, Keycloak, ForgeRock, JumpCloud, ...) works.

## Setup guides

| Identity Provider | Guide |
|---|---|
| Okta | [okta-setup.md](okta-setup.md) |
| Microsoft Entra ID (Azure AD) | [microsoft-entra-id-setup.md](microsoft-entra-id-setup.md) |
| Auth0 | [auth0-setup.md](auth0-setup.md) |
| Google | [google-oidc-setup.md](google-oidc-setup.md) |
| Amazon Cognito User Pool | [cognito-user-pool-setup.md](cognito-user-pool-setup.md) |
| IAM Identity Center (IDC) | [iam-identity-center-setup.md](iam-identity-center-setup.md) |
| Any other OIDC IdP | [generic-oidc-setup.md](generic-oidc-setup.md) + this contract |

## IdP Requirements Contract

The setup guides tell you how to click through a specific IdP; this contract
tells you **what the platform actually consumes from tokens and why**. A
Keycloak (or any generic-OIDC) administrator should be able to configure a
fully-attributed deployment from this page plus the generic guide alone.

### Hard requirements (login fails without these)

| Requirement | Detail | Error when violated |
|---|---|---|
| OIDC 1.0, Authorization Code + PKCE | Public client, **no client secret**. Implicit, ROPC, and device flows are not supported | Login flow fails at the IdP |
| ID token signing RS256 or stronger | HS256 not supported | Token validation failure |
| Exact `iss` match + public JWKS | The IAM OIDC Provider pins the issuer; the JWKS endpoint must be publicly reachable | STS: `Token is not from a supported provider` |
| `aud` = your client ID | Must match the IAM OIDC Provider's `ClientIdList` | STS: `Incorrect token audience` / `InvalidIdentityToken` |
| Redirect URI `http://localhost:8400/callback` | Exact match; the helper falls back to a random port if 8400 is taken (see the [generic guide troubleshooting](generic-oidc-setup.md#7-troubleshooting)) | IdP refuses to redirect |
| Nonce echo | The IdP must echo the `nonce` parameter back in the ID token (OIDC-required) | `Invalid nonce in ID token` |
| `sub` claim | OIDC-mandatory anyway | — |

### Claims contract (what each claim drives)

Three tiers: **hard** (above), **required for attribution**, and
**optional-feature**. Column three is the honest answer to "what breaks
without it".

| Claim | Consumed by | Tier | What breaks without it |
|---|---|---|---|
| `email` | STS session name (`source/go/internal/federation/sts.go:98-120`) → `line_item_iam_principal` in CUR 2.0 (per-user cost attribution); quota-check identity; OTEL `x-user-email` dimension | **Required for attribution** | Session name falls back to `sub` — cost attribution degrades to an opaque ID. Quota behavior is governed by the `MISSING_EMAIL_ENFORCEMENT` knob (`deployment/infrastructure/lambda-functions/quota_check/index.py:20`): `block` (default) denies credentials to email-less tokens; `warn` lets them through with degraded attribution |
| `groups` **or** `cognito:groups` | Quota group entitlements (`quota_check/index.py:293-333` accepts both claim names, as an array or comma-separated string) | Optional-feature | Group quota policies never match; every user falls to the default policy. Cognito User Pools emit `cognito:groups`; most other IdPs need a claim mapping to emit `groups` — both work unmodified |
| `custom:department` / `department` | Quota pseudo-group `department:<x>` (`quota_check/index.py:331`); OTEL `x-department` dimension | Optional-feature | No department-level quota or dashboard dimension |
| `name` | Cognito principal tag `UserName` (Cognito-mode auth stack) | Optional | Tag empty |
| `team`, `cost_center`, `organization_id`, `location`, `role`, `manager` | otel-helper → collector attribution dimensions (`source/go/internal/otel/headers.go:4-16`) | Optional-feature | Those dashboard dimensions are absent |
| `https://aws.amazon.com/tags` (nested, or flattened per-key) | STS session tags → CloudTrail/CUR (see [COST_ATTRIBUTION.md](../COST_ATTRIBUTION.md)) | Optional-feature | Tag-based cost attribution off; per-user attribution via session name still works |
| Refresh token (`offline_access` scope) | Silent refresh | Optional | Browser re-login every session instead of 7-30 day silent renewal |

### Scopes to request per feature

| Feature | Scopes |
|---|---|
| Baseline login + attribution | `openid profile email` |
| Silent refresh | + `offline_access` |
| Group quota entitlements | + `groups` (or your IdP's equivalent claim mapping) |

### Conformance checklist

Before opening a support ticket, decode a real ID token from your IdP (for
example at jwt.io) and check:

1. [ ] `iss` exactly matches the issuer URL you gave `gip init` (scheme, host, path, no trailing-slash mismatch)
2. [ ] `aud` equals your client ID
3. [ ] Header `alg` is `RS256` or stronger (not `HS256`)
4. [ ] `email` is present and populated
5. [ ] `groups` (or `cognito:groups`) is present if you configured group quota policies
6. [ ] A `nonce` value from the auth request is echoed in the token

If all six pass and login still fails, see the
[generic guide's troubleshooting section](generic-oidc-setup.md#7-troubleshooting).

### Note for IDC (IAM Identity Center) deployments

IDC mode does not use this OIDC contract: identity is the IAM ARN, quota
authentication is SigV4, and OTEL attribution flows via STS. See
[iam-identity-center-setup.md](iam-identity-center-setup.md).
