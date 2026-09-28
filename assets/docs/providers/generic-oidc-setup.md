# Generic OIDC Setup Guide (PingFederate, Keycloak, ForgeRock, etc.)

This guide covers setting up a generic OIDC identity provider for use with Amazon Bedrock. Use this path when your IdP is **not** Okta, Auth0, Microsoft Entra ID (Azure AD), or AWS Cognito User Pool — for example PingFederate, Keycloak, ForgeRock, or a custom OIDC-compliant deployment.

> **Why this guide exists:** Earlier versions of this solution treated the "Okta (or generic OIDC)" choice as Okta-specific. Selecting it for a non-Okta IdP failed in several places (CFN domain regex, hardcoded Okta thumbprint, Okta-only OAuth endpoint paths). The dedicated `Generic OIDC` choice in `gip init` fixes this.

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Create OIDC Application in Your IdP](#2-create-oidc-application-in-your-idp)
3. [Collect Required Information](#3-collect-required-information)
4. [Run gip init](#4-run-gip-init)
5. [Worked Example: PingFederate](#5-worked-example-pingfederate)
6. [Worked Example: Keycloak](#6-worked-example-keycloak)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Prerequisites

Your IdP must:

- Implement **OIDC 1.0** with the **Authorization Code + PKCE** flow (the credential helper does not support implicit, ROPC, or device flows).
- Issue tokens whose `iss` claim **exactly** matches the issuer URL you configure in `gip init`. AWS IAM validates this on every `AssumeRoleWithWebIdentity` call.
- Allow `http://localhost:8400/callback` as a redirect URI for your application registration.
- Expose a public JWKS endpoint (the URL is required at deploy time and at every token validation).

If your IdP also publishes `/.well-known/openid-configuration`, `gip init` will auto-discover the endpoint URLs for you. Most modern IdPs do.

---

## 2. Create OIDC Application in Your IdP

The exact UI varies by product, but every IdP needs you to configure these properties:

| Setting | Value | Notes |
|---|---|---|
| Application / client type | **Public client** (or "native", "SPA", "desktop") | Confidential client requires a client secret — only Azure AD currently supports that path here. |
| Grant types | **Authorization Code** with **PKCE** | Refresh tokens optional but recommended. |
| Redirect URI | `http://localhost:8400/callback` | Exact match. The credential helper auto-falls-back to a random port if 8400 is taken — if your IdP requires that to be allowlisted too, see [Troubleshooting](#7-troubleshooting). |
| Scopes | `openid`, `profile`, `email` | `groups` if you plan to use group-based quotas. |
| ID token signing | **RS256** or stronger | HS256 is not supported by the credential helper. |

Do **not** set a client secret. The credential helper uses PKCE instead.

---

## 3. Collect Required Information

Before running `gip init`, gather these five values:

| Value | Description | Example |
|---|---|---|
| **Issuer URL** | The exact value of the `iss` claim in tokens issued by your IdP. Must start with `https://`. | `https://auth.example.com` |
| **Client ID** | The OIDC application client ID from the IdP. | `bedrock-cli-prod` |
| **Authorization endpoint** | Full URL where the user is redirected to log in. | `https://auth.example.com/as/authorization.oauth2` |
| **Token endpoint** | Full URL the credential helper POSTs to for code exchange. | `https://auth.example.com/as/token.oauth2` |
| **JWKS URI** | Public JWKS endpoint that AWS IAM uses to validate ID tokens. | `https://auth.example.com/pf/JWKS` |

A **certificate thumbprint** for the IAM OIDC Provider is optional. When it is left unset, IAM retrieves the top intermediate CA thumbprint itself and validates the JWKS endpoint's TLS certificate against its own library of trusted root CAs ([CloudFormation reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-iam-oidcprovider.html)). You only need to supply one if your JWKS host's certificate is signed by a private CA — see [Troubleshooting](#7-troubleshooting).

> **Tip:** Most of these come straight from `{issuer}/.well-known/openid-configuration`. `gip init` queries that automatically and pre-fills the prompts.

---

## 4. Run gip init

```bash
cd source
poetry install
poetry run gip init
```

When the wizard asks "Select your identity provider type", choose **Generic OIDC (PingFederate, Keycloak, ForgeRock, etc.)**. The wizard will:

1. Prompt for the issuer URL.
2. Query `{issuer}/.well-known/openid-configuration`. If discovery succeeds, the next three prompts (authorization endpoint, token endpoint, JWKS URI) are pre-filled — confirm or override.
3. Ask for an optional certificate thumbprint. Leave it blank unless your JWKS host uses a private CA (see [Troubleshooting](#7-troubleshooting)).
4. Continue with federation type, region, model selection, etc.

The resulting profile is saved to `~/.gip/profiles/<name>.json` with these new fields:

```json
{
  "provider_type": "generic",
  "oidc_issuer_url": "https://auth.example.com",
  "oidc_authorization_endpoint": "https://auth.example.com/as/authorization.oauth2",
  "oidc_token_endpoint": "https://auth.example.com/as/token.oauth2",
  "oidc_jwks_uri": "https://auth.example.com/pf/JWKS"
}
```

If you entered a thumbprint, it is stored as `"oidc_thumbprint": "<40 hex chars>"` alongside these fields. Older profiles that already carry an `oidc_thumbprint` keep working unchanged — `gip deploy auth` still passes it to the stack.

Then deploy:

```bash
poetry run gip deploy auth
```

This applies `deployment/infrastructure/bedrock-auth-generic.yaml`, which provisions the IAM OIDC Provider with your issuer URL (and the thumbprint list, only if you set one), plus the federated role and Bedrock policy.

### Upgrading an existing stack

Stacks created by earlier releases always carry a thumbprint list on their IAM OIDC provider, and IAM rejects removing it: updating such a stack with an empty `OidcThumbprintList` fails and rolls back with `Value at 'thumbprintList' failed to satisfy constraint: Member must not be null` (verified live). `gip deploy auth` handles this automatically — before updating it reads the provider's current list (`iam:GetOpenIDConnectProvider`) and passes it back unchanged; nothing is computed. If you deploy the template by hand, read the list with `aws iam get-open-id-connect-provider --open-id-connect-provider-arn <arn>` and pass `OidcThumbprintList=<comma-separated list>` on every update. The same applies to the other `bedrock-auth-*.yaml` templates.

---

## 5. Worked Example: PingFederate

PingFederate (self-hosted or PingOne) typically uses these endpoint paths:

| Field | Value |
|---|---|
| Issuer URL | `https://<your-pingfederate-host>` |
| Authorization endpoint | `https://<host>/as/authorization.oauth2` |
| Token endpoint | `https://<host>/as/token.oauth2` |
| JWKS URI | `https://<host>/pf/JWKS` |

**In the PingFederate admin console:**

1. **Applications → OAuth → Clients** → **Add Client**.
2. Set **Client ID** (you'll enter this in `gip init`).
3. **Client Authentication** → **None** (we use PKCE).
4. **Allowed Grant Types** → check **Authorization Code**. Optional: **Refresh Token**.
5. **Redirect URIs** → `http://localhost:8400/callback`.
6. **Require Proof Key for Code Exchange (PKCE)** → check.
7. **Allowed Scopes** → `openid`, `profile`, `email`.
8. Save, then complete `gip init` using the **Generic OIDC** option. PingFederate publishes `/.well-known/openid-configuration` by default, so endpoint discovery should succeed.

---

## 6. Worked Example: Keycloak

Keycloak's URL layout is realm-scoped:

| Field | Value |
|---|---|
| Issuer URL | `https://<keycloak-host>/realms/<realm-name>` |
| Authorization endpoint | `https://<host>/realms/<realm>/protocol/openid-connect/auth` |
| Token endpoint | `https://<host>/realms/<realm>/protocol/openid-connect/token` |
| JWKS URI | `https://<host>/realms/<realm>/protocol/openid-connect/certs` |

**In the Keycloak admin console:**

1. Select your **realm**.
2. **Clients** → **Create client**.
3. **Client type** → `OpenID Connect`. **Client ID** → e.g. `bedrock-cli`.
4. Click **Next**.
5. **Client authentication** → **Off** (public client + PKCE).
6. **Standard flow** → on. **Direct access grants** → off.
7. Click **Next**.
8. **Valid redirect URIs** → `http://localhost:8400/callback`.
9. Save the client. Open it, go to the **Advanced** tab.
10. **Proof Key for Code Exchange Code Challenge Method** → `S256`.
11. Save.

`gip init` discovery will work against the realm-scoped issuer URL.

---

## 7. Troubleshooting

### `Discovery failed: HTTP 404 from .../openid-configuration`

Your IdP doesn't publish a discovery document. The wizard falls through to manual entry — populate each prompt by hand using your IdP's documentation.

### My JWKS host's TLS certificate is signed by a private CA

IAM validates the JWKS endpoint's TLS certificate against its library of trusted root CAs; configured thumbprints are used only when the certificate is not signed by one of those CAs. If your IdP's JWKS host presents a certificate from a private (corporate) CA, supply the CA thumbprint at the optional `gip init` prompt — or set `oidc_thumbprint` in `~/.gip/profiles/<name>.json` and redeploy with `poetry run gip deploy auth`. Follow the IAM guide to obtain it: [Obtain the thumbprint for an OpenID Connect identity provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html). Paste the 40 hex characters; colons and upper-case are normalized.

### `Token is not from a supported provider` from STS

The `iss` claim in your ID token does not exactly match the issuer URL configured on the IAM OIDC Provider. Common causes:

- Trailing slash mismatch (`https://auth.example.com/` vs `https://auth.example.com`). AWS treats these as different.
- Keycloak realm name mismatch (typo, wrong realm).
- IdP returns the issuer with a different host than the one you registered (e.g. internal vs external hostname).

Re-run `gip init` and pin the issuer URL to whatever your IdP literally puts in the `iss` claim. You can decode a sample token at [jwt.io](https://jwt.io) to confirm.

### `Authentication timeout - no authorization code received`

The IdP didn't redirect back to `http://localhost:8400/callback`. Check the IdP application's allowed redirect URIs. If your network blocks `localhost:8400` specifically, set the `REDIRECT_PORT` environment variable to a different port and update the IdP application accordingly — the credential helper will use that port instead.

### `Invalid nonce in ID token`

The IdP did not echo the `nonce` parameter back in the ID token. This is required by OIDC and most modern IdPs do it correctly — if you hit this, check whether your IdP needs a configuration toggle to include `nonce`.

### Where do I find the JWKS thumbprint manually?

Follow the IAM guide: [Obtain the thumbprint for an OpenID Connect identity provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html). You only need it for a private-CA JWKS host (see above).

### My IdP rotates the JWKS cert. What do I do?

If you left the thumbprint unset, nothing: IAM validates the JWKS TLS certificate against its trusted root CA library, so public-CA rotations need no action. If you configured a private-CA thumbprint, the IAM OIDC Provider accepts several — the wizard collects one; to add another, edit `~/.gip/profiles/<name>.json` and set `oidc_thumbprint` to a comma-separated list, then redeploy with `poetry run gip deploy auth`.

---

## Cost Attribution (Optional)

Per-user Bedrock costs are tracked automatically via the session tag — no IdP changes needed. For additional attribution (department, cost center, etc.), inject those values as custom claims in the ID token and they will flow through to CloudTrail via Cognito Identity Pool principal tags. See the [Cost Attribution](../COST_ATTRIBUTION.md) guide.

---

## Next Steps

After `gip init` succeeds:

```bash
poetry run gip deploy        # deploy all configured stacks
poetry run gip test    # smoke-test authentication + Bedrock invoke
poetry run gip package       # build distribution for end users
```
