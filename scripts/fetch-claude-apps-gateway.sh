#!/bin/bash
# Materialize the pinned AWS Samples Claude Apps Gateway subtrees (ADR-0036).
#
# The upstream subtrees claude-apps-gateway/ and claude-apps-gateway-bootstrap/
# are not tracked in this repository. This script fetches them from the
# canonical repository at the exact commit recorded in
# vendor/aws-samples/anthropic-on-aws/UPSTREAM.json, verifies each subtree's
# git tree id against a tree reconstructed from the materialized entries, and
# fails closed: on any mismatch it removes what it wrote and exits non-zero.
#
# Network access is plain `git clone`/`git fetch` of the canonical repository.
# Offline verification uses an isolated temporary Git object database and index;
# it never reads or mutates this repository's index.
set -euo pipefail

canonical_upstream=https://github.com/aws-samples/anthropic-on-aws.git
subtrees=(claude-apps-gateway claude-apps-gateway-bootstrap)
verification_format=git-tree-sha1-v1

usage() {
    cat <<'EOF'
Usage: scripts/fetch-claude-apps-gateway.sh [--verify | --clean | --print-tree-id [DIR...]]

Materializes claude-apps-gateway/ and claude-apps-gateway-bootstrap/ under
vendor/aws-samples/anthropic-on-aws/ at the commit pinned in UPSTREAM.json.
The fetch reconstructs each materialized subtree as a Git tree and compares its
tree id with UPSTREAM.json. It removes the materialized trees if anything differs.

  (no flag)        fetch, verify, and materialize both subtrees (needs network)
  --verify         verify already-materialized subtrees against UPSTREAM.json (offline)
  --clean          remove the materialized subtrees
  --print-tree-id  print the reconstructed Git tree id of each DIR (default: both subtrees)
  -h, --help       show this help

Verification format: git-tree-sha1-v1. It records relative path bytes, Git mode
(100644/100755/120000), regular-file bytes, and symlink target bytes. Directories
are represented by nested trees. Unsupported filesystem types and empty
directories are rejected; symlinks are never followed.

Exit codes: 0 verified, 1 verification or fetch failure, 2 usage, 3 missing tool.
EOF
}

mode=fetch
dirs=()
set_mode() {
    if [ "$mode" != fetch ]; then
        printf 'Use one of --verify, --clean, --print-tree-id at a time.\n' >&2
        exit 2
    fi
    mode=$1
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --verify)
            set_mode verify
            shift
            ;;
        --clean)
            set_mode clean
            shift
            ;;
        --print-tree-id)
            set_mode print
            shift
            dirs=("$@")
            break
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

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
vendor_dir="$repo_root/vendor/aws-samples/anthropic-on-aws"
pin_file="$vendor_dir/UPSTREAM.json"

need_tool() {
    command -v "$1" >/dev/null 2>&1 && return 0
    printf 'Missing required tool: %s\n' "$1" >&2
    exit 3
}

