# ABOUTME: Unit tests for distribute.py — archive creation, platform detection, checksums
# ABOUTME: Covers DistributeCommand pure logic methods (no AWS calls) + presign principal modes

"""Tests for governed_inference_platform.cli.commands.distribute module."""

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from rich.console import Console

from governed_inference_platform.cli.commands.distribute import DistributeCommand, S3UploadProgress


@pytest.fixture
def cmd():
    """Create a DistributeCommand instance without invoking CLI machinery."""
    c = DistributeCommand.__new__(DistributeCommand)
    return c


@pytest.fixture
def package_dir(tmp_path):
    """Create a realistic package directory structure."""
    pkg = tmp_path / "dist" / "my-profile" / "2026-06-01-120000"
    pkg.mkdir(parents=True)
    # Create config
    (pkg / "config.json").write_text('{"profile": "my-profile"}')
    (pkg / "install.sh").write_text("#!/bin/bash\necho install")
    (pkg / "install.bat").write_text("@echo off\necho install")
    (pkg / "README.md").write_text("# README")
    # Create platform binaries (tiny stubs)
    for platform in [
        "credential-process-macos-arm64",
        "credential-process-macos-intel",
        "credential-process-linux-x64",
        "credential-process-linux-arm64",
        "credential-process-windows.exe",
        "otel-helper-macos-arm64",
        "otel-helper-macos-intel",
        "otel-helper-linux-x64",
        "otel-helper-linux-arm64",
        "otel-helper-windows.exe",
    ]:
        (pkg / platform).write_bytes(b"\x00" * 100)
    # Windows otel-helper launcher + AV-resilient fallback (required by install.bat)
    (pkg / "otel-helper.ps1").write_text("# otel-helper.ps1")
    (pkg / "otel-helper.cmd").write_text("@echo off\nREM otel-helper.cmd")
    (pkg / "harnesses" / "opencode").mkdir(parents=True)
    (pkg / "harnesses" / "opencode" / "opencode.json").write_text('{"model":"test"}\n')
    (pkg / "harnesses" / "README.md").write_text("# Harnesses\n")
    return pkg


class TestS3UploadProgress:
    """Tests for S3UploadProgress callback."""

    def test_progress_tracks_bytes(self):
        progress_bar = MagicMock()
        tracker = S3UploadProgress("test.zip", 1000, progress_bar)
        tracker.set_task_id("task-1")

        tracker(250)
        progress_bar.update.assert_called_with("task-1", completed=250)

        tracker(250)
        progress_bar.update.assert_called_with("task-1", completed=500)

    def test_no_task_id_does_not_crash(self):
        progress_bar = MagicMock()
        tracker = S3UploadProgress("test.zip", 1000, progress_bar)
        # No set_task_id called
        tracker(100)  # Should not raise
        progress_bar.update.assert_not_called()


