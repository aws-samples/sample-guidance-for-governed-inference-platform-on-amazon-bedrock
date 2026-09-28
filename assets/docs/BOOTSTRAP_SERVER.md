# Claude Desktop bootstrap

The locally maintained GIP bootstrap servers are retired. Claude Desktop
configuration now follows the exact AWS Samples Claude Apps Gateway contracts.

## Default: gateway-native Desktop bootstrap

The same Claude Apps Gateway used by Claude Code serves Claude Desktop at
`/user/bootstrap`. Add a `desktop` block to the matching upstream gateway
policy and deliver `bootstrapUrl` through MDM or Desktop's import flow. See
(after `scripts/fetch-claude-apps-gateway.sh` has materialized the pinned source):

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway/README.md`

## Optional companion add-on

For a separate PKCE configuration/MCP overlay, use the upstream companion
application unchanged (materialized by the same fetch):

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway-bootstrap/`

Canonical source:

<https://github.com/aws-samples/anthropic-on-aws/tree/main/claude-apps-gateway-bootstrap>

The add-on attaches to the deployed gateway's existing ALB. Inference,
sessions, policy, spend controls, and telemetry remain gateway responsibilities.

## What GIP no longer does

- `gip deploy bootstrap` does not create a parallel Lambda/API Gateway
  implementation.
- GIP does not translate upstream Desktop keys or managed MCP shapes.
- GIP packaging does not synthesize gateway login/bootstrap settings from
  legacy profile fields.
- Network plugin delivery from the retired `/plugins` route is not part of the
  selected upstream contract; use the delivery mechanism documented upstream.

The legacy templates and Lambda code have been removed from the repository
(not included in this sample). Identify a deployed legacy stack with
`aws cloudformation get-template --stack-name <name>` and remove it with
`aws cloudformation delete-stack --stack-name <name>` — teardown needs no
template file. Propose a new upstream pin with
`scripts/sync-claude-apps-gateway.sh` and materialize it with
`scripts/fetch-claude-apps-gateway.sh`; pin bumps arrive through a tested review PR.
