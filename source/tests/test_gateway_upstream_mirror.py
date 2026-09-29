# ABOUTME: Offline provenance checks for the pinned AWS Samples gateway source (ADR-0036).

import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.support.proc import run_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_ROOT = REPO_ROOT / "vendor" / "aws-samples" / "anthropic-on-aws"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch-claude-apps-gateway.sh"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-claude-apps-gateway.sh"
SUBTREES = {"claude-apps-gateway", "claude-apps-gateway-bootstrap"}
VERIFICATION_FORMAT = "git-tree-sha1-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX modes, symlinks, and FIFOs")


def _pin() -> dict:
    return json.loads((VENDOR_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))


def _git_plumbing_env(scratch: Path) -> dict[str, str]:
    home = scratch / "home"
    xdg = scratch / "xdg"
    home.mkdir()
    xdg.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
        }
    )
    return env


def _run_git_plumbing(git_dir: Path, env: dict[str, str], *args: str, payload: bytes | None = None) -> bytes:
    result = run_cmd(
        ["git", "--git-dir", str(git_dir), *args],
        input=payload,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.strip()


def _reference_tree_in_store(root: Path, git_dir: Path, env: dict[str, str]) -> str:
    entries: list[tuple[bytes, bytes]] = []
    with os.scandir(root) as children:
        for child in children:
            path = Path(child.path)
            name = os.fsencode(child.name)
            if child.is_symlink():
                mode = b"120000"
                kind = b"blob"
                object_id = _run_git_plumbing(
                    git_dir,
                    env,
                    "hash-object",
                    "-w",
                    "--stdin",
                    payload=os.fsencode(os.readlink(path)),
                )
                sort_name = name
            elif child.is_file(follow_symlinks=False):
                file_mode = child.stat(follow_symlinks=False).st_mode
                mode = b"100755" if file_mode & 0o111 else b"100644"
                kind = b"blob"
                object_id = _run_git_plumbing(
                    git_dir,
                    env,
                    "hash-object",
                    "-w",
                    "--stdin",
                    payload=path.read_bytes(),
                )
                sort_name = name
            elif child.is_dir(follow_symlinks=False):
                mode = b"040000"
                kind = b"tree"
                object_id = _reference_tree_in_store(path, git_dir, env).encode("ascii")
                sort_name = name + b"/"
            else:
                raise ValueError(f"unsupported fixture entry: {path}")
            entry = mode + b" " + kind + b" " + object_id + b"\t" + name + b"\0"
            entries.append((sort_name, entry))
    tree_input = b"".join(entry for _, entry in sorted(entries))
    return _run_git_plumbing(git_dir, env, "mktree", "-z", payload=tree_input).decode("ascii")


def _reference_tree_id(root: Path) -> str:
    """Build a reference tree with index-free Git plumbing, not the fetch script."""
    with tempfile.TemporaryDirectory(prefix="gateway-reference-tree-") as temporary:
        scratch = Path(temporary)
        git_dir = scratch / "objects.git"
        env = _git_plumbing_env(scratch)
        initialized = run_cmd(
            ["git", "init", "--quiet", "--bare", str(git_dir)],
            capture_output=True,
            check=False,
            env=env,
        )
        assert initialized.returncode == 0, initialized.stderr.decode("utf-8", errors="replace")
        return _reference_tree_in_store(root, git_dir, env)


def _write_sample_tree(root: Path, marker: str = "sample") -> None:
    (root / "nested dir").mkdir(parents=True)
    (root / "plain file.txt").write_bytes(f"{marker}\r\n".encode())
    executable = root / "run.sh"
    executable.write_bytes(b"#!/bin/sh\nprintf 'ok\\n'\n")
    executable.chmod(0o755)
    (root / "nested dir" / "line\nbreak.bin").write_bytes(bytes(range(32)))
    os.symlink("plain file.txt", root / "plain-link")


def _write_pin(
    repo_root: Path,
    *,
    repository: str,
    commit: str = "1" * 40,
    trees: dict[str, str] | None = None,
) -> None:
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"
    if trees is None:
        trees = {subtree: _reference_tree_id(vendor_root / subtree) for subtree in sorted(SUBTREES)}
    pin = {
        "repository": repository,
        "commit": commit,
        "commit_date": "2026-01-01T00:00:00Z",
        "verification": {"format": VERIFICATION_FORMAT},
        "paths": trees,
    }
    (vendor_root / "UPSTREAM.json").write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")


def _isolated_fetch_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    scripts = repo_root / "scripts"
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"
    scripts.mkdir(parents=True)
    vendor_root.mkdir(parents=True)
    script = scripts / FETCH_SCRIPT.name
    shutil.copy2(FETCH_SCRIPT, script)
    for subtree in SUBTREES:
        _write_sample_tree(vendor_root / subtree, subtree)
    _write_pin(repo_root, repository="https://github.com/aws-samples/anthropic-on-aws.git")
    return repo_root, script


def _workflow_run_step(name: str) -> str:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "sync-claude-apps-gateway.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["open-review-pr"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def test_upstream_manifest_pins_repository_commit_and_subtrees():
    manifest = _pin()

    assert manifest["repository"] == "https://github.com/aws-samples/anthropic-on-aws.git"
    assert HEX40.match(manifest["commit"])
    assert set(manifest["paths"]) == SUBTREES
    assert all(HEX40.match(tree) for tree in manifest["paths"].values())


def test_upstream_manifest_versions_git_tree_verification():
    manifest = _pin()

    assert manifest["verification"] == {"format": VERIFICATION_FORMAT}
    assert "sha256_manifest" not in manifest
    assert "sha256_manifest_recipe" not in manifest


def test_upstream_subtrees_are_not_tracked_and_are_ignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for subtree in SUBTREES:
        assert f"vendor/aws-samples/anthropic-on-aws/{subtree}/" in gitignore

    tracked = run_cmd(  # nosec B603 -- fixed argv
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "vendor/aws-samples/anthropic-on-aws"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    deleted = run_cmd(  # nosec B603 -- fixed argv; index entries whose file is gone (unstaged removals)
        ["git", "-C", str(REPO_ROOT), "ls-files", "--deleted", "--", "vendor/aws-samples/anthropic-on-aws"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sorted(set(tracked.stdout.split()) - set(deleted.stdout.split())) == [
        "vendor/aws-samples/anthropic-on-aws/LICENSE",
        "vendor/aws-samples/anthropic-on-aws/README.md",
        "vendor/aws-samples/anthropic-on-aws/UPSTREAM.json",
    ]


def test_fetch_script_parses_and_documents_its_modes():
    assert FETCH_SCRIPT.is_file()

    syntax = run_cmd(["bash", "-n", str(FETCH_SCRIPT)], capture_output=True, text=True, check=False)  # nosec B603
    assert syntax.returncode == 0, syntax.stderr

    helptext = run_cmd(["bash", str(FETCH_SCRIPT), "--help"], capture_output=True, text=True, check=False)  # nosec B603
    assert helptext.returncode == 0, helptext.stderr
    for token in ("UPSTREAM.json", "--verify", "--clean", "--print-tree-id", VERIFICATION_FORMAT):
        assert token in helptext.stdout


def test_fetch_script_uses_git_only_for_network_access():
    text = FETCH_SCRIPT.read_text(encoding="utf-8")

    assert "git clone" in text
    assert "curl" not in text
    assert "wget" not in text


@posix_only
def test_print_tree_id_matches_git_object_format_for_paths_modes_and_symlinks(tmp_path):
    tree = tmp_path / "subtree"
    _write_sample_tree(tree)

    first = run_cmd(
        ["bash", str(FETCH_SCRIPT), "--print-tree-id", str(tree)], capture_output=True, text=True, check=False
    )  # nosec B603
    assert first.returncode == 0, first.stderr
    digest, printed_path = first.stdout.rstrip("\n").split("  ", 1)
    assert printed_path == str(tree)
    assert digest == _reference_tree_id(tree)

    (tree / "plain-link").unlink()
    os.symlink("different\ntarget", tree / "plain-link")
    second = run_cmd(
        ["bash", str(FETCH_SCRIPT), "--print-tree-id", str(tree)], capture_output=True, text=True, check=False
    )  # nosec B603
    assert second.returncode == 0, second.stderr
    assert second.stdout.split("  ", 1)[0] != digest
    assert second.stdout.split("  ", 1)[0] == _reference_tree_id(tree)


@posix_only
def test_print_tree_id_fails_closed_on_an_empty_directory(tmp_path):
    result = run_cmd(
        ["bash", str(FETCH_SCRIPT), "--print-tree-id", str(tmp_path)], capture_output=True, text=True, check=False
    )  # nosec B603

    assert result.returncode != 0
    assert result.stdout == ""


@posix_only
def test_print_tree_id_rejects_fifo_without_blocking(tmp_path):
    tree = tmp_path / "subtree"
    _write_sample_tree(tree)
    os.mkfifo(tree / "named-pipe")

    result = run_cmd(
        ["bash", str(FETCH_SCRIPT), "--print-tree-id", str(tree)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )  # nosec B603

    assert result.returncode != 0
    assert "unsupported filesystem entry" in result.stderr.lower()


@posix_only
def test_print_tree_id_does_not_use_caller_git_index(tmp_path):
    tree = tmp_path / "subtree"
    _write_sample_tree(tree)
    caller_index = tmp_path / "caller-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(caller_index)}

    result = run_cmd(
        ["bash", str(FETCH_SCRIPT), "--print-tree-id", str(tree)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )  # nosec B603

    assert result.returncode == 0, result.stderr
    assert not caller_index.exists()


def _tamper_materialized_tree(tree: Path, case: str) -> None:
    if case == "content":
        (tree / "plain file.txt").write_bytes(b"tampered\n")
    elif case == "path":
        (tree / "plain file.txt").rename(tree / "renamed file.txt")
    elif case == "file-type":
        (tree / "plain file.txt").unlink()
        os.symlink("run.sh", tree / "plain file.txt")
    elif case == "executable-mode":
        (tree / "run.sh").chmod(0o644)
    elif case == "added-symlink":
        os.symlink("/etc/passwd", tree / "escape-link")
    elif case == "symlink-target":
        (tree / "plain-link").unlink()
        os.symlink("run.sh", tree / "plain-link")
    elif case == "extra-file":
        (tree / "extra.txt").write_bytes(b"extra\n")
    elif case == "missing-file":
        (tree / "plain file.txt").unlink()
    else:  # pragma: no cover - protects the test table itself
        raise AssertionError(f"unknown tamper case: {case}")


@posix_only
@pytest.mark.parametrize(
    "case",
    [
        "content",
        "path",
        "file-type",
        "executable-mode",
        "added-symlink",
        "symlink-target",
        "extra-file",
        "missing-file",
    ],
)
def test_offline_verify_rejects_every_git_visible_tamper(tmp_path, case):
    repo_root, script = _isolated_fetch_repo(tmp_path)
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"

    clean = run_cmd(["bash", str(script), "--verify"], capture_output=True, text=True, check=False)  # nosec B603
    assert clean.returncode == 0, clean.stderr

    _tamper_materialized_tree(vendor_root / "claude-apps-gateway", case)
    tampered = run_cmd(["bash", str(script), "--verify"], capture_output=True, text=True, check=False, timeout=20)  # nosec B603

    assert tampered.returncode != 0
    assert "tree id mismatch" in tampered.stderr.lower()


def test_sync_script_has_valid_shell_syntax():
    result = run_cmd(["bash", "-n", str(SYNC_SCRIPT)], capture_output=True, text=True, check=False)  # nosec B603

    assert result.returncode == 0, result.stderr


def test_sync_script_rejects_alternate_source_in_write_mode(tmp_path):
    result = run_cmd(  # nosec B603
        ["bash", str(SYNC_SCRIPT), "--source", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "allowed only with --check" in result.stderr


def _must_run(argv: list[str], **kwargs) -> str:
    result = run_cmd(argv, capture_output=True, text=True, check=False, **kwargs)  # nosec B603
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _local_fetch_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for subtree in SUBTREES:
        _write_sample_tree(upstream / subtree, subtree)
    (upstream / "LICENSE").write_text("fixture license\n", encoding="utf-8")

    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    _must_run(["git", "init", "--quiet", str(upstream)])
    _must_run(["git", "-C", str(upstream), "add", "."])
    _must_run(
        ["git", "-C", str(upstream), "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture"],
        env=identity,
    )
    commit = _must_run(["git", "-C", str(upstream), "rev-parse", "HEAD"])
    trees = {subtree: _must_run(["git", "-C", str(upstream), "rev-parse", f"HEAD:{subtree}"]) for subtree in SUBTREES}
    assert trees == {subtree: _reference_tree_id(upstream / subtree) for subtree in SUBTREES}

    repo_root = tmp_path / "consumer"
    scripts = repo_root / "scripts"
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"
    scripts.mkdir(parents=True)
    vendor_root.mkdir(parents=True)
    script = scripts / FETCH_SCRIPT.name
    script_text = FETCH_SCRIPT.read_text(encoding="utf-8").replace(
        "canonical_upstream=https://github.com/aws-samples/anthropic-on-aws.git",
        f"canonical_upstream={shlex.quote(str(upstream))}",
        1,
    )
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    _write_pin(repo_root, repository=str(upstream), commit=commit, trees=trees)
    return repo_root, script, upstream


@posix_only
def test_fetch_preserves_executable_mode_and_symlink_then_cleans(tmp_path):
    repo_root, script, _ = _local_fetch_fixture(tmp_path)
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"

    fetched = run_cmd(["bash", str(script)], capture_output=True, text=True, check=False, timeout=30)  # nosec B603
    assert fetched.returncode == 0, fetched.stderr
    for subtree in SUBTREES:
        tree = vendor_root / subtree
        assert tree.joinpath("run.sh").stat().st_mode & stat.S_IXUSR
        assert tree.joinpath("plain-link").is_symlink()
        assert os.readlink(tree / "plain-link") == "plain file.txt"

    verified = run_cmd(["bash", str(script), "--verify"], capture_output=True, text=True, check=False, timeout=20)  # nosec B603
    assert verified.returncode == 0, verified.stderr

    cleaned = run_cmd(["bash", str(script), "--clean"], capture_output=True, text=True, check=False)  # nosec B603
    assert cleaned.returncode == 0, cleaned.stderr
    assert all(not os.path.lexists(vendor_root / subtree) for subtree in SUBTREES)


@posix_only
def test_fetch_removes_all_materialized_output_when_post_copy_verification_fails(tmp_path):
    repo_root, script, _ = _local_fetch_fixture(tmp_path)
    vendor_root = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    real_cp = shutil.which("cp")
    assert real_cp
    cp_wrapper = fake_bin / "cp"
    cp_wrapper.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f'{shlex.quote(real_cp)} "$@"\n'
        "destination=${!#}\n"
        'case "$destination" in\n'
        '  */claude-apps-gateway) chmod 0644 "$destination/run.sh" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    cp_wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = run_cmd(["bash", str(script)], capture_output=True, text=True, check=False, timeout=30, env=env)  # nosec B603

    assert result.returncode != 0
    assert "removed the partially materialized subtrees" in result.stderr.lower()
    assert all(not os.path.lexists(vendor_root / subtree) for subtree in SUBTREES)


@posix_only
def test_sync_rejects_upstream_license_symlink_without_copying_target(tmp_path):
    repo_root, _, upstream = _local_fetch_fixture(tmp_path)
    destination = repo_root / "vendor" / "aws-samples" / "anthropic-on-aws"
    trusted_license = b"trusted license\n"
    (destination / "LICENSE").write_bytes(trusted_license)
    upstream_license = upstream / "LICENSE"
    upstream_license.unlink()
    os.symlink("/etc/passwd", upstream_license)
    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    _must_run(["git", "-C", str(upstream), "add", "LICENSE"])
    _must_run(
        ["git", "-C", str(upstream), "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "symlink"],
        env=identity,
    )

    sync_copy = repo_root / "scripts" / SYNC_SCRIPT.name
    sync_text = SYNC_SCRIPT.read_text(encoding="utf-8").replace(
        "canonical_upstream=https://github.com/aws-samples/anthropic-on-aws.git",
        f"canonical_upstream={shlex.quote(str(upstream))}",
        1,
    )
    sync_copy.write_text(sync_text, encoding="utf-8")
    sync_copy.chmod(0o755)

    result = run_cmd(
        ["bash", str(sync_copy), "--ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )  # nosec B603

    assert result.returncode != 0
    assert "license must be a regular file" in result.stderr.lower()
    assert (destination / "LICENSE").read_bytes() == trusted_license


def test_sync_workflow_separates_untrusted_tests_from_pr_credentials():
    workflow = (REPO_ROOT / ".github" / "workflows" / "sync-claude-apps-gateway.yml").read_text(encoding="utf-8")

    assert "ref: main" in workflow
    assert "beta" not in workflow
    assert "persist-credentials: false" in workflow
    assert "open-review-pr:" in workflow
    assert "needs: validate" in workflow
    assert "permissions:\n      contents: write\n      pull-requests: write" in workflow
    assert "actions/checkout@v" not in workflow
    assert "actions/setup-node@v" not in workflow
    assert "actions/setup-python@v" not in workflow


def test_sync_workflow_tests_the_materialized_pin_and_commits_only_the_pin_files():
    workflow = (REPO_ROOT / ".github" / "workflows" / "sync-claude-apps-gateway.yml").read_text(encoding="utf-8")

    assert "run: scripts/sync-claude-apps-gateway.sh\n" in workflow
    assert "run: scripts/fetch-claude-apps-gateway.sh\n" in workflow
    assert "cache-dependency-path" not in workflow
    assert "npm ci\n          npm test" in workflow
    assert (
        "git add vendor/aws-samples/anthropic-on-aws/UPSTREAM.json vendor/aws-samples/anthropic-on-aws/LICENSE"
        in workflow
    )
    assert "rsync" not in workflow


def _run_pr_workflow_step(tmp_path: Path, *, pr_exists: bool) -> list[list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_stub = fake_bin / "git"
    git_stub.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = status ]; then printf " M vendor/aws-samples/anthropic-on-aws/UPSTREAM.json\\n"; fi\n',
        encoding="utf-8",
    )
    git_stub.chmod(0o755)
    gh_stub = fake_bin / "gh"
    gh_stub.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = pr ] && [ "${2:-}" = view ]; then\n'
        '  [ "$GH_PR_EXISTS" = true ]\n'
        "  exit\n"
        "fi\n"
        "{\n"
        '  printf "%s" "$1"\n'
        "  shift\n"
        '  for argument in "$@"; do printf "\\t%s" "$argument"; done\n'
        '  printf "\\n"\n'
        '} >> "$GH_LOG"\n',
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_PR_EXISTS": str(pr_exists).lower(),
        "GH_TOKEN": "test-token",
        "UPSTREAM_COMMIT": "b" * 40,
        "previous_commit": "a" * 40,
    }
    result = run_cmd(
        ["bash", "-euo", "pipefail", "-c", _workflow_run_step("Open or update review pull request")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )  # nosec B603
    assert result.returncode == 0, result.stderr
    return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("pr_exists", [False, True])
def test_sync_workflow_refreshes_pr_title_body_and_compare_url_after_create_or_update(tmp_path, pr_exists):
    commands = _run_pr_workflow_step(tmp_path, pr_exists=pr_exists)
    creates = [command for command in commands if command[:2] == ["pr", "create"]]
    edits = [command for command in commands if command[:2] == ["pr", "edit"]]

    assert len(creates) == (0 if pr_exists else 1)
    assert len(edits) == 1
    edit = edits[0]
    title = edit[edit.index("--title") + 1]
    body = edit[edit.index("--body") + 1]
    assert title == "chore: bump Claude Apps Gateway upstream pin"
    assert f"{'a' * 40}...{'b' * 40}" in body
    assert "git-tree-sha1-v1" in body


def test_pull_requests_verify_the_pin_by_fetching_it():
    workflow_path = REPO_ROOT / ".github" / "workflows" / "check-claude-apps-gateway-mirror.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    steps = parsed["jobs"]["verify"]["steps"]
    run_steps = [step["run"] for step in steps if "run" in step]

    assert "vendor/aws-samples/anthropic-on-aws/**" in workflow
    assert "- 'scripts/fetch-claude-apps-gateway.sh'" in workflow
    assert "persist-credentials: false" in workflow
    assert run_steps[0] == "scripts/fetch-claude-apps-gateway.sh"
    assert run_steps[1] == "scripts/fetch-claude-apps-gateway.sh --verify"
    assert 'sync-claude-apps-gateway.sh --check --ref "$commit"' in run_steps[2]
    assert not any("scan" in step.lower() or "audit" in step.lower() for step in run_steps)