class TestCheckOldFlatStructure:
    """Tests for _check_old_flat_structure."""

    def test_old_structure_detected(self, cmd, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "credential-process-macos-arm64").write_bytes(b"\x00")
        assert cmd._check_old_flat_structure(dist) is True

    def test_new_structure_not_detected(self, cmd, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        # New structure has profile/timestamp subdirs, no flat files
        (dist / "my-profile" / "2026-01-01-000000").mkdir(parents=True)
        assert cmd._check_old_flat_structure(dist) is False

    def test_nonexistent_dir(self, cmd, tmp_path):
        dist = tmp_path / "nonexistent"
        assert cmd._check_old_flat_structure(dist) is False


class TestScanDistributions:
    """Tests for _scan_distributions."""

    def test_scans_profile_timestamp_structure(self, cmd, package_dir):
        dist = package_dir.parent.parent
        builds = cmd._scan_distributions(dist)

        assert "my-profile" in builds
        assert len(builds["my-profile"]) == 1
        assert builds["my-profile"][0]["timestamp"] == "2026-06-01-120000"
        assert builds["my-profile"][0]["path"] == package_dir

    def test_detects_platforms(self, cmd, package_dir):
        dist = package_dir.parent.parent
        builds = cmd._scan_distributions(dist)

        platforms = builds["my-profile"][0]["platforms"]
        assert "macos-arm64" in platforms
        assert "windows" in platforms
        assert "linux-x64" in platforms

    def test_calculates_size(self, cmd, package_dir):
        dist = package_dir.parent.parent
        builds = cmd._scan_distributions(dist)

        assert builds["my-profile"][0]["size"] > 0

    def test_empty_dir(self, cmd, tmp_path):
        dist = tmp_path / "empty"
        dist.mkdir()
        builds = cmd._scan_distributions(dist)
        assert builds == {}

    def test_nonexistent_dir(self, cmd, tmp_path):
        builds = cmd._scan_distributions(tmp_path / "ghost")
        assert builds == {}

    def test_skips_non_package_dirs(self, cmd, tmp_path):
        """Dirs without config.json or install scripts are skipped."""
        dist = tmp_path / "dist"
        profile = dist / "my-profile" / "2026-01-01-000000"
        profile.mkdir(parents=True)
        (profile / "random.txt").write_text("not a package")

        builds = cmd._scan_distributions(dist)
        assert builds["my-profile"] == []


class TestDetectPlatforms:
    """Tests for _detect_platforms."""

    def test_all_platforms(self, cmd, package_dir):
        platforms = cmd._detect_platforms(package_dir)
        assert set(platforms) == {"macos-arm64", "macos-intel", "linux-x64", "linux-arm64", "windows"}

    def test_windows_only(self, cmd, tmp_path):
        build = tmp_path / "build"
        build.mkdir()
        (build / "credential-process-windows.exe").write_bytes(b"\x00")
        platforms = cmd._detect_platforms(build)
        assert platforms == ["windows"]

    def test_empty_dir(self, cmd, tmp_path):
        build = tmp_path / "empty"
        build.mkdir()
        assert cmd._detect_platforms(build) == []


class TestFormatSize:
    """Tests for _format_size."""

    def test_bytes(self, cmd):
        assert "B" in cmd._format_size(500)

    def test_kilobytes(self, cmd):
        assert "KB" in cmd._format_size(1500)

    def test_megabytes(self, cmd):
        assert "MB" in cmd._format_size(1500000)

    def test_gigabytes(self, cmd):
        assert "GB" in cmd._format_size(1500000000)


class TestCreateArchive:
    """Tests for _create_archive."""

    def test_creates_zip(self, cmd, package_dir):
        archive = cmd._create_archive(package_dir)
        assert archive.exists()
        assert archive.name == "gip-package.zip"

    def test_zip_contains_expected_files(self, cmd, package_dir):
        archive = cmd._create_archive(package_dir)
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            assert "gip-package/config.json" in names
            assert "gip-package/install.sh" in names
            assert "gip-package/credential-process-macos-arm64" in names

    def test_zip_contains_windows_otel_helper_scripts(self, cmd, package_dir):
        """otel-helper.cmd/.ps1 are required by install.bat and must ship in the zip."""
        archive = cmd._create_archive(package_dir)
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            assert "gip-package/otel-helper.ps1" in names
            assert "gip-package/otel-helper.cmd" in names

    def test_combined_archive_excludes_windows_artifacts_until_credential_binary_exists(self, cmd, package_dir):
        (package_dir / "credential-process-windows.exe").unlink()

        archive = cmd._create_archive(package_dir)

        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())
        assert "gip-package/credential-process-linux-x64" in names
        assert "gip-package/install.bat" not in names
        assert "gip-package/gip-install.ps1" not in names
        assert "gip-package/otel-helper-windows.exe" not in names
        assert "gip-package/otel-helper.ps1" not in names
        assert "gip-package/otel-helper.cmd" not in names

    def test_zip_includes_settings_dir(self, cmd, package_dir):
        settings = package_dir / "gip-settings"
        settings.mkdir()
        (settings / "settings.json").write_text('{"key": "val"}')

        archive = cmd._create_archive(package_dir)
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            assert "gip-package/gip-settings/settings.json" in names

    def test_zip_harness_tree_matches_package_tree(self, cmd, package_dir):
        archive = cmd._create_archive(package_dir)
        expected = {
            f"gip-package/{path.relative_to(package_dir).as_posix()}"
            for path in (package_dir / "harnesses").rglob("*")
            if path.is_file()
        }
        with zipfile.ZipFile(archive, "r") as zf:
            actual = {name for name in zf.namelist() if name.startswith("gip-package/harnesses/")}
        assert actual == expected

    def test_skips_missing_files(self, cmd, tmp_path):
        """Only config.json present — zip still creates fine."""
        pkg = tmp_path / "sparse"
        pkg.mkdir()
        (pkg / "config.json").write_text("{}")

        archive = cmd._create_archive(pkg)
        assert archive.exists()
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            assert "gip-package/config.json" in names
            assert len(names) == 1


