# ABOUTME: Regression tests for manifest-backed, conservative cleanup.
# ABOUTME: Verifies gip never removes unowned files, settings, or AWS profiles.

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from governed_inference_platform.cli.commands.cleanup import CleanupCommand


def _command(force=True):
    command = CleanupCommand.__new__(CleanupCommand)
    options = {"profile": "gip", "force": force, "credentials-only": False}
    command.option = options.get
    return command


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _write_manifest(home: Path, *, files=None, profiles=None, sso_sessions=None):
    install = home / "gip"
    install.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "files": files or {},
        "aws_profiles": profiles or {},
        "aws_sso_sessions": sso_sessions or {},
    }
    (install / ".gip-ownership.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_cleanup_removes_only_digest_matched_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    user_file = install / "user-notes.txt"
    user_file.write_text("keep", encoding="utf-8")

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text('{"env":{"USER_SETTING":"keep"}}', encoding="utf-8")

    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    owned_section = "[profile gip]\ncredential_process = gip\n\n"
    other_section = "[profile Personal]\nregion = us-west-2\n"
    aws_config = aws_dir / "config"
    aws_config.write_text(owned_section + other_section, encoding="utf-8")

    _write_manifest(
        tmp_path,
        files={
            "gip/credential-process": _digest("owned"),
            ".claude/settings.json": _digest("original-settings"),
        },
        profiles={"gip": _digest(owned_section)},
    )

    assert _command().handle() == 0
    assert not owned.exists()
    assert user_file.read_text(encoding="utf-8") == "keep"
    assert settings.read_text(encoding="utf-8") == '{"env":{"USER_SETTING":"keep"}}'
    assert "gip" not in aws_config.read_text(encoding="utf-8")
    assert "Personal" in aws_config.read_text(encoding="utf-8")

    remaining_manifest = json.loads((install / ".gip-ownership.json").read_text(encoding="utf-8"))
    assert remaining_manifest["files"] == {".claude/settings.json": _digest("original-settings")}


def test_legacy_cleanup_preserves_unknown_files_settings_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    binary = install / "credential-process"
    binary.write_text("legacy", encoding="utf-8")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    aws_config.write_text("[profile gip]\nregion = us-east-1\n", encoding="utf-8")

    assert _command().handle() == 0
    assert binary.exists()
    assert settings.exists()
    assert "gip" in aws_config.read_text(encoding="utf-8")


def test_manifest_path_traversal_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    outside = tmp_path.parent / "outside-gip-test"
    outside.write_text("keep", encoding="utf-8")
    try:
        _write_manifest(tmp_path, files={"../outside-gip-test": _digest("keep")})
        assert _command().handle() == 0
        assert outside.read_text(encoding="utf-8") == "keep"
    finally:
        outside.unlink(missing_ok=True)


def test_cleanup_is_idempotent_after_owned_file_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "otel-helper"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/otel-helper": _digest("owned")})

    command = _command()
    assert command.handle() == 0
    assert command.handle() == 0
    assert not install.exists()


@pytest.mark.skipif(os.name == "nt", reason="creating directory symlinks requires elevated Windows privileges")
def test_symlinked_install_root_is_never_followed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    external = tmp_path / "external"
    external.mkdir()
    victim = external / "credential-process"
    victim.write_text("owned", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "files": {"gip/credential-process": _digest("owned")},
        "aws_profiles": {},
    }
    (external / ".gip-ownership.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "gip").symlink_to(external, target_is_directory=True)

    assert _command().handle() == 0
    assert victim.read_text(encoding="utf-8") == "owned"


def test_file_changed_during_confirmation_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})

    def mutate_then_confirm(*_args, **_kwargs):
        owned.write_text("changed", encoding="utf-8")
        return True

    with patch("governed_inference_platform.cli.commands.cleanup.Confirm.ask", side_effect=mutate_then_confirm):
        assert _command(force=False).handle() == 1

    assert owned.read_text(encoding="utf-8") == "changed"


