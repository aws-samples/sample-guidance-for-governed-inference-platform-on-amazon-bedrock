# Example Organization Plugin

This directory demonstrates a source bundle for an organization-managed Claude
plugin.

## What it shows

- **`.claude-plugin/plugin.json`** — Plugin manifest with name, version, and
  installation preference.
- **`skills/code-review/SKILL.md`** — An organization-wide skill that enforces
  a code-review checklist.

## Delivery

The retired GIP `/plugins` bootstrap route is not used. Package this directory
according to your review process and distribute it through the filesystem/MDM
mechanism documented by the exact pinned AWS Samples gateway source:

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway-bootstrap/`

Keep deployment policy outside this example. The upstream README defines the
current Desktop directories and client contract; refresh it with
`scripts/sync-claude-apps-gateway.sh` rather than adding a local network
delivery protocol.