class TestCreatePerOsArchives:
    """Tests for _create_per_os_archives."""

    def test_creates_separate_archives(self, cmd, package_dir):
        archives = cmd._create_per_os_archives(package_dir)
        # Should have 5 platform archives (all binaries present)
        assert len(archives) == 5
        labels = [label for _, label, _ in archives]
        assert "Windows" in labels
        assert "macOS ARM64" in labels
        assert "Linux x64" in labels

    def test_each_archive_contains_platform_binary(self, cmd, package_dir):
        archives = cmd._create_per_os_archives(package_dir)
        for _platform, _label, archive_path in archives:
            with zipfile.ZipFile(archive_path, "r") as zf:
                names = zf.namelist()
                # Should have config.json in every platform archive
                assert "gip-package/config.json" in names

    def test_windows_archive_has_bat_and_ps1(self, cmd, package_dir):
        (package_dir / "gip-install.ps1").write_text("# ps1")
        archives = cmd._create_per_os_archives(package_dir)
        win_archives = [(p, l, a) for p, l, a in archives if p == "windows"]
        assert len(win_archives) == 1
        _, _, win_path = win_archives[0]
        with zipfile.ZipFile(win_path, "r") as zf:
            names = zf.namelist()
            assert "gip-package/install.bat" in names
            assert "gip-package/gip-install.ps1" in names
            # AV-resilient otel-helper launcher + fallback (required by install.bat)
            assert "gip-package/otel-helper.ps1" in names
            assert "gip-package/otel-helper.cmd" in names

    def test_skips_platform_without_binary(self, cmd, tmp_path):
        """Only Linux x64 binary present → only 1 archive."""
        pkg = tmp_path / "linux-only"
        pkg.mkdir()
        (pkg / "config.json").write_text("{}")
        (pkg / "credential-process-linux-x64").write_bytes(b"\x00")

        archives = cmd._create_per_os_archives(pkg)
        assert len(archives) == 1
        assert archives[0][0] == "linux-x64"


class TestExtraFilesInArchives:
    """Tests for admin-defined extra_files across the three archive builders."""

    @pytest.fixture
    def package_with_extras(self, package_dir):
        """Add extra files (a folder + two OS-specific scripts) into the build dir."""
        certs = package_dir / "certs"
        certs.mkdir()
        (certs / "ca.pem").write_text("CERT")
        (package_dir / "preinstall-mac.sh").write_text("#!/bin/bash")
        (package_dir / "preinstall-windows.ps1").write_text("# ps1")
        return package_dir

    _EXTRAS = [
        {"name": "certs", "targets": "all", "from": "~/x/certs"},
        {"name": "preinstall-mac.sh", "targets": "macos", "from": "~/x/pre.sh"},
        {"name": "preinstall-windows.ps1", "targets": "windows", "from": "~/x/pre.ps1"},
    ]

    def test_all_os_archive_contains_every_extra(self, cmd, package_with_extras):
        archive = cmd._create_archive(package_with_extras, self._EXTRAS)
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
        assert "gip-package/certs/ca.pem" in names
        assert "gip-package/preinstall-mac.sh" in names
        assert "gip-package/preinstall-windows.ps1" in names

    def test_all_os_archive_without_extras_unchanged(self, cmd, package_with_extras):
        """No extra_files arg → extras present in the build dir are NOT auto-added."""
        archive = cmd._create_archive(package_with_extras)
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
        assert "gip-package/certs/ca.pem" not in names
        assert "gip-package/preinstall-mac.sh" not in names

    def test_per_os_filters_by_platform(self, cmd, package_with_extras):
        archives = cmd._create_per_os_archives(package_with_extras, self._EXTRAS)
        by_platform = {p: a for p, _label, a in archives}

        with zipfile.ZipFile(by_platform["macos-arm64"], "r") as zf:
            mac_names = zf.namelist()
        assert "gip-package/certs/ca.pem" in mac_names  # all
        assert "gip-package/preinstall-mac.sh" in mac_names  # macos
        assert "gip-package/preinstall-windows.ps1" not in mac_names  # windows excluded

        with zipfile.ZipFile(by_platform["windows"], "r") as zf:
            win_names = zf.namelist()
        assert "gip-package/certs/ca.pem" in win_names  # all
        assert "gip-package/preinstall-windows.ps1" in win_names  # windows
        assert "gip-package/preinstall-mac.sh" not in win_names  # macos excluded

    def test_per_os_arch_target_lands_in_family_zip(self, cmd, package_dir):
        """An arch-specific target (macos-arm64) belongs in the macos-arm64 per-OS zip."""
        (package_dir / "arm-only.sh").write_text("arm")
        entries = [{"name": "arm-only.sh", "targets": "macos-arm64", "from": "~/x"}]
        archives = cmd._create_per_os_archives(package_dir, entries)
        by_platform = {p: a for p, _label, a in archives}

        with zipfile.ZipFile(by_platform["macos-arm64"], "r") as zf:
            assert "gip-package/arm-only.sh" in zf.namelist()
        with zipfile.ZipFile(by_platform["macos-intel"], "r") as zf:
            assert "gip-package/arm-only.sh" not in zf.namelist()

    def test_invalid_entries_skipped_silently(self, cmd, package_with_extras):
        """A colliding/invalid entry must not crash or ship — package fails fast upstream."""
        bad = [{"name": "config.json", "targets": "all", "from": "~/x"}]
        archive = cmd._create_archive(package_with_extras, bad)
        with zipfile.ZipFile(archive, "r") as zf:
            # config.json is the generated one, not clobbered by the extra
            assert "gip-package/config.json" in zf.namelist()