git_tree_id_of() (
    # Build a raw Git tree from a work tree in an isolated temporary repository.
    # `git add` is forced to include ignored paths; info/attributes disables all
    # byte transformations. Git uses lstat/readlink, so symlinks are not followed.
    local dir=$1 scratch object_format tree fs_count=0 index_count=0 entry mode rest
    if [ ! -d "$dir" ] || [ -L "$dir" ]; then
        printf 'Not a real directory: %s\n' "$dir" >&2
        return 1
    fi

    scratch=$(mktemp -d)
    trap 'rm -rf "$scratch"' EXIT
    mkdir -p "$scratch/home" "$scratch/xdg"
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
        GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_CONFIG \
        GIT_CONFIG_GLOBAL GIT_TEMPLATE_DIR
    export GIT_CONFIG_COUNT=0

    if ! (cd "$dir" && find . ! -type d ! -type f ! -type l -print0 >"$scratch/unsupported"); then
        printf 'Could not enumerate %s safely.\n' "$dir" >&2
        return 1
    fi
    if [ -s "$scratch/unsupported" ]; then
        printf 'Unsupported filesystem entry under %s; only directories, regular files, and symlinks are allowed.\n' "$dir" >&2
        return 1
    fi
    if ! (cd "$dir" && find . -mindepth 1 -type d -empty -print0 >"$scratch/empty-directories"); then
        printf 'Could not inspect directories under %s safely.\n' "$dir" >&2
        return 1
    fi
    if [ -s "$scratch/empty-directories" ]; then
        printf 'Unsupported empty directory under %s; Git trees represent directories through entries.\n' "$dir" >&2
        return 1
    fi
    if ! (cd "$dir" && find . \( -type f -o -type l \) -print0 >"$scratch/filesystem-entries"); then
        printf 'Could not enumerate entries under %s safely.\n' "$dir" >&2
        return 1
    fi
    while IFS= read -r -d '' entry; do
        fs_count=$((fs_count + 1))
    done <"$scratch/filesystem-entries"
    if [ "$fs_count" -eq 0 ]; then
        printf 'No Git entries under %s\n' "$dir" >&2
        return 1
    fi

    if ! HOME="$scratch/home" XDG_CONFIG_HOME="$scratch/xdg" GIT_CONFIG_NOSYSTEM=1 \
        GIT_ATTR_NOSYSTEM=1 GIT_DEFAULT_HASH=sha1 git init --quiet "$scratch/repo"; then
        printf 'Could not initialize isolated Git verification state.\n' >&2
        return 1
    fi
    printf '%s\n' '* -text -crlf -eol -filter -ident -working-tree-encoding' \
        >"$scratch/repo/.git/info/attributes"

    raw_git() {
        HOME="$scratch/home" XDG_CONFIG_HOME="$scratch/xdg" GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 \
            git --git-dir="$scratch/repo/.git" --work-tree="$dir" \
            -c core.autocrlf=false \
            -c core.filemode=true \
            -c core.symlinks=true \
            -c core.ignorecase=false \
            -c core.precomposeunicode=false \
            "$@"
    }

    object_format=$(raw_git rev-parse --show-object-format 2>/dev/null || printf 'sha1')
    if [ "$object_format" != sha1 ]; then
        printf 'Isolated Git verification requires SHA-1 object format, got %s.\n' "$object_format" >&2
        return 1
    fi
    if ! raw_git add --force --all -- .; then
        printf 'Could not index materialized entries under %s.\n' "$dir" >&2
        return 1
    fi
    if ! raw_git ls-files --stage -z >"$scratch/index-entries"; then
        printf 'Could not inspect isolated Git verification state.\n' >&2
        return 1
    fi
    while IFS=' ' read -r -d '' mode rest; do
        case "$mode" in
            100644|100755|120000) ;;
            *)
                printf 'Unsupported Git mode %s under %s.\n' "$mode" "$dir" >&2
                return 1
                ;;
        esac
        index_count=$((index_count + 1))
    done <"$scratch/index-entries"
    if [ "$index_count" -ne "$fs_count" ]; then
        printf 'Git could represent %s of %s materialized entries under %s; refusing incomplete verification.\n' \
            "$index_count" "$fs_count" "$dir" >&2
        return 1
    fi
    if ! tree=$(raw_git write-tree); then
        printf 'Could not reconstruct Git tree under %s.\n' "$dir" >&2
        return 1
    fi
    printf '%s' "$tree"
)

read_pin() {
    local value
    if ! value=$(jq -er "$1" "$pin_file"); then
        printf 'UPSTREAM.json (%s) is missing %s\n' "$pin_file" "$1" >&2
        exit 1
    fi
    printf '%s' "$value"
}

require_hex() {
    local value=$1 width=$2 what=$3
    if ! printf '%s' "$value" | grep -Eq "^[0-9a-f]{$width}\$"; then
        printf 'UPSTREAM.json %s is not a %s-character lowercase hex id: %s\n' "$what" "$width" "$value" >&2
        exit 1
    fi
}

require_verification_format() {
    local recorded
    recorded=$(read_pin .verification.format)
    if [ "$recorded" != "$verification_format" ]; then
        printf 'UPSTREAM.json verification.format is %s; this script supports only %s.\n' \
            "$recorded" "$verification_format" >&2
        exit 1
    fi
}

verify_materialized() {
    # Reconstruct and compare each materialized subtree with its pinned Git tree. Offline.
    local ok=true subtree expected actual
    require_verification_format
    for subtree in "${subtrees[@]}"; do
        expected=$(read_pin ".paths[\"$subtree\"]")
        require_hex "$expected" 40 "paths.$subtree"
        if [ ! -d "$vendor_dir/$subtree" ] || [ -L "$vendor_dir/$subtree" ]; then
            printf '%s: not materialized (run scripts/fetch-claude-apps-gateway.sh)\n' "$subtree" >&2
            ok=false
            continue
        fi
        if ! actual=$(git_tree_id_of "$vendor_dir/$subtree"); then
            ok=false
            continue
        fi
        if [ "$actual" != "$expected" ]; then
            printf '%s: Git tree id mismatch\n  expected %s\n  actual   %s\n' "$subtree" "$expected" "$actual" >&2
            printf '  The materialized tree is not the pinned upstream paths, types, modes, targets, and bytes\n' >&2
            printf '  (local edits, build output,\n' >&2
            printf '  or a stale pin). Run: scripts/fetch-claude-apps-gateway.sh --clean && scripts/fetch-claude-apps-gateway.sh\n' >&2
            ok=false
            continue
        fi
        printf '%s: verified %s tree %s\n' "$subtree" "$verification_format" "$actual"
    done
    [ "$ok" = true ]
}

