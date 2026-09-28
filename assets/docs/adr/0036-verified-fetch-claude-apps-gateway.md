# ADR-0036: Deliver the pinned Claude Apps Gateway source by verified fetch, not a committed mirror

Status: Accepted · Date: 2026-09-02 · Hardened: 2026-09-03 · Supersedes the delivery mechanism of [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md)

## Context

ADR-0035 committed byte-for-byte mirrors of the `claude-apps-gateway` and
`claude-apps-gateway-bootstrap` subtrees of `aws-samples/anthropic-on-aws`
under `vendor/aws-samples/anthropic-on-aws/`. Security scanning reports
findings inside those mirrored files (container base images outside ECR Public and a
CDK construct setting), which could not be suppressed locally without patching
upstream files — the fork ADR-0035 forbids.
Nothing in the build, tests, or docs build consumes the mirrored files
(`mkdocs.yml`; `source/tests/test_gateway_upstream_mirror.py` checked presence
only); they exist so customers can `cd` into a reviewed, pinned copy.

The mirror had also drifted silently: a secrets-hygiene pass rewrote two example
JWT secrets in `claude-apps-gateway/cdk/README.md` and
`cdk/scripts/test-gateway.sh`, so the committed tree (`f3bcf327`) no longer
matched the tree id recorded in `UPSTREAM.json` (`f724a2aa`).

The first verified-fetch implementation hashed only regular-file bytes. Its
offline check therefore accepted executable-bit changes and added or retargeted
symlinks. The source-checkout tree-id check happened before copy and could not
prove that the materialized output retained the same paths, types, and modes.

## Decision

- Stop tracking the two subtrees; gitignore their paths. `UPSTREAM.json` keeps
  repository, exact commit, commit date, and per-subtree Git tree id, and records
  `verification.format: git-tree-sha1-v1`. The upstream commit remains
  `49a637a8f8f249c4715a37179302fa0c49729b41`; this is a metadata-only migration
  from the incomplete regular-file SHA-256 manifest.
- `git-tree-sha1-v1` reconstructs a Git tree from the materialized filesystem in
  an isolated temporary object database and index. It disables Git content
  filters and line-ending conversion, forces ignored files into the temporary
  index, and compares the resulting tree id with `UPSTREAM.json.paths`. Git tree
  entries deterministically cover raw relative-path bytes, regular-file mode
  (`100644` or `100755`), symlink mode (`120000`), regular-file blob bytes, and
  symlink-target bytes. Nested tree objects represent directories. Traversal
  uses `find` without following symlinks; FIFOs, devices, sockets, Gitlinks,
  empty directories, and entries Git cannot represent are rejected.
- `scripts/fetch-claude-apps-gateway.sh` still uses plain `git clone` and
  `git fetch` of the canonical repository at the exact pin (sparse checkout of
  the two subtrees). It checks each upstream tree id, reconstructs the checked
  out tree, copies with executable modes and symlinks preserved, then
  reconstructs the copied output. Any mismatch removes all output written by
  that fetch and exits non-zero. `--verify` performs the same output check
  offline; `--clean` removes both trees; `--print-tree-id` exposes the
  reconstruction for pin generation and diagnosis. No operation reads or
  mutates the parent repository's index.
- `scripts/sync-claude-apps-gateway.sh` proposes a pin: it writes only
  `UPSTREAM.json` and the `LICENSE` copy, after rejecting a symlink or any other
  non-regular upstream `LICENSE`. The weekly workflow materializes the proposed
  pin, runs the upstream tests, and opens or force-updates a review PR.
  After either path it edits the PR title and body so the compare URL and tested
  commit cannot remain stale. Pull requests touching the pin or scripts run the
  fetch, offline verify, and pinned `sync --check` in CI.
- Intent of ADR-0035 is unchanged: no local fork, upstream authoritative, exact
  pinned commit, `gip deploy gateway|bootstrap` stays retired, tested PR-only
  pin bumps, never auto-merge or auto-deploy.

## Alternatives considered

- Git submodule at the pinned commit: ZIP downloads ship empty dirs.
- Patch `FROM` lines to `public.ecr.aws` in the sync: cannot clear the
  distroless or CDK findings and creates the permanent fork ADR-0035 forbids.
- Exact-commit archive URL + published SHA-256 (no script): equivalent
  transport with weaker verification; the sparse checkout already existed.
- Immutable GitHub release asset with attestation: follow-up pending repo policy.

## Consequences

A fresh clone no longer contains the upstream source; deploying needs `git`,
`jq`, and github.com access, which the CDK deploy already requires. What the
repository reviews is the pin, not the bytes: reviewers follow the compare URL
in the sync PR. Rollback is restoring the previous reviewed `UPSTREAM.json` and
`LICENSE`, running `--clean`, then fetching again. Materialized trees are local,
mutable state; `--verify` now distinguishes content, path, file type, executable
mode, symlink target, missing entry, and extra entry changes.

The check proves equality to Git's SHA-1 object identity; it is not a publisher
signature, transparency proof, or freshness guarantee. Trust still rests on the
reviewed canonical repository/commit and Git transport. Git modes distinguish
only executable from non-executable regular files, not full POSIX permissions.
The host filesystem must preserve the pinned path bytes, symlinks, and executable
bit; the scripts target Bash on Linux/macOS, not native Windows filesystems.

Identity also does not imply clean content: scan the materialized upstream source
with your own tooling before you deploy it, and re-scan after every pin change.

## Evidence

`vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`;
`scripts/fetch-claude-apps-gateway.sh`; `scripts/sync-claude-apps-gateway.sh`;
`.github/workflows/check-claude-apps-gateway-mirror.yml`;
`.github/workflows/sync-claude-apps-gateway.yml`;
`source/tests/test_gateway_upstream_mirror.py`; drift: see the CHANGELOG (Security hardening).