class TestCalculateChecksum:
    """Tests for _calculate_checksum."""

    def test_produces_sha256(self, cmd, tmp_path):
        f = tmp_path / "test.bin"
        content = b"hello world"
        f.write_bytes(content)

        result = cmd._calculate_checksum(f)
        expected = hashlib.sha256(content).hexdigest()
        assert result == expected

    def test_empty_file(self, cmd, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")

        result = cmd._calculate_checksum(f)
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected


class TestGenerateRestrictedUrl:
    """Tests for _generate_restricted_url."""

    def test_rejects_unenforced_ip_restriction(self, cmd):
        mock_s3 = MagicMock()

        with pytest.raises(ValueError, match="not enforced"):
            cmd._generate_restricted_url(mock_s3, "my-bucket", "packages/x.zip", "10.0.0.1", 48)

        mock_s3.generate_presigned_url.assert_not_called()


class TestDistributionSafety:
    @staticmethod
    def _console():
        return Console(file=StringIO(), force_terminal=False)

    @staticmethod
    def _landing_profile():
        return SimpleNamespace(
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            stack_names={},
            extra_files=None,
        )

    @staticmethod
    def _cloud_profile():
        return SimpleNamespace(
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            stack_names={},
            extra_files=None,
            enable_distribution=True,
            name="my-profile",
        )

    def test_allowed_ips_fails_before_any_side_effect(self, cmd):
        cmd.option = lambda name: "10.0.0.0/8" if name == "allowed-ips" else None

        with (
            patch.object(cmd, "_check_ssl_proxy_environment", side_effect=AssertionError("proxy check called")),
            patch(
                "governed_inference_platform.cli.commands.distribute.boto3.client",
                side_effect=AssertionError("AWS called"),
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                side_effect=AssertionError("AWS called"),
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.shutil.copy2", side_effect=AssertionError("copied")
            ),
        ):
            assert cmd.handle() == 1

    def test_local_landing_page_profile_uses_zero_aws_calls(self, cmd, package_dir, tmp_path, monkeypatch):
        output_root = tmp_path / "local-output"
        options = {
            "allowed-ips": None,
            "package-path": str(package_dir.parent.parent),
            "build-profile": None,
            "timestamp": None,
            "latest": True,
            "profile": None,
            "get-latest": False,
            "per-os": False,
        }
        cmd.option = options.get
        profile = SimpleNamespace(
            enable_distribution=False,
            distribution_type="landing-page",
            extra_files=None,
        )
        config = SimpleNamespace(active_profile="my-profile", get_profile=lambda _name: profile)
        monkeypatch.chdir(output_root.parent)

        with (
            patch.object(cmd, "_check_ssl_proxy_environment"),
            patch.object(cmd, "_upload_landing_page_packages", side_effect=AssertionError("landing upload called")),
            patch("governed_inference_platform.cli.commands.distribute.Config.load", return_value=config),
            patch(
                "governed_inference_platform.cli.commands.distribute.boto3.client",
                side_effect=AssertionError("AWS called"),
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                side_effect=AssertionError("AWS called"),
            ),
        ):
            assert cmd.handle() == 0

        archives = list(package_dir.parent.parent.glob("gip-package-*.zip"))
        assert len(archives) == 1

    def test_local_per_os_respects_custom_package_path(self, cmd, package_dir, tmp_path):
        output_root = tmp_path / "custom-output"
        options = {"package-path": str(output_root), "per-os": True}
        cmd.option = options.get
        profile = SimpleNamespace(enable_distribution=False, extra_files=None)

        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.boto3.client",
                side_effect=AssertionError("AWS called"),
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                side_effect=AssertionError("AWS called"),
            ),
        ):
            assert cmd._create_local_distribution(profile, MagicMock(), package_dir) == 0

        assert len(list(output_root.glob("gip-*.zip"))) == 5

    def test_local_archive_is_copied_exactly_once(self, cmd, package_dir, tmp_path):
        output_root = tmp_path / "local-output"
        cmd.option = {"package-path": str(output_root), "per-os": False}.get
        profile = SimpleNamespace(enable_distribution=False, extra_files=None)

        def copy_once(source, destination):
            Path(destination).write_bytes(Path(source).read_bytes())

        with patch(
            "governed_inference_platform.cli.commands.distribute.shutil.copy2", side_effect=copy_once
        ) as copy_file:
            assert cmd._create_local_distribution(profile, MagicMock(), package_dir) == 0

        copy_file.assert_called_once()

    def test_malformed_landing_build_timestamp_fails_before_aws(self, cmd, package_dir):
        malformed_path = package_dir.with_name("not-a-timestamp")
        package_dir.rename(malformed_path)

        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                side_effect=AssertionError("AWS called"),
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.boto3.client",
                side_effect=AssertionError("AWS called"),
            ),
        ):
            assert cmd._upload_landing_page_packages(self._landing_profile(), MagicMock(), malformed_path) == 1

    def test_landing_upload_failure_preserves_stale_packages_and_fails(self, cmd, package_dir):
        s3 = MagicMock()
        events = []

        def upload(_client, _path, _bucket, key, **_kwargs):
            events.append(("upload", key))
            if key == "packages/linux/latest.zip":
                raise RuntimeError("upload failed")

        def delete(**kwargs):
            events.append(("delete", kwargs["Key"]))

        s3.delete_object.side_effect = delete
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket", "DistributionURL": "https://landing.example"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=s3),
            patch.object(cmd, "_upload_file_with_retry", side_effect=upload),
        ):
            result = cmd._upload_landing_page_packages(self._landing_profile(), self._console(), package_dir)

        assert result == 1
        assert any(event == ("upload", "packages/windows/latest.zip") for event in events)
        assert not any(operation == "delete" for operation, _key in events)

    def test_landing_stale_cleanup_runs_only_after_all_uploads(self, cmd, package_dir):
        for filename in (
            "credential-process-macos-arm64",
            "credential-process-macos-intel",
            "otel-helper-macos-arm64",
            "otel-helper-macos-intel",
        ):
            (package_dir / filename).unlink()

        s3 = MagicMock()
        events = []

        def upload(_client, _path, _bucket, key, **_kwargs):
            events.append(("upload", key))

        def delete(**kwargs):
            events.append(("delete", kwargs["Key"]))

        s3.delete_object.side_effect = delete
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket", "DistributionURL": "https://landing.example"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=s3),
            patch.object(cmd, "_upload_file_with_retry", side_effect=upload),
        ):
            result = cmd._upload_landing_page_packages(self._landing_profile(), self._console(), package_dir)

        assert result == 0
        assert events[-1] == ("delete", "packages/mac/latest.zip")
        assert all(operation == "upload" for operation, _key in events[:-1])

    def test_windows_fallbacks_without_credential_binary_are_not_published(self, cmd, package_dir):
        (package_dir / "credential-process-windows.exe").unlink()
        (package_dir / "otel-helper-windows.exe").unlink()
        s3 = MagicMock()
        events = []
        all_platform_names = set()

        def upload(_client, _path, _bucket, key, **_kwargs):
            events.append(("upload", key))
            if key == "packages/all-platforms/latest.zip":
                with zipfile.ZipFile(_path, "r") as archive:
                    all_platform_names.update(archive.namelist())

        def delete(**kwargs):
            events.append(("delete", kwargs["Key"]))

        s3.delete_object.side_effect = delete
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket", "DistributionURL": "https://landing.example"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=s3),
            patch.object(cmd, "_upload_file_with_retry", side_effect=upload),
        ):
            result = cmd._upload_landing_page_packages(self._landing_profile(), self._console(), package_dir)

        assert result == 0
        assert not any(event == ("upload", "packages/windows/latest.zip") for event in events)
        assert "gip-package/credential-process-linux-x64" in all_platform_names
        assert "gip-package/install.bat" not in all_platform_names
        assert "gip-package/otel-helper.ps1" not in all_platform_names
        assert "gip-package/otel-helper.cmd" not in all_platform_names
        assert events[-1] == ("delete", "packages/windows/latest.zip")

    def test_landing_stale_delete_failure_returns_nonzero(self, cmd, package_dir):
        for filename in (
            "credential-process-macos-arm64",
            "credential-process-macos-intel",
            "otel-helper-macos-arm64",
            "otel-helper-macos-intel",
        ):
            (package_dir / filename).unlink()

        s3 = MagicMock()
        s3.delete_object.side_effect = RuntimeError("delete failed")
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket", "DistributionURL": "https://landing.example"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=s3),
            patch.object(cmd, "_upload_file_with_retry"),
        ):
            result = cmd._upload_landing_page_packages(self._landing_profile(), self._console(), package_dir)

        assert result == 1

    def test_partial_per_os_upload_returns_nonzero(self, cmd, tmp_path):
        first = tmp_path / "first.zip"
        second = tmp_path / "second.zip"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://download.example"
        cmd.option = {"package-path": str(tmp_path)}.get

        def upload(_client, _path, _bucket, key, **_kwargs):
            if "linux" in key:
                raise RuntimeError("upload failed")

        with (
            patch.object(
                cmd,
                "_create_per_os_archives",
                return_value=[("windows", "Windows", first), ("linux-x64", "Linux x64", second)],
            ),
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=s3),
            patch.object(cmd, "_upload_file_with_retry", side_effect=upload),
        ):
            result = cmd._distribute_per_os(
                tmp_path,
                self._cloud_profile(),
                ["windows", "linux-x64"],
                48,
                MagicMock(),
            )

        assert result == 1

    @pytest.mark.parametrize(
        "upload_error, ssm_error, expected",
        [
            (None, None, 0),
            (RuntimeError("upload failed"), None, 1),
            (None, RuntimeError("pointer failed"), 1),
        ],
    )
    def test_cloud_distribution_result_and_no_local_copy(self, cmd, package_dir, upload_error, ssm_error, expected):
        codebuild = MagicMock()
        codebuild.list_builds_for_project.return_value = {"ids": []}
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://download.example"
        ssm = MagicMock()
        if ssm_error:
            ssm.put_parameter.side_effect = ssm_error

        def client(service, **_kwargs):
            return {"codebuild": codebuild, "s3": s3, "ssm": ssm}[service]

        cmd.option = {
            "expires-hours": "48",
            "per-os": False,
            "allowed-ips": None,
            "show-qr": False,
        }.get
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.get_codebuild_region", return_value="us-east-1"),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", side_effect=client),
            patch.object(cmd, "_upload_file_with_retry", side_effect=upload_error),
            patch("governed_inference_platform.cli.commands.distribute.shutil.copy2") as copy_file,
        ):
            result = cmd._create_distribution(self._cloud_profile(), self._console(), package_dir)

        assert result == expected
        copy_file.assert_not_called()
        if expected == 0:
            distribution = json.loads(ssm.put_parameter.call_args.kwargs["Value"])
            cmd._validate_distribution_record(distribution)
            assert distribution["created"].endswith("+00:00")

    def test_timestamp_mismatch_is_not_written_as_latest(self, cmd, package_dir):
        codebuild = MagicMock()
        codebuild.list_builds_for_project.return_value = {"ids": []}
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://download.example"
        ssm = MagicMock()
        distribution = cmd._build_distribution_record(
            datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc),
            48,
            "https://download.example",
            "abc123",
        )
        distribution["package_key"] = "packages/other/package.zip"

        def client(service, **_kwargs):
            return {"codebuild": codebuild, "s3": s3, "ssm": ssm}[service]

        cmd.option = {
            "expires-hours": "48",
            "per-os": False,
            "allowed-ips": None,
            "show-qr": False,
        }.get
        with (
            patch(
                "governed_inference_platform.cli.commands.distribute.get_stack_outputs",
                return_value={"DistributionBucket": "bucket"},
            ),
            patch("governed_inference_platform.cli.commands.distribute.get_codebuild_region", return_value="us-east-1"),
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", side_effect=client),
            patch.object(cmd, "_upload_file_with_retry"),
            patch.object(cmd, "_build_distribution_record", return_value=distribution),
        ):
            result = cmd._create_distribution(self._cloud_profile(), self._console(), package_dir)

        assert result == 1
        ssm.put_parameter.assert_not_called()

    def test_get_latest_accepts_legacy_naive_expiration(self, cmd):
        legacy_record = {
            "url": "https://download.example",
            "expires": "2999-07-15T12:34:56",
            "package_key": "packages/legacy/package.zip",
            "checksum": "abc123",
            "filename": "legacy-package.zip",
        }
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps(legacy_record)}}
        cmd.option = {"show-qr": False}.get

        with (
            patch("governed_inference_platform.cli.commands.distribute.boto3.client", return_value=ssm),
            patch.object(cmd, "_show_download_stats"),
        ):
            result = cmd._get_latest_url(self._cloud_profile(), self._console())

        assert result == 0


