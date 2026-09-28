# 0033 — Keep Claude Desktop plugin ZIP delivery non-default until authenticated

Status: Superseded by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md) · 2026-08-15

## Context

The device-code bootstrap can serve an authenticated `plugins-registry.json`,
but each entry points to a private S3 HTTPS URL. Claude Desktop does not SigV4
sign that fetch, so the ZIP returns 403. The index is metadata-only; the ZIP is
content-bearing and needs its own authenticated delivery design.

## Decision

Do not expose this as a working public-sample path. New CLI deployments do not
wire a plugin bucket into the bootstrap stack. `/bootstrap` omits
`organizationPluginsUrl` unless an operator explicitly configures a registry.
The device-code mode remains available for dynamic config; MDM filesystem
distribution is the supported Claude Desktop organization-plugin channel.

Keep the index renderer and explicit CloudFormation parameters as a dormant
integration seam. Customer docs label the path experimental and blocked.

## Alternatives considered

- **Presign in the distributor:** rejected because a static index outlives the
  URL and fails later without regeneration.
- **Make the artifact bucket public:** rejected; plugin ZIPs are executable
  organization content.
- **Proxy or presign per bootstrap request:** viable future design, but it needs
  immutable-version binding, bounded expiry, range/size behavior, auditability,
  and end-to-end Desktop proof.
- **Delete the path:** rejected for now; the authenticated index and renderer
  are tested seams for the future delivery design.

## Consequences and reopen condition

Existing profiles still parse, but CLI deployment produces dynamic config only
unless the registry is explicitly wired outside the CLI. Do not claim native
Claude Desktop plugin delivery until a client can fetch an immutable approved ZIP and
the full flow passes live Desktop validation.

Evidence: `assets/docs/PLUGINS.md:108-187`,
`deployment/legacy/lambda-functions/bootstrap_device_code/index.py`,
`source/governed_inference_platform/cli/commands/deploy.py`, and the related
bootstrap tests.
