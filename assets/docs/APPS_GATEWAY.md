# Claude Apps Gateway on AWS

Claude Code and Claude Desktop use the same Claude Apps Gateway inference and
policy plane. This repository does **not** maintain a separate gateway
implementation. It pins the AWS Samples worked example to an exact upstream
commit and materializes it unchanged from:

<https://github.com/aws-samples/anthropic-on-aws/tree/main/claude-apps-gateway>

The pin is `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`: repository,
commit, each subtree's Git tree id, and the versioned `git-tree-sha1-v1`
verification format. The subtrees themselves are not tracked in this repository
([ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md));
`scripts/fetch-claude-apps-gateway.sh` fetches the pinned commit with plain
`git`, reconstructs the materialized paths/types/modes/targets/bytes as Git
trees, verifies their ids, and materializes:

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway/`

The upstream README, CDK application, tests, deployment scripts, architecture,
operational caveats, and configuration reference are authoritative. The sample
is reference material, not an AWS or Anthropic supported production artifact.

## Deploy

Materialize the pinned source first, then follow the upstream instructions
without local patches:

```bash
scripts/fetch-claude-apps-gateway.sh   # needs git, jq, and github.com access; exits non-zero on any mismatch
cd vendor/aws-samples/anthropic-on-aws/claude-apps-gateway/cdk
cp .env.example .env
# Fill the values documented by upstream.
npm ci
./scripts/deploy.sh
```

The fetch refuses to overwrite an existing materialized tree. To confirm a tree
is still the pinned upstream bytes run `scripts/fetch-claude-apps-gateway.sh
--verify` (your `.env`, `node_modules/`, and `cdk.out/` make it report a
difference, which is expected after a deploy); to start over run `--clean`
and fetch again.

`gip deploy gateway` is intentionally retired so local CloudFormation
parameters cannot drift from the upstream CDK. Use the upstream CDK and its
operations scripts for deploy, status, and teardown; GIP does not guess the
upstream stack's region or name.

## Claude Desktop

Claude Desktop is not a separate inference implementation:

1. The same gateway handles identity, policy, inference, spend controls, and
   telemetry.
2. A matching gateway policy opts Desktop in with a `desktop` block.
3. Desktop reads `<gateway>/user/bootstrap` when `bootstrapUrl` is delivered by
   MDM or imported configuration.

Use the gateway-native Desktop overlay documented in the upstream README when
that contract is sufficient. Use the AWS Samples companion add-on when you need
the separate PKCE configuration/MCP overlay, materialized by the same fetch:

`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway-bootstrap/`

That add-on consumes the existing upstream gateway; it does not replace or
modify the inference plane.

## Updates

Propose a new pin, then materialize and verify it:

```bash
scripts/sync-claude-apps-gateway.sh       # rewrites UPSTREAM.json (and the LICENSE copy) for upstream main
scripts/fetch-claude-apps-gateway.sh --clean
scripts/fetch-claude-apps-gateway.sh      # fetches the new pin; fails closed on any mismatch
```

The weekly `sync-claude-apps-gateway.yml` workflow does the same, executes the
upstream CDK and shell tests against the materialized pin, and opens a review
pull request that changes only `UPSTREAM.json` (plus the `LICENSE` copy) and
links the upstream diff. It never auto-merges or auto-deploys upstream changes.
Pull requests that touch the pin or the scripts run
`check-claude-apps-gateway-mirror.yml`, which fetches and verifies the pin.

**Rollback.** The pin is the only state: check out the previous
`vendor/aws-samples/anthropic-on-aws/UPSTREAM.json` (for example
`git checkout <previous-commit> -- vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`),
run `scripts/fetch-claude-apps-gateway.sh --clean`, then
`scripts/fetch-claude-apps-gateway.sh` to materialize the prior upstream
commit. A newer pin can synthesize a real CloudFormation delta for an existing
gateway stack; run `cdk diff` from the materialized directory before deploying
an update, and keep the previous pin available for the rollback window.

## Migrating a legacy GIP stack

The previous local CloudFormation gateway and bootstrap templates have been
removed from the repository (not included in this sample). To
retrieve the template of a deployed legacy stack, use
`aws cloudformation get-template --stack-name <name>`; teardown needs no
template file (`aws cloudformation delete-stack --stack-name <name>`). Do not
attempt an in-place CloudFormation-to-CDK update of the stateful
PostgreSQL-backed gateway.

1. Record the legacy stack outputs, client configuration, IdP settings, and
   current database identifier. Create and verify a manual pre-cutover RDS
   snapshot in addition to the template's snapshot-on-delete policy.
2. Register separate upstream OIDC callbacks and deploy the upstream CDK on a
   separate private hostname. Do not reuse the production hostname during the
   pilot.
3. Treat gateway sessions, device grants, and spend state as non-migrating.
   The new PostgreSQL store starts independently; do not copy internal tables
   between implementations.
4. Move a pilot cohort's MDM settings to the upstream hostname. Validate fresh
   sign-in, inference, policy selection, telemetry, spend controls, Desktop
   `/user/bootstrap`, and any companion PKCE/MCP overlay. Keep the legacy stack
   and hostname unchanged through a defined rollback window.
5. Cut over remaining MDM settings only after the pilot passes. Rollback means
   restoring the prior MDM hostname while the legacy service and IdP callbacks
   are still available.
6. After the rollback window, remove the legacy bootstrap stack first, then the
   legacy gateway. `gip destroy bootstrap` and `gip destroy gateway` verify the
   live template signature before deletion. Confirm the final RDS snapshot is
   available and restorable before removing remaining rollback state.

Upstream stacks, build buckets, CodeBuild projects, and other external build
artifacts follow the upstream teardown order and are never deleted by GIP.