class TestDistributionTimestamps:
    def test_distribution_json_uses_one_utc_timestamp(self, cmd):
        published_at = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc)

        distribution = cmd._build_distribution_record(published_at, 48, "https://download.example", "abc123")
        serialized = json.loads(json.dumps(distribution))

        cmd._validate_distribution_record(serialized)
        assert serialized["timestamp"] == "20260715-123456"
        assert serialized["created"] == "2026-07-15T12:34:56+00:00"
        assert serialized["filename"] == "gip-package-20260715-123456.zip"
        assert serialized["package_key"] == ("packages/20260715-123456/gip-package-20260715-123456.zip")

    def test_malformed_distribution_timestamp_fails_closed(self, cmd):
        distribution = cmd._build_distribution_record(
            datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc),
            48,
            "https://download.example",
            "abc123",
        )
        distribution["timestamp"] = "2026-07-15"

        with pytest.raises(ValueError, match="Malformed distribution timestamp"):
            cmd._validate_distribution_record(distribution)

    def test_distribution_timestamp_mismatch_fails_closed(self, cmd):
        distribution = cmd._build_distribution_record(
            datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc),
            48,
            "https://download.example",
            "abc123",
        )
        distribution["filename"] = "gip-package-20260715-123457.zip"

        with pytest.raises(ValueError, match="does not match package filename or key"):
            cmd._validate_distribution_record(distribution)