def test_profile_changed_after_scan_is_not_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    original = "[profile gip]\nregion = us-east-1\n"
    aws_config.write_text(original, encoding="utf-8")
    command = _command()

    aws_config.write_text("[profile gip]\nregion = eu-west-1\n", encoding="utf-8")
    with pytest.raises(OSError, match="changed after confirmation"):
        command._remove_profile_section(aws_config, "gip", _digest(original))

    assert "eu-west-1" in aws_config.read_text(encoding="utf-8")


def test_cleanup_removes_owned_profile_and_sso_session_together(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    profile = "[profile gip]\nsso_session = gip-session\n\n"
    session = "[sso-session gip-session]\nsso_start_url = https://example.awsapps.com/start\n"
    other = "[profile Personal]\nregion = us-west-2\n"
    aws_config.write_text(profile + session + other, encoding="utf-8")
    _write_manifest(
        tmp_path,
        profiles={"gip": _digest(profile)},
        sso_sessions={"gip-session": _digest(session)},
    )

    assert _command().handle() == 0

    remaining = aws_config.read_text(encoding="utf-8")
    assert "gip" not in remaining
    assert "Personal" in remaining


def test_cleanup_removes_every_digest_matched_profile_in_shared_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    first = "[profile gip]\ncredential_process = gip --profile gip\n\n"
    second = "[profile Research]\ncredential_process = gip --profile Research\n\n"
    collector = "[profile Research-collector]\ncredential_process = gip --profile Research\n\n"
    personal = "[profile Personal]\nregion = us-west-2\n"
    aws_config.write_text(first + second + collector + personal, encoding="utf-8")
    _write_manifest(
        tmp_path,
        profiles={
            "gip": _digest(first),
            "Research": _digest(second),
            "Research-collector": _digest(collector),
        },
    )

    assert _command().handle() == 0

    remaining = aws_config.read_text(encoding="utf-8")
    assert "gip" not in remaining
    assert "Research" not in remaining
    assert "Personal" in remaining


def test_cleanup_preserves_public_marker_block_without_manifest_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    wrapper = "# >>> gip claude wrapper >>>\nuser content\n# <<< gip claude wrapper <<<\n"
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(wrapper, encoding="utf-8")
    _write_manifest(tmp_path)

    assert _command().handle() == 0

    assert bashrc.read_text(encoding="utf-8") == wrapper


def test_cleanup_preserves_wrapper_added_after_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("export KEEP=1\n", encoding="utf-8")

    def add_wrapper_then_confirm(*_args, **_kwargs):
        bashrc.write_text(
            'export KEEP=1\n# >>> gip claude wrapper >>>\nclaude() { command claude "$@"; }\n'
            "# <<< gip claude wrapper <<<\n",
            encoding="utf-8",
        )
        return True

    with patch("governed_inference_platform.cli.commands.cleanup.Confirm.ask", side_effect=add_wrapper_then_confirm):
        assert _command(force=False).handle() == 0

    assert "gip claude wrapper" in bashrc.read_text(encoding="utf-8")


def test_cleanup_preserves_wrapper_changed_after_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})
    bashrc = tmp_path / ".bashrc"
    wrapper = '# >>> gip claude wrapper >>>\nclaude() { command claude "$@"; }\n# <<< gip claude wrapper <<<\n'
    bashrc.write_text(wrapper, encoding="utf-8")

    def change_wrapper_then_confirm(*_args, **_kwargs):
        bashrc.write_text("export NEW=1\n" + wrapper, encoding="utf-8")
        return True

    with patch("governed_inference_platform.cli.commands.cleanup.Confirm.ask", side_effect=change_wrapper_then_confirm):
        assert _command(force=False).handle() == 0

    assert bashrc.read_text(encoding="utf-8") == "export NEW=1\n" + wrapper


def test_cleanup_does_not_remove_unowned_empty_subdirectories(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    unowned = install / "user-empty-directory"
    unowned.mkdir()
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})

    assert _command().handle() == 0

    assert unowned.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="creating directory symlinks requires elevated Windows privileges")
