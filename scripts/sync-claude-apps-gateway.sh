#!/bin/bash
set -euo pipefail

canonical_upstream=https://github.com/aws-samples/anthropic-on-aws.git
upstream=$canonical_upstream
ref=main
check_only=false
verification_format=git-tree-sha1-v1

usage() {
    cat <<'EOF'
Usage: scripts/sync-claude-apps-gateway.sh [--check] [--ref REF] [--source REPOSITORY]

Proposes a new pin for the official AWS Samples Claude Apps Gateway and Desktop
bootstrap subtrees. Resolves REF (default: main) in the canonical repository and
records the commit, commit date, subtree tree ids, and versioned
git-tree-sha1-v1 verification format in
vendor/aws-samples/anthropic-on-aws/UPSTREAM.json; refreshes the LICENSE copy. It
writes nothing else and rejects a symlink or non-regular upstream LICENSE.
Materialize and verify the pin with
scripts/fetch-claude-apps-gateway.sh (ADR-0036).

--check exits non-zero when the committed UPSTREAM.json or LICENSE differs from
the pin computed for REF (use --ref <pinned commit> to validate the current pin).
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            check_only=true
            shift
            ;;
        --ref)
            ref=${2:?--ref requires a value}
            shift 2
            ;;
        --source)
            upstream=${2:?--source requires a value}
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$upstream" != "$canonical_upstream" ] && [ "$check_only" != true ]; then
    printf '%s\n' "--source is allowed only with --check; write-mode syncs must use the canonical repository." >&2
    exit 2
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
destination="$repo_root/vendor/aws-samples/anthropic-on-aws"
fetch_script="$repo_root/scripts/fetch-claude-apps-gateway.sh"
work=$(mktemp -d)
local_worktree=false
cleanup() {
    if [ "$local_worktree" = true ]; then
        git -C "$upstream" worktree remove --force "$work/source" >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

if [ -d "$upstream/.git" ]; then
    git -C "$upstream" worktree add --quiet --detach "$work/source" "$ref"
    local_worktree=true
else
    git clone --quiet --filter=blob:none --no-checkout "$upstream" "$work/source"
    git -C "$work/source" sparse-checkout init --cone
    git -C "$work/source" sparse-checkout set claude-apps-gateway claude-apps-gateway-bootstrap
    git -C "$work/source" fetch --quiet --depth 1 origin "$ref"
    git -C "$work/source" checkout --quiet --detach FETCH_HEAD
fi

if [ ! -f "$work/source/LICENSE" ] || [ -L "$work/source/LICENSE" ]; then
    printf 'Upstream LICENSE must be a regular file, not a symlink or unsupported filesystem type.\n' >&2
    exit 1
fi

commit=$(git -C "$work/source" rev-parse HEAD)
commit_date=$(git -C "$work/source" show -s --format=%cI HEAD)
gateway_tree=$(git -C "$work/source" rev-parse HEAD:claude-apps-gateway)
bootstrap_tree=$(git -C "$work/source" rev-parse HEAD:claude-apps-gateway-bootstrap)
gateway_materialized=$("$fetch_script" --print-tree-id "$work/source/claude-apps-gateway")
gateway_materialized=${gateway_materialized%% *}
bootstrap_materialized=$("$fetch_script" --print-tree-id "$work/source/claude-apps-gateway-bootstrap")
bootstrap_materialized=${bootstrap_materialized%% *}
if [ "$gateway_materialized" != "$gateway_tree" ] || [ "$bootstrap_materialized" != "$bootstrap_tree" ]; then
    printf '%s\n' "Checked-out source cannot be represented exactly by $verification_format." >&2
    exit 1
fi

cat >"$work/UPSTREAM.json" <<EOF
{
  "repository": "$canonical_upstream",
  "commit": "$commit",
  "commit_date": "$commit_date",
  "verification": {
    "format": "$verification_format"
  },
  "paths": {
    "claude-apps-gateway": "$gateway_tree",
    "claude-apps-gateway-bootstrap": "$bootstrap_tree"
  }
}
EOF

if [ "$check_only" = true ]; then
    status=0
    diff -u "$destination/UPSTREAM.json" "$work/UPSTREAM.json" || status=1
    cmp -s "$work/source/LICENSE" "$destination/LICENSE" || { printf 'LICENSE differs from upstream.\n'; status=1; }
    if [ "$status" -ne 0 ]; then
        printf 'Claude Apps Gateway pin differs from %s at %s.\n' "$canonical_upstream" "$commit" >&2
        exit 1
    fi
    printf 'Claude Apps Gateway pin matches %s at %s.\n' "$canonical_upstream" "$commit"
    exit 0
fi

mkdir -p "$destination"
cp "$work/UPSTREAM.json" "$destination/UPSTREAM.json"
cp "$work/source/LICENSE" "$destination/LICENSE"
printf 'Pinned Claude Apps Gateway upstream at %s in %s.\n' "$commit" "$destination/UPSTREAM.json"
printf 'Materialize it with scripts/fetch-claude-apps-gateway.sh\n'