class TestResolvePresignContext:
    """Role mode caps expiry at 12h with a message; user mode is unchanged."""

    def test_role_mode_caps_expiry_at_12h_with_message(self, cmd):
        console = MagicMock()
        outputs = {"PresignPrincipalType": "role", "PresignRoleArn": "arn:aws:iam::123456789012:role/presign"}

        principal_type, role_arn, hours = cmd._resolve_presign_context(outputs, 168, console)

        assert principal_type == "role"
        assert role_arn == "arn:aws:iam::123456789012:role/presign"
        assert hours == 12
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "capping URL expiry at 12 hours" in printed
        assert "temporary credentials" in printed
        assert "no static IAM" in printed

    def test_role_mode_within_cap_unchanged_no_message(self, cmd):
        console = MagicMock()
        outputs = {"PresignPrincipalType": "role", "PresignRoleArn": "arn:aws:iam::123456789012:role/presign"}

        _, _, hours = cmd._resolve_presign_context(outputs, 8, console)

        assert hours == 8
        console.print.assert_not_called()

    def test_role_mode_exactly_12h_not_capped(self, cmd):
        console = MagicMock()
        outputs = {"PresignPrincipalType": "role", "PresignRoleArn": "arn:x"}

        _, _, hours = cmd._resolve_presign_context(outputs, 12, console)

        assert hours == 12
        console.print.assert_not_called()

    def test_user_mode_unchanged_at_168h(self, cmd):
        console = MagicMock()
        outputs = {"PresignPrincipalType": "iam-user"}

        principal_type, role_arn, hours = cmd._resolve_presign_context(outputs, 168, console)

        assert principal_type == "iam-user"
        assert role_arn is None
        assert hours == 168
        console.print.assert_not_called()

    def test_missing_output_defaults_to_user_mode(self, cmd):
        """Stacks deployed before PresignPrincipalType existed have no output."""
        console = MagicMock()

        principal_type, role_arn, hours = cmd._resolve_presign_context({}, 168, console)

        assert principal_type == "iam-user"
        assert role_arn is None
        assert hours == 168
        console.print.assert_not_called()