def test_ancestor_replaced_by_symlink_after_confirmation_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})
    external = tmp_path / "external"
    external.mkdir()
    victim = external / "credential-process"
    victim.write_text("owned", encoding="utf-8")
    moved = tmp_path / "original-install"

    def swap_ancestor_then_confirm(*_args, **_kwargs):
        install.rename(moved)
        install.symlink_to(external, target_is_directory=True)
        return True

    with patch("governed_inference_platform.cli.commands.cleanup.Confirm.ask", side_effect=swap_ancestor_then_confirm):
        assert _command(force=False).handle() == 1

    assert victim.read_text(encoding="utf-8") == "owned"
    assert (moved / "credential-process").read_text(encoding="utf-8") == "owned"


def test_manifest_can_own_zero_binary_collector_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    collector = tmp_path / ".gip" / "collector-config.yaml"
    collector.parent.mkdir()
    collector.write_text("receivers: {}\n", encoding="utf-8")
    _write_manifest(tmp_path, files={".gip/collector-config.yaml": _digest("receivers: {}\n")})

    assert _command().handle() == 0

    assert not collector.exists()
    assert collector.parent.exists()


def test_config_rewrite_aborts_if_file_changes_before_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    owned = "[profile gip]\nregion = us-east-1\n"
    aws_config.write_text(owned, encoding="utf-8")
    command = _command()
    original_replace = command._replace_if_unchanged

    def mutate_before_replace(path, content, snapshot, mode):
        path.write_text("[profile gip]\nregion = eu-west-1\n", encoding="utf-8")
        return original_replace(path, content, snapshot, mode)

    monkeypatch.setattr(command, "_replace_if_unchanged", mutate_before_replace)

    with pytest.raises(OSError, match="changed after confirmation"):
        command._remove_profile_section(aws_config, "gip", _digest(owned))

    assert "eu-west-1" in aws_config.read_text(encoding="utf-8")


def test_manifest_rewrite_preserves_concurrent_change(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    owned = install / "credential-process"
    owned.write_text("owned", encoding="utf-8")
    _write_manifest(tmp_path, files={"gip/credential-process": _digest("owned")})
    command = _command()
    original_update = command._update_ownership_manifest

    def mutate_before_update(manifest_path, manifest, removed_file_keys, removed_config_entries, console):
        manifest_path.write_text('{"concurrent": true}\n', encoding="utf-8")
        return original_update(manifest_path, manifest, removed_file_keys, removed_config_entries, console)

    monkeypatch.setattr(command, "_update_ownership_manifest", mutate_before_update)

    assert command.handle() == 1

    assert json.loads((install / ".gip-ownership.json").read_text(encoding="utf-8")) == {"concurrent": True}


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX ps/kill path; Windows leaves the collector running")
def test_runtime_collector_pid_is_stopped_only_for_exact_install_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    pid_file = install / "collector.pid"
    pid_file.write_text("1234\n", encoding="utf-8")
    command_line = f"{install / 'otelcol'} --config {install / 'collector-config.yaml'}"
    process = type("Process", (), {"returncode": 0, "stdout": command_line})()

    with patch("governed_inference_platform.cli.commands.cleanup.subprocess.run", return_value=process):
        matched = CleanupCommand._matching_collector_pid(pid_file, install)

    assert matched == (pid_file, _digest("1234\n"))
    console = type("Console", (), {"print": lambda self, *_args, **_kwargs: None})()
    with (
        patch("governed_inference_platform.cli.commands.cleanup.subprocess.run", return_value=process),
        patch("governed_inference_platform.cli.commands.cleanup.os.kill") as kill,
    ):
        _command()._stop_owned_collector(pid_file, matched[1], install, console)

    kill.assert_called_once_with(1234, 15)
    assert not pid_file.exists()


def test_runtime_collector_pid_preserves_unrelated_process(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    pid_file = install / "collector.pid"
    pid_file.write_text("1234\n", encoding="utf-8")
    process = type("Process", (), {"returncode": 0, "stdout": "/usr/local/bin/otelcol --config /tmp/other"})()

    with patch("governed_inference_platform.cli.commands.cleanup.subprocess.run", return_value=process):
        assert CleanupCommand._matching_collector_pid(pid_file, install) is None

    assert pid_file.exists()


def test_replace_link_failure_restores_original(tmp_path, monkeypatch):
    target = tmp_path / "config"
    target.write_text("original", encoding="utf-8")
    _, snapshot, mode = CleanupCommand._read_regular_snapshot(target)
    original_link = os.link

    def fail_replacement_link(source, destination, *args, **kwargs):
        if Path(destination) == target:
            raise OSError("simulated link failure")
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_replacement_link)

    with pytest.raises(OSError, match="original restored"):
        CleanupCommand._replace_if_unchanged(target, b"replacement", snapshot, mode)

    assert target.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".config.gip-quarantine-*"))


