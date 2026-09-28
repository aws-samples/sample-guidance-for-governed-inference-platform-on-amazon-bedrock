# ABOUTME: Cleanup command to remove installed authentication components
# ABOUTME: Removes files and configuration created by the test or manual installation

"""Cleanup command - Remove installed authentication components."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from cleo.commands.command import Command
from cleo.helpers import option
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from governed_inference_platform.cli.utils.proc import run_checked


class CleanupCommand(Command):
    name = "cleanup"
    description = "Remove installed authentication components"
    OWNERSHIP_MANIFEST = ".gip-ownership.json"
    OWNERSHIP_SCHEMA_VERSION = 1

    options = [
        option("force", description="Skip confirmation prompts", flag=True),
        option(
            "profile",
            description="Profile used by --credentials-only; full cleanup removes the manifest-owned installation",
            flag=False,
            default="gip",
        ),
        option(
            "credentials-only", description="Only clear cached credentials without removing other components", flag=True
        ),
    ]

    def handle(self) -> int:
        """Execute the cleanup command."""
        console = Console()

        profile_name = self.option("profile")
        force = self.option("force")
        credentials_only = self.option("credentials-only")

        # Handle credentials-only mode
        if credentials_only:
            return self._clear_credentials_only(console, profile_name, force)

        # Show what will be cleaned
        console.print(
            Panel.fit(
                "[bold yellow]Authentication Cleanup[/bold yellow]\n\n"
                "This will remove components installed by the test command or manual installation",
                border_style="yellow",
                padding=(1, 2),
            )
        )

        auth_dir = Path.home() / "gip"
        aws_config = Path.home() / ".aws" / "config"
        claude_settings = Path.home() / ".claude" / "settings.json"

        manifest_path, manifest = self._load_ownership_manifest(auth_dir, console)
        owned_files, unresolved_files = self._matching_owned_files(manifest, auth_dir, console)
        profile_sections = self._matching_config_sections(manifest, aws_config, "profile", "aws_profiles", console)
        sso_session_sections = self._matching_config_sections(
            manifest, aws_config, "sso-session", "aws_sso_sessions", console
        )

        items_to_remove = [("File", str(path), "Digest-matched gip-owned file") for _, path, _ in owned_files]
        items_to_remove.extend(
            ("AWS Profile", name, f"Digest-matched section in {aws_config}") for _, name, _ in profile_sections
        )
        items_to_remove.extend(
            ("AWS SSO session", name, f"Digest-matched section in {aws_config}") for _, name, _ in sso_session_sections
        )
        collector_pid = self._matching_collector_pid(auth_dir / "collector.pid", auth_dir)
        if collector_pid is not None:
            items_to_remove.append(("Process", str(collector_pid[0]), "Verified gip collector sidecar"))

        if manifest is None:
            if auth_dir.exists() or aws_config.exists() or claude_settings.exists():
                console.print(
                    "[yellow]No gip ownership manifest found. Legacy files, AWS profiles, and Claude settings "
                    "will be preserved.[/yellow]"
                )
        elif unresolved_files:
            console.print(
                "[yellow]Some manifest entries no longer match their recorded digest and will be preserved.[/yellow]"
            )

        if not items_to_remove:
            console.print("[green]No manifest-backed authentication components found to clean up.[/green]")
            return 0

        # Display what will be removed
        console.print("\n[bold]Items to be removed:[/bold]")
        for item_type, item_path, description in items_to_remove:
            console.print(f"  • {item_type}: [cyan]{item_path}[/cyan]")
            console.print(f"    [dim]{description}[/dim]")

        # Confirm removal
        if not force:
            if not Confirm.ask("\n[bold yellow]Remove these items?[/bold yellow]"):
                console.print("\n[yellow]Cleanup cancelled.[/yellow]")
                return 0

        # Perform cleanup
        console.print("\n[bold]Cleaning up...[/bold]")

        had_failures = False
        if collector_pid is not None and not self._stop_owned_collector(
            collector_pid[0], collector_pid[1], auth_dir, console
        ):
            had_failures = True

        removed_file_keys = []
        for manifest_key, path, expected_digest in owned_files:
            try:
                if not self._unlink_digest_matched(path, expected_digest, Path.home()):
                    console.print(f"[yellow]Preserving {path}: file changed after confirmation.[/yellow]")
                    had_failures = True
                    continue
                removed_file_keys.append(manifest_key)
                console.print(f"✓ Removed {path}")
            except Exception as e:
                console.print(f"[red]✗ Failed to remove {path}: {e}[/red]")
                had_failures = True

        removed_config_entries: dict[str, list[str]] = {"aws_profiles": [], "aws_sso_sessions": []}
        sections_to_remove = profile_sections + sso_session_sections
        if sections_to_remove:
            try:
                self._remove_config_sections(aws_config, sections_to_remove)
                for _, name, _ in profile_sections:
                    removed_config_entries["aws_profiles"].append(name)
                    console.print(f"✓ Removed AWS profile '{name}'")
                for _, name, _ in sso_session_sections:
                    removed_config_entries["aws_sso_sessions"].append(name)
                    console.print(f"✓ Removed AWS SSO session '{name}'")
            except Exception as e:
                console.print(f"[red]✗ Failed to remove AWS configuration: {e}[/red]")
                had_failures = True

        if not self._update_ownership_manifest(
            manifest_path,
            manifest,
            removed_file_keys,
            removed_config_entries,
            console,
        ):
            had_failures = True
        self._remove_empty_owned_directories(auth_dir, console)

        if had_failures:
            console.print("\n[red]Cleanup completed with preserved or failed items.[/red]")
        else:
            console.print("\n[green]Cleanup completed![/green]")

        # Show next steps
        console.print("\n[bold]Next steps:[/bold]")
        console.print("• Run 'gip package' to create a new distribution")
        console.print("• Run 'gip test' to reinstall and test")

        return 1 if had_failures else 0

    @staticmethod
    def _has_symlink_component(path: Path, root: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def _digest_matches(path: Path, expected_digest: str) -> bool:
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode):
                return False
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return False
                digest = hashlib.sha256()
                while block := os.read(descriptor, 65536):
                    digest.update(block)
            finally:
                os.close(descriptor)
            after = os.lstat(path)
        except OSError:
            return False
        return (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) and digest.hexdigest() == expected_digest.lower()

    @classmethod
    def _unlink_digest_matched(cls, path: Path, expected_digest: str, root: Path) -> bool:
        if cls._has_symlink_component(path, root):
            return False
        if os.name == "nt":
            quarantine = path.with_name(f".{path.name}.gip-quarantine-{uuid.uuid4().hex}")
            try:
                os.replace(path, quarantine)
            except OSError:
                return False
            if cls._digest_matches(quarantine, expected_digest):
                quarantine.unlink()
                return True
            cls._restore_quarantine_no_clobber(quarantine, path)
            return False

        try:
            relative = path.relative_to(root)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(root, directory_flags)
            try:
                for part in relative.parts[:-1]:
                    next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd

                name = relative.name
                quarantine = f".{name}.gip-quarantine-{uuid.uuid4().hex}"
                os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                try:
                    file_fd = os.open(
                        quarantine,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        opened = os.fstat(file_fd)
                        if not stat.S_ISREG(opened.st_mode):
                            raise OSError("Quarantined entry is not a regular file")
                        digest = hashlib.sha256()
                        while block := os.read(file_fd, 65536):
                            digest.update(block)
                    finally:
                        os.close(file_fd)
                    if digest.hexdigest() != expected_digest.lower():
                        raise OSError("Quarantined entry digest does not match")
                except OSError:
                    cls._restore_quarantine_at_no_clobber(directory_fd, quarantine, name)
                    return False
                os.unlink(quarantine, dir_fd=directory_fd)
                return True
            finally:
                os.close(directory_fd)
        except (OSError, ValueError):
            return False

    def _load_ownership_manifest(self, auth_dir: Path, console) -> tuple[Path, dict | None]:
        manifest_path = auth_dir / self.OWNERSHIP_MANIFEST
        self._ownership_manifest_snapshot = None
        if auth_dir.is_symlink() or self._has_symlink_component(manifest_path, Path.home()):
            console.print("[yellow]Refusing a symlinked ownership-manifest path. Preserving installed files.[/yellow]")
            return manifest_path, None
        if not manifest_path.is_file():
            return manifest_path, None
        try:
            content, snapshot, _ = self._read_regular_snapshot(manifest_path)
            manifest = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            console.print(f"[yellow]Could not read ownership manifest: {error}. Preserving installed files.[/yellow]")
            return manifest_path, None
        if not isinstance(manifest, dict) or manifest.get("schema_version") != self.OWNERSHIP_SCHEMA_VERSION:
            console.print("[yellow]Unsupported ownership manifest. Preserving installed files.[/yellow]")
            return manifest_path, None
        if (
            not isinstance(manifest.get("files", {}), dict)
            or not isinstance(manifest.get("aws_profiles", {}), dict)
            or not isinstance(manifest.get("aws_sso_sessions", {}), dict)
        ):
            console.print("[yellow]Invalid ownership manifest. Preserving installed files.[/yellow]")
            return manifest_path, None
        self._ownership_manifest_snapshot = snapshot
        return manifest_path, manifest

    @staticmethod
    def _read_regular_snapshot(path: Path) -> tuple[bytes, tuple[int, int, int, int, str], int]:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"Not a regular file: {path}")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f"Not a regular file: {path}")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OSError(f"File changed while opening: {path}")
            blocks = []
            digest = hashlib.sha256()
            while block := os.read(descriptor, 65536):
                blocks.append(block)
                digest.update(block)
        finally:
            os.close(descriptor)
        after = os.lstat(path)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            raise OSError(f"File changed while reading: {path}")
        return b"".join(blocks), (*before_identity, digest.hexdigest()), stat.S_IMODE(before.st_mode)

    @classmethod
    def _snapshot_matches(cls, path: Path, expected: tuple[int, int, int, int, str]) -> bool:
        try:
            _, actual, _ = cls._read_regular_snapshot(path)
        except OSError:
            return False
        return actual == expected

    @classmethod
    def _replace_if_unchanged(
        cls,
        path: Path,
        content: bytes,
        expected_snapshot: tuple[int, int, int, int, str],
        mode: int,
    ) -> None:
        temporary_path = None
        quarantine_path = path.with_name(f".{path.name}.gip-quarantine-{uuid.uuid4().hex}")
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.gip-", delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, mode)
            os.replace(path, quarantine_path)
            if not cls._snapshot_matches(quarantine_path, expected_snapshot):
                if not cls._restore_quarantine_no_clobber(quarantine_path, path):
                    raise OSError(f"{path} changed after confirmation; original preserved at {quarantine_path}")
                raise OSError(f"{path} changed after confirmation")
            try:
                os.link(temporary_path, path)
            except OSError as error:
                if cls._restore_quarantine_no_clobber(quarantine_path, path):
                    raise OSError(f"Could not install replacement for {path}; original restored") from error
                raise OSError(
                    f"Could not install replacement for {path}; original preserved at {quarantine_path}"
                ) from error
            temporary_path.unlink()
            temporary_path = None
            quarantine_path.unlink()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _restore_quarantine_no_clobber(quarantine_path: Path, path: Path) -> bool:
        """Restore quarantined bytes without overwriting a concurrent destination."""
        source_fd = None
        destination_fd = None
        destination_created = False
        try:
            source_fd = os.open(
                quarantine_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                return False
            destination_fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IMODE(source_stat.st_mode),
            )
            destination_created = True
            while block := os.read(source_fd, 65536):
                remaining = memoryview(block)
                while remaining:
                    remaining = remaining[os.write(destination_fd, remaining) :]
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = None
            os.close(source_fd)
            source_fd = None
            quarantine_path.unlink()
            return True
        except OSError:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
            if destination_created:
                path.unlink(missing_ok=True)
            return False

    @staticmethod
    def _restore_quarantine_at_no_clobber(directory_fd: int, quarantine: str, name: str) -> bool:
        source_fd = None
        destination_fd = None
        destination_created = False
        try:
            source_fd = os.open(
                quarantine,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                return False
            destination_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IMODE(source_stat.st_mode),
                dir_fd=directory_fd,
            )
            destination_created = True
            while block := os.read(source_fd, 65536):
                remaining = memoryview(block)
                while remaining:
                    remaining = remaining[os.write(destination_fd, remaining) :]
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = None
            os.close(source_fd)
            source_fd = None
            os.unlink(quarantine, dir_fd=directory_fd)
            return True
        except OSError:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
            if destination_created:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
            return False

    def _matching_owned_files(self, manifest: dict | None, auth_dir: Path, console) -> tuple[list, list]:
        if manifest is None:
            return [], []

        home = Path.home()
        auth_root = home / "gip"
        settings_path = home / ".claude" / "settings.json"
        collector_config = home / ".gip" / "collector-config.yaml"
        matched = []
        unresolved = []
        for raw_path, expected_digest in manifest.get("files", {}).items():
            relative = Path(raw_path) if isinstance(raw_path, str) else Path("..")
            valid_digest = (
                isinstance(expected_digest, str)
                and len(expected_digest) == 64
                and all(character in "0123456789abcdefABCDEF" for character in expected_digest)
            )
            if relative.is_absolute() or ".." in relative.parts or not valid_digest:
                unresolved.append(raw_path)
                continue
            candidate = home / relative
            allowed = candidate in (settings_path, collector_config) or candidate.is_relative_to(auth_root)
            if not allowed or self._has_symlink_component(candidate, home) or not candidate.is_file():
                unresolved.append(raw_path)
                continue
            if not self._digest_matches(candidate, expected_digest):
                unresolved.append(raw_path)
                continue
            matched.append((raw_path, candidate, expected_digest.lower()))

        if unresolved:
            console.print(f"[dim]Preserving unmatched ownership entries: {', '.join(map(str, unresolved))}[/dim]")
        return matched, unresolved

    @staticmethod
    def _config_section(lines: list[str], section_type: str, name: str) -> tuple[int, int, str] | None:
        header = f"[{section_type} {name}]"
        start = None
        for index, line in enumerate(lines):
            if line.strip() == header:
                start = index
                break
        if start is None:
            return None
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                end = index
                break
        return start, end, "".join(lines[start:end])

    @staticmethod
    def _config_section_digest(section: str) -> str:
        canonical = section.replace("\r\n", "\n").replace("\r", "\n")
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _matching_config_section(
        self,
        manifest: dict | None,
        aws_config: Path,
        section_type: str,
        name: str,
        manifest_key: str,
        console,
    ):
        if manifest is None or not aws_config.is_file() or self._has_symlink_component(aws_config, Path.home()):
            return None
        expected_digest = manifest.get(manifest_key, {}).get(name)
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            return None
        lines = aws_config.read_text(encoding="utf-8").splitlines(keepends=True)
        section = self._config_section(lines, section_type, name)
        if section is None:
            return None
        actual_digest = self._config_section_digest(section[2])
        if actual_digest != expected_digest.lower():
            console.print(f"[dim]Preserving modified AWS {section_type} '{name}'.[/dim]")
            return None
        return expected_digest.lower()

    def _matching_config_sections(
        self,
        manifest: dict | None,
        aws_config: Path,
        section_type: str,
        manifest_key: str,
        console,
    ) -> list[tuple[str, str, str]]:
        if manifest is None:
            return []
        matched = []
        for name in manifest.get(manifest_key, {}):
            expected_digest = self._matching_config_section(
                manifest, aws_config, section_type, name, manifest_key, console
            )
            if expected_digest is not None:
                matched.append((section_type, name, expected_digest))
        return matched

    def _matching_profile_section(self, manifest: dict | None, aws_config: Path, profile_name: str, console):
        return self._matching_config_section(manifest, aws_config, "profile", profile_name, "aws_profiles", console)

    def _remove_profile_section(self, aws_config: Path, profile_name: str, expected_digest: str) -> None:
        self._remove_config_sections(aws_config, [("profile", profile_name, expected_digest)])

    def _remove_config_sections(self, aws_config: Path, sections: list[tuple[str, str, str]]) -> None:
        if self._has_symlink_component(aws_config, Path.home()):
            raise OSError("AWS config path became a symlink")
        content, snapshot, mode = self._read_regular_snapshot(aws_config)
        lines = content.decode("utf-8").splitlines(keepends=True)
        ranges = []
        for section_type, name, expected_digest in sections:
            section = self._config_section(lines, section_type, name)
            if section is None or self._config_section_digest(section[2]) != expected_digest:
                raise OSError(f"AWS {section_type} changed after confirmation")
            ranges.append((section[0], section[1]))
        for start, end in sorted(ranges, reverse=True):
            del lines[start:end]
        self._replace_if_unchanged(aws_config, "".join(lines).encode(), snapshot, mode)

    @classmethod
    def _matching_collector_pid(cls, pid_file: Path, auth_dir: Path) -> tuple[Path, str] | None:
        if cls._has_symlink_component(pid_file, Path.home()):
            return None
        try:
            content, snapshot, _ = cls._read_regular_snapshot(pid_file)
            pid = int(content.decode("ascii").strip())
            result = subprocess.run(  # nosec B603 — fixed argv (ps), int-cast pid, no shell
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, UnicodeDecodeError, ValueError, subprocess.SubprocessError):
            return None
        command = result.stdout.strip()
        expected_binary = str(auth_dir / "otelcol")
        expected_config = str(auth_dir / "collector-config.yaml")
        if result.returncode != 0 or expected_binary not in command or expected_config not in command:
            return None
        return pid_file, snapshot[-1]

    def _stop_owned_collector(self, pid_file: Path, expected_digest: str, auth_dir: Path, console) -> bool:
        if not self._digest_matches(pid_file, expected_digest):
            console.print("[yellow]Collector PID file changed; leaving the process running.[/yellow]")
            return False
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if os.name == "nt":
            console.print("[yellow]Cannot verify collector process identity on Windows; leaving it running.[/yellow]")
            return False
        try:
            result = subprocess.run(  # nosec B603 — fixed argv (ps), int-cast pid, no shell
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            command = result.stdout.strip()
            if result.returncode != 0:
                if self._unlink_digest_matched(pid_file, expected_digest, Path.home()):
                    console.print(f"✓ Removed stale collector PID file {pid_file}")
                    return True
                console.print("[yellow]Collector exited, but its PID file changed; preserving it.[/yellow]")
                return False
            if "otelcol" not in command or str(auth_dir) not in command:
                console.print("[yellow]PID does not identify the gip collector; leaving it running.[/yellow]")
                return False
            os.kill(pid, 15)
            console.print(f"✓ Stopped collector sidecar (PID {pid})")
            if self._unlink_digest_matched(pid_file, expected_digest, Path.home()):
                console.print(f"✓ Removed {pid_file}")
                return True
            console.print("[yellow]Collector stopped, but its PID file changed; preserving it.[/yellow]")
            return False
        except (OSError, subprocess.SubprocessError):
            console.print("[yellow]Could not verify or stop the collector process.[/yellow]")
            return False

    def _update_ownership_manifest(
        self,
        manifest_path: Path,
        manifest: dict | None,
        removed_file_keys: list[str],
        removed_config_entries: dict[str, list[str]],
        console,
    ) -> bool:
        if manifest is None:
            return True
        for key in removed_file_keys:
            manifest.get("files", {}).pop(key, None)
        for manifest_key, names in removed_config_entries.items():
            for name in names:
                manifest.get(manifest_key, {}).pop(name, None)
        if manifest.get("files") or manifest.get("aws_profiles") or manifest.get("aws_sso_sessions"):
            if self._has_symlink_component(manifest_path, Path.home()):
                console.print("[yellow]Ownership manifest path changed; preserving the manifest.[/yellow]")
                return False
            snapshot = getattr(self, "_ownership_manifest_snapshot", None)
            if snapshot is None:
                console.print("[yellow]Ownership manifest snapshot unavailable; preserving the manifest.[/yellow]")
                return False
            try:
                mode = stat.S_IMODE(os.lstat(manifest_path).st_mode)
                content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
                self._replace_if_unchanged(manifest_path, content, snapshot, mode)
            except OSError as error:
                console.print(f"[yellow]Ownership manifest changed; preserving it: {error}[/yellow]")
                return False
            return True
        try:
            snapshot = getattr(self, "_ownership_manifest_snapshot", None)
            if snapshot is None or not self._unlink_digest_matched(manifest_path, snapshot[-1], Path.home()):
                console.print("[yellow]Ownership manifest changed; preserving it.[/yellow]")
                return False
            else:
                console.print(f"✓ Removed {manifest_path}")
                return True
        except OSError as error:
            console.print(f"[yellow]Could not remove {manifest_path}: {error}[/yellow]")
            return False

    @staticmethod
    def _remove_empty_owned_directories(auth_dir: Path, console) -> None:
        if auth_dir.exists() and not auth_dir.is_symlink():
            try:
                auth_dir.rmdir()
                console.print(f"✓ Removed empty directory {auth_dir}")
            except OSError:
                pass

    def _clear_credentials_only(self, console, profile_name, force):
        """Clear only cached credentials without removing other components."""
        console.print(
            Panel.fit(
                "[bold cyan]Clear Cached Credentials[/bold cyan]\n\n"
                f"This will clear cached credentials for profile: {profile_name}",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        # Check if credential-process exists
        credential_process = Path.home() / "gip" / "credential-process"

        if not credential_process.exists():
            console.print("[yellow]Credential process not found. Nothing to clear.[/yellow]")
            return 0

        # Confirm clearing
        if not force:
            if not Confirm.ask("\n[bold yellow]Clear cached credentials?[/bold yellow]"):
                console.print("\n[yellow]Operation cancelled.[/yellow]")
                return 0

        # Run the credential process with --clear-cache flag
        console.print("\n[bold]Clearing cached credentials...[/bold]")

        try:
            result = run_checked(  # nosec B603 — list argv, no shell
                [str(credential_process), "--profile", profile_name, "--clear-cache"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0:
                if result.stderr:
                    # Parse the output to show what was cleared
                    for line in result.stderr.split("\n"):
                        if line.strip():
                            console.print(f"  {line}")
                console.print("\n[green]✓ Cached credentials cleared successfully![/green]")
            else:
                console.print(f"[red]Failed to clear credentials: {result.stderr}[/red]")
                return 1

        except subprocess.TimeoutExpired:
            console.print("[red]Operation timed out[/red]")
            return 1
        except Exception as e:
            console.print(f"[red]Error clearing credentials: {e}[/red]")
            return 1

        console.print("\n[bold]Next steps:[/bold]")
        console.print("• The next AWS command will trigger re-authentication")
        console.print("• Use 'export AWS_PROFILE=gip' to set the profile")

        return 0