class TestCreatePresignClient:
    """Role mode presigns via STS AssumeRole with session duration == URL expiry."""

    @pytest.fixture
    def profile(self):
        p = MagicMock()
        p.aws_region = "us-east-1"
        return p

    @staticmethod
    def _fake_boto3(monkeypatch, sts_client, s3_calls):
        import governed_inference_platform.cli.commands.distribute as dist_mod

        def fake_client(service, **kwargs):
            if service == "sts":
                return sts_client
            s3_calls.append(kwargs)
            return MagicMock(name="s3-presign-client")

        monkeypatch.setattr(dist_mod.boto3, "client", fake_client)

    def test_assumes_role_with_expiry_matched_duration(self, cmd, profile, monkeypatch):
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "AKIATEST", "SecretAccessKey": "secret", "SessionToken": "token"}
        }
        s3_calls = []
        self._fake_boto3(monkeypatch, sts, s3_calls)

        _, hours = cmd._create_presign_client(profile, "arn:aws:iam::123456789012:role/presign", 12, MagicMock())

        assert hours == 12
        sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/presign",
            RoleSessionName="gip-distribute-presign",
            DurationSeconds=12 * 3600,
        )
        assert len(s3_calls) == 1
        assert s3_calls[0]["aws_access_key_id"] == "AKIATEST"
        assert s3_calls[0]["aws_session_token"] == "token"

    def test_role_chaining_falls_back_to_one_hour(self, cmd, profile, monkeypatch):
        from botocore.exceptions import ClientError

        chaining_error = ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": (
                        "The requested DurationSeconds exceeds the 1 hour session limit for roles "
                        "assumed by role chaining."
                    ),
                }
            },
            "AssumeRole",
        )
        sts = MagicMock()
        sts.assume_role.side_effect = [
            chaining_error,
            {"Credentials": {"AccessKeyId": "AKIATEST", "SecretAccessKey": "secret", "SessionToken": "token"}},
        ]
        s3_calls = []
        self._fake_boto3(monkeypatch, sts, s3_calls)
        console = MagicMock()

        _, hours = cmd._create_presign_client(profile, "arn:role", 12, console)

        assert hours == 1
        assert sts.assume_role.call_args_list[1][1]["DurationSeconds"] == 3600
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "1 hour" in printed

    def test_non_duration_error_propagates(self, cmd, profile, monkeypatch):
        from botocore.exceptions import ClientError

        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "AssumeRole")
        sts = MagicMock()
        sts.assume_role.side_effect = denied
        self._fake_boto3(monkeypatch, sts, [])

        with pytest.raises(ClientError):
            cmd._create_presign_client(profile, "arn:role", 12, MagicMock())