def test_replace_never_overwrites_concurrent_destination(tmp_path, monkeypatch):
    target = tmp_path / "config"
    target.write_text("original", encoding="utf-8")
    _, snapshot, mode = CleanupCommand._read_regular_snapshot(target)
    original_link = os.link

    def create_concurrent_destination(source, destination, *args, **kwargs):
        if Path(destination) == target:
            target.write_text("concurrent", encoding="utf-8")
            raise FileExistsError("simulated race")
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", create_concurrent_destination)

    with pytest.raises(OSError, match="original preserved at"):
        CleanupCommand._replace_if_unchanged(target, b"replacement", snapshot, mode)

    assert target.read_text(encoding="utf-8") == "concurrent"
    quarantines = list(tmp_path.glob(".config.gip-quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is not available on Windows")
def test_snapshot_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="Not a regular file"):
        CleanupCommand._read_regular_snapshot(fifo)


def test_crlf_aws_sections_match_canonical_manifest_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    aws_config = tmp_path / ".aws" / "config"
    aws_config.parent.mkdir()
    canonical = "[profile gip]\nregion = us-east-1\n\n"
    aws_config.write_bytes(canonical.replace("\n", "\r\n").encode())
    manifest = {
        "schema_version": 1,
        "files": {},
        "aws_profiles": {"gip": _digest(canonical)},
        "aws_sso_sessions": {},
    }

    assert _command()._matching_profile_section(manifest, aws_config, "gip", None) == _digest(canonical)


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX ps/kill path; Windows leaves the collector running")
def test_verified_collector_that_exits_before_stop_removes_stale_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    pid_file = install / "collector.pid"
    pid_file.write_text("1234\n", encoding="utf-8")
    stopped = type("Process", (), {"returncode": 1, "stdout": ""})()
    console = type("Console", (), {"print": lambda self, *_args, **_kwargs: None})()

    with patch("governed_inference_platform.cli.commands.cleanup.subprocess.run", return_value=stopped):
        assert _command()._stop_owned_collector(pid_file, _digest("1234\n"), install, console)

    assert not pid_file.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")
def test_windows_leaves_collector_running_and_keeps_pid_file(tmp_path, monkeypatch):
    """Windows cannot verify the collector's command line, so cleanup must not kill or unlink."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    install = tmp_path / "gip"
    install.mkdir()
    pid_file = install / "collector.pid"
    pid_file.write_text("1234\n", encoding="utf-8")
    console = type("Console", (), {"print": lambda self, *_args, **_kwargs: None})()

    with (
        patch("governed_inference_platform.cli.commands.cleanup.subprocess.run") as run,
        patch("governed_inference_platform.cli.commands.cleanup.os.kill") as kill,
    ):
        assert not _command()._stop_owned_collector(pid_file, _digest("1234\n"), install, console)

    run.assert_not_called()
    kill.assert_not_called()
    assert pid_file.exists()
