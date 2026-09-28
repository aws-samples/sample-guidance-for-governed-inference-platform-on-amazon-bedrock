# Upstream Claude Apps Gateway source

This directory pins, but does not track, two subtrees from the canonical AWS
Samples repository recorded in `UPSTREAM.json`:

- `claude-apps-gateway/`
- `claude-apps-gateway-bootstrap/`

`UPSTREAM.json` is the pin file: it records the repository, exact commit,
per-subtree Git tree ids, and the versioned `git-tree-sha1-v1` verification
format. Upstream remains authoritative; this repository does not carry a local
fork.

## Materialize and verify

From the repository root, use the commands exposed by the fetch script:

```bash
scripts/fetch-claude-apps-gateway.sh
scripts/fetch-claude-apps-gateway.sh --verify
scripts/fetch-claude-apps-gateway.sh --clean
```

The first command fetches the exact pinned commit, verifies each upstream tree
id, materializes both gitignored subtrees with executable bits and symlinks
preserved, then reconstructs and compares their Git tree ids. It fails closed
and removes partial output on a mismatch. `--verify` performs the same check
offline; `--clean` removes both trees. Do not edit materialized files: content,
path, regular-file/symlink type, executable mode, symlink-target, missing/extra
entry, and build-output changes make offline verification fail.

`git-tree-sha1-v1` builds an isolated temporary Git object database and index;
it does not touch this repository's index or follow symlinks. Git tree entries
cover relative path bytes, mode (`100644`, `100755`, or `120000`), and the blob
bytes (file content or symlink target). FIFOs, devices, sockets, Gitlinks, and
empty directories are rejected. This is exact Git object-identity checking,
not a publisher signature or attestation; Git records only executable vs.
non-executable mode, and the host filesystem must support the pinned names,
symlinks, and executable bit.

**Network limitation:** materialization requires `git`, `jq`, and access to
`github.com`. Fetch and verify the source before entering a network-isolated
build environment; `--verify` and `--clean` do not fetch from the network.

## Security scanning of the fetched bytes

Git tree ids prove that the materialized paths, types, modes, symlink targets,
and bytes equal the reviewed pin; they do not prove that those bytes are free of
security findings. Scan the materialized upstream source with your own tooling
before you deploy it, and re-scan after every pin change.

## Review, updates, and rollback

`scripts/sync-claude-apps-gateway.sh` proposes a new pin (it rewrites
`UPSTREAM.json` and this directory's `LICENSE` copy only, and rejects a
non-regular or symlinked upstream `LICENSE`). The weekly sync
workflow materializes the proposed pin, runs the upstream tests and
`npm audit`, and opens a review pull request that links the upstream diff; it
never deploys or auto-merges upstream changes. Dependency fixes must come from
upstream and arrive in a subsequent pinned sync.

Reviewers inspect the linked upstream diff and the workflow results before a pin
change is merged.
To roll back, restore the previous reviewed
`UPSTREAM.json` and matching `LICENSE`, run the `--clean` command above, then
run the fetch command again. See
[ADR-0036](../../../assets/docs/adr/0036-verified-fetch-claude-apps-gateway.md)
and the [Apps Gateway guide](../../../assets/docs/APPS_GATEWAY.md).

The upstream project is a worked example, not an AWS or Anthropic supported
production artifact. Its own README and license remain authoritative.
Passing its static tests proves pin integrity and upstream contracts, not a
live customer IdP/MDM/Desktop deployment.