class _CFLoader(yaml.SafeLoader):
    """YAML loader tolerating CloudFormation intrinsics (test_cfn_naming_limits.py pattern)."""


for _tag in ["!Ref", "!Sub", "!GetAtt", "!If", "!Equals", "!Not", "!Select", "!Join", "!Split", "!Condition"]:
    _CFLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
        ),
    )


class TestPresignedS3TemplateContract:
    """Both presign principal paths exist; user resources are conditional."""

    @pytest.fixture
    def template(self):
        path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "presigned-s3-distribution.yaml"
        if not path.exists():
            pytest.skip("presigned-s3-distribution.yaml not found")
        return yaml.load(path.read_text(), Loader=_CFLoader)  # nosec B506

    def test_presign_principal_type_parameter(self, template):
        param = template["Parameters"]["PresignPrincipalType"]
        assert param["Default"] == "iam-user"
        assert sorted(param["AllowedValues"]) == ["iam-user", "role"]

    def test_admin_principal_arn_pattern_parameter(self, template):
        param = template["Parameters"]["AdminPrincipalArnPattern"]
        assert param["Default"] == "*"

    def test_both_mode_conditions_exist(self, template):
        assert "UseIamUserPresign" in template["Conditions"]
        assert "UseRolePresign" in template["Conditions"]

    @pytest.mark.parametrize(
        "resource",
        ["DistributionUser", "DistributionUserAccessKey", "DistributionUserPolicy", "DistributionUserSecret"],
    )
    def test_user_resources_conditional_on_iam_user_mode(self, template, resource):
        assert template["Resources"][resource]["Condition"] == "UseIamUserPresign"

    def test_presign_role_conditional_on_role_mode(self, template):
        role = template["Resources"]["DistributionPresignRole"]
        assert role["Type"] == "AWS::IAM::Role"
        assert role["Condition"] == "UseRolePresign"
        assert role["Properties"]["MaxSessionDuration"] == 43200

    def test_role_trust_policy_limited_to_account_and_admin_pattern(self, template):
        statement = template["Resources"]["DistributionPresignRole"]["Properties"]["AssumeRolePolicyDocument"][
            "Statement"
        ][0]
        condition = statement["Condition"]
        assert "aws:PrincipalAccount" in condition["StringEquals"]
        assert "aws:PrincipalArn" in condition["StringLike"]

    def test_role_policy_grants_only_get_object_on_packages(self, template):
        policy = template["Resources"]["DistributionPresignRole"]["Properties"]["Policies"][0]
        statement = policy["PolicyDocument"]["Statement"][0]
        assert statement["Action"] == ["s3:GetObject"]
        assert "packages/*" in statement["Resource"]

    def test_outputs_expose_mode_and_conditional_role_arn(self, template):
        outputs = template["Outputs"]
        assert "PresignPrincipalType" in outputs
        assert "Condition" not in outputs["PresignPrincipalType"]
        assert outputs["PresignRoleArn"]["Condition"] == "UseRolePresign"

    def test_secret_outputs_conditional_on_iam_user_mode(self, template):
        outputs = template["Outputs"]
        assert outputs["DistributionSecretArn"]["Condition"] == "UseIamUserPresign"
        assert outputs["DistributionSecretName"]["Condition"] == "UseIamUserPresign"