case "$mode" in
    print)
        need_tool find
        need_tool git
        if [ "${#dirs[@]}" -eq 0 ]; then
            for subtree in "${subtrees[@]}"; do
                dirs+=("$vendor_dir/$subtree")
            done
        fi
        for dir in "${dirs[@]}"; do
            tree=$(git_tree_id_of "$dir")
            printf '%s  %s\n' "$tree" "$dir"
        done
        exit 0
        ;;
    clean)
        for subtree in "${subtrees[@]}"; do
            rm -rf "${vendor_dir:?}/${subtree:?}"
        done
        printf 'Removed materialized subtrees under %s\n' "$vendor_dir"
        exit 0
        ;;
    verify)
        need_tool find
        need_tool git
        need_tool jq
        if verify_materialized; then
            exit 0
        fi
        exit 1
        ;;
esac

# --- fetch -------------------------------------------------------------------
need_tool find
need_tool jq
need_tool git
require_verification_format

repository=$(read_pin .repository)
commit=$(read_pin .commit)
require_hex "$commit" 40 "commit"
if [ "$repository" != "$canonical_upstream" ]; then
    printf 'UPSTREAM.json repository is %s; only the canonical %s is fetched.\n' "$repository" "$canonical_upstream" >&2
    exit 1
fi

present=0
for subtree in "${subtrees[@]}"; do
    if [ -e "$vendor_dir/$subtree" ] || [ -L "$vendor_dir/$subtree" ]; then
        present=$((present + 1))
    fi
done
if [ "$present" -eq "${#subtrees[@]}" ]; then
    if verify_materialized; then
        printf 'Already materialized and verified at %s.\n' "$commit"
        exit 0
    fi
    exit 1
fi
if [ "$present" -gt 0 ]; then
    printf 'Some subtrees already exist under %s. Run --clean first; nothing was changed.\n' "$vendor_dir" >&2
    exit 1
fi

work=$(mktemp -d)
materializing=false
cleanup() {
    rm -rf "$work"
    if [ "$materializing" = true ]; then
        for subtree in "${subtrees[@]}"; do
            rm -rf "${vendor_dir:?}/${subtree:?}"
        done
        printf 'Verification failed; removed the partially materialized subtrees.\n' >&2
    fi
}
trap cleanup EXIT

git clone --quiet --filter=blob:none --no-checkout "$repository" "$work/source"
git -C "$work/source" sparse-checkout init --cone
git -C "$work/source" sparse-checkout set "${subtrees[@]}"
git -C "$work/source" fetch --quiet --depth 1 origin "$commit"
git -C "$work/source" checkout --quiet --detach FETCH_HEAD

fetched=$(git -C "$work/source" rev-parse HEAD)
if [ "$fetched" != "$commit" ]; then
    printf 'Fetched commit %s does not equal the pinned %s.\n' "$fetched" "$commit" >&2
    exit 1
fi

for subtree in "${subtrees[@]}"; do
    expected_tree=$(read_pin ".paths[\"$subtree\"]")
    require_hex "$expected_tree" 40 "paths.$subtree"
    actual_tree=$(git -C "$work/source" rev-parse "HEAD:$subtree")
    if [ "$actual_tree" != "$expected_tree" ]; then
        printf '%s: tree id at %s is %s, UPSTREAM.json records %s.\n' "$subtree" "$commit" "$actual_tree" "$expected_tree" >&2
        exit 1
    fi
    source_tree=$(git_tree_id_of "$work/source/$subtree")
    if [ "$source_tree" != "$expected_tree" ]; then
        printf '%s: checked-out entries at %s reconstruct tree %s, UPSTREAM.json records %s.\n' \
            "$subtree" "$commit" "$source_tree" "$expected_tree" >&2
        printf '  The pin is inconsistent; regenerate it with scripts/sync-claude-apps-gateway.sh --ref %s\n' "$commit" >&2
        exit 1
    fi
done

materializing=true
for subtree in "${subtrees[@]}"; do
    cp -Rp "$work/source/$subtree" "$vendor_dir/$subtree"
done
verify_materialized
materializing=false
printf 'Materialized %s and %s at %s (%s tree ids verified).\n' \
    "${subtrees[0]}" "${subtrees[1]}" "$commit" "$verification_format"
