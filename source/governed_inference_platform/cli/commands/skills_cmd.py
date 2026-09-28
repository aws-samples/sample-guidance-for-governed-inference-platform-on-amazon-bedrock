# ABOUTME: `gip skills` command group — publish/list/approve/sync for the governed skills registry
# ABOUTME: Validates SKILL.md + _meta (frozen io.gip.skill/v1 schema), uploads artifacts, drives approval

"""Governed skills registry commands.

Composition (R13): AWS Agent Registry = governance source of truth (approval
state, versions, model-compat metadata); S3 artifact bucket = versioned
skill folders (``skills/<name>/<version>/``); distributor Lambda = renders
APPROVED records into each harness's native delivery channel. These commands
are the publisher/curator/consumer surface:

    gip skills publish <dir>            validate + upload + register + submit
    gip skills list                     list records and their status
    gip skills approve <name>@<ver>     curator approve/reject (+ distribute)
    gip skills sync                     pull approved skills into harness dirs

This supersedes the documented-but-never-implemented ``gip plugins add/sync``
interface (assets/docs/PLUGINS.md) — the bootstrap ``/plugins`` feed is now
written by the distributor Lambda from approved registry records.

All registry API calls go through cli/utils/agent_registry.py (the
namespace-migration isolation point — bedrock-agentcore -> agent-registry,
Aug/Sep 2026).
"""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml
from botocore.exceptions import ClientError
from cleo.commands.command import Command
from cleo.helpers import argument, option
from rich.console import Console
from rich.table import Table

from governed_inference_platform.cli.utils.agent_registry import AgentRegistryClient
from governed_inference_platform.cli.utils.aws import get_stack_outputs
from governed_inference_platform.config import Config

# ---------------------------------------------------------------------------
# Frozen _meta schema v1 (R13 §5 / R15 §4.1 contract). Changing field names or
# semantics requires a new namespace version (io.gip.skill/v2), not an edit.
# ---------------------------------------------------------------------------

META_NAMESPACE = "io.gip.skill/v1"
META_FILE = "_meta.json"

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# R15 §4.1: model_compat[].status enum
COMPAT_STATUSES = ("verified", "drift", "unverified", "incompatible")

# Publish-side strictness: unknown keys are rejected to catch typos before a
# record is submitted (the registry itself allows unknown fields for forward
# compatibility — a future schema rides a new namespace, not loose keys here).
ALLOWED_META_KEYS = {
    "name",
    "version",
    "description",
    "category",
    "harnesses",
    "model_compat",
    "fork_of",
    "default_for_families",
    "evals",
}
ALLOWED_EVALS_KEYS = {"suite_ref", "latest_score_ref", "gate"}

# Files never shipped in a skill artifact
EXCLUDED_ARTIFACT_NAMES = {"__pycache__", ".DS_Store", ".git"}

# Per-harness sync matrix (R13 §4): `gip skills sync` materializes approved
# skills into the native skill directories. ~/.claude/skills is shared —
# Claude Code personal skills, and OpenCode reads it natively; ~/.codex/skills
# is the Codex CLI user dir (/etc/codex/skills is the MDM admin path, out of
# scope for a user-level sync).
HARNESS_SYNC_DIRS = {
    "claude-code": Path(".claude") / "skills",  # also read by OpenCode
    "codex": Path(".codex") / "skills",
}

SKILLS_LOCK_KEY = "distribution/skills-lock.json"
SKILLS_LOCK_SCHEMA_VERSION = 2
APPROVED_ARTIFACT_PREFIX = "approved/sha256/"
MAX_SKILL_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SKILL_ARCHIVE_MEMBERS = 1_000
MAX_SKILL_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_SKILL_COMPRESSION_RATIO = 100
MAX_SKILL_ARCHIVE_PATH_BYTES = 1_024
MAX_SKILLS_LOCK_BYTES = 1024 * 1024
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


# ---------------------------------------------------------------------------
# Validation helpers (module-level for testability)
# ---------------------------------------------------------------------------


def parse_skill_frontmatter(text: str) -> tuple[dict | None, list[str]]:
    """Parse SKILL.md YAML frontmatter. Returns (frontmatter, errors)."""
    errors: list[str] = []
    if not text.startswith("---"):
        return None, ["SKILL.md: missing YAML frontmatter (must start with '---')"]
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, ["SKILL.md: unterminated YAML frontmatter"]
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        return None, [f"SKILL.md: invalid YAML frontmatter: {e}"]
    if not isinstance(frontmatter, dict):
        return None, ["SKILL.md: frontmatter must be a YAML mapping"]
    # agentskills.io required fields
    if not frontmatter.get("name"):
        errors.append("SKILL.md: frontmatter 'name' is required")
    if not frontmatter.get("description"):
        errors.append("SKILL.md: frontmatter 'description' is required")
    return frontmatter, errors


def validate_meta(meta: dict, frontmatter: dict) -> list[str]:
    """Validate a _meta.json document against the frozen v1 schema."""
    errors: list[str] = []
    if not isinstance(meta, dict):
        return [f"{META_FILE}: must be a JSON object"]

    unknown = set(meta) - ALLOWED_META_KEYS
    if unknown:
        errors.append(f"{META_FILE}: unknown keys {sorted(unknown)} (frozen schema {META_NAMESPACE})")

    name = meta.get("name")
    if not name or not isinstance(name, str):
        errors.append(f"{META_FILE}: 'name' is required")
    elif not SKILL_NAME_RE.match(name):
        errors.append(f"{META_FILE}: 'name' must match {SKILL_NAME_RE.pattern} (got '{name}')")
    elif frontmatter.get("name") and frontmatter["name"] != name:
        errors.append(f"{META_FILE}: 'name' ('{name}') does not match SKILL.md frontmatter ('{frontmatter['name']}')")

    version = meta.get("version")
    if not version or not isinstance(version, str):
        errors.append(f"{META_FILE}: 'version' is required")
    elif not SEMVER_RE.match(version):
        errors.append(f"{META_FILE}: 'version' must be semver MAJOR.MINOR.PATCH (got '{version}')")

    for key in ("description", "category"):
        if key in meta and not isinstance(meta[key], str):
            errors.append(f"{META_FILE}: '{key}' must be a string")

    if "harnesses" in meta and not isinstance(meta["harnesses"], dict):
        errors.append(f"{META_FILE}: 'harnesses' must be an object of per-harness emit hints")

    families: list[str] = []
    model_compat = meta.get("model_compat", [])
    if not isinstance(model_compat, list):
        errors.append(f"{META_FILE}: 'model_compat' must be a list")
    else:
        for i, entry in enumerate(model_compat):
            if not isinstance(entry, dict):
                errors.append(f"{META_FILE}: model_compat[{i}] must be an object")
                continue
            family = entry.get("model_family")
            if not family or not isinstance(family, str):
                errors.append(f"{META_FILE}: model_compat[{i}].model_family is required")
            else:
                families.append(family)
            status = entry.get("status", "unverified")
            if status not in COMPAT_STATUSES:
                errors.append(
                    f"{META_FILE}: model_compat[{i}].status must be one of {COMPAT_STATUSES} (got '{status}')"
                )
            if "model_ids" in entry and not isinstance(entry["model_ids"], list):
                errors.append(f"{META_FILE}: model_compat[{i}].model_ids must be a list")

    fork_of = meta.get("fork_of")
    if fork_of is not None:
        if not isinstance(fork_of, dict) or not fork_of.get("skill") or not fork_of.get("version"):
            errors.append(f'{META_FILE}: \'fork_of\' must be null or {{"skill": ..., "version": ...}}')

    default_for = meta.get("default_for_families", [])
    if not isinstance(default_for, list):
        errors.append(f"{META_FILE}: 'default_for_families' must be a list")
    else:
        for family in default_for:
            if family not in families:
                errors.append(f"{META_FILE}: default_for_families entry '{family}' has no matching model_compat entry")

    evals = meta.get("evals")
    if evals is not None:
        if not isinstance(evals, dict):
            errors.append(f"{META_FILE}: 'evals' must be an object")
        else:
            unknown_evals = set(evals) - ALLOWED_EVALS_KEYS
            if unknown_evals:
                errors.append(f"{META_FILE}: evals has unknown keys {sorted(unknown_evals)}")
            gate = evals.get("gate")
            if gate is not None and not isinstance(gate, dict):
                errors.append(f'{META_FILE}: evals.gate must be an object (e.g. {{"min_score": null}})')

    return errors


def check_family_ownership(existing_records: list[dict], meta: dict) -> list[str]:
    """Enforce the R15 §4.2 write-time invariant: one ``default_for_families``
    owner per model family per skill name across all existing records."""
    errors: list[str] = []
    claimed = set(meta.get("default_for_families") or [])
    if not claimed:
        return errors
    for record in existing_records:
        other = extract_record_meta(record)
        if not other or other.get("name") != meta.get("name"):
            continue
        if other.get("version") == meta.get("version"):
            continue
        overlap = claimed & set(other.get("default_for_families") or [])
        if overlap:
            errors.append(
                f"default_for_families conflict: {sorted(overlap)} already owned by "
                f"{other.get('name')}@{other.get('version')} — move ownership in one publish"
            )
    return errors


def extract_record_meta(record: dict) -> dict | None:
    """Pull the io.gip.skill/v1 payload out of a registry record (defensive).

    Accepts both descriptor shapes: preview ``agentSkills.skillDefinition
    .inlineContent`` and GA ``agentSkillsDefinition.data`` (registry-faq
    "Change 3", retrieved 2026-07-29).
    """
    descriptors = record.get("descriptors", {})
    agent_skills = descriptors.get("agentSkills", {})
    definition = agent_skills.get("definition", {}) or record.get("definition", {})
    inline = None
    if not definition and isinstance(agent_skills.get("skillDefinition"), dict):
        inline = agent_skills["skillDefinition"].get("inlineContent")
    if not definition and inline is None and isinstance(descriptors.get("agentSkillsDefinition"), dict):
        inline = descriptors["agentSkillsDefinition"].get("data")
    if not definition and inline is not None:
        if isinstance(inline, str):
            try:
                definition = json.loads(inline)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"record {record.get('recordId', record.get('name', '?'))} has invalid skillDefinition JSON: {e}"
                ) from e
        elif isinstance(inline, dict):
            definition = inline
    if not isinstance(definition, dict):
        return None
    meta = (definition.get("_meta") or {}).get(META_NAMESPACE)
    return meta if isinstance(meta, dict) else None


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------


def iter_skill_files(skill_dir: Path):
    """Yield (path, posix_relpath) for every file shipped in the artifact."""
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if any(part in EXCLUDED_ARTIFACT_NAMES for part in rel.parts):
            continue
        yield path, rel.as_posix()


def build_artifact_zip(skill_dir: Path) -> bytes:
    """Deterministic zip of the skill directory (fixed timestamps, sorted
    entries) so the sha256 in _meta is stable across republish attempts."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in iter_skill_files(skill_dir):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _read_s3_body(response: dict, max_bytes: int, label: str) -> bytes:
    content_length = response.get("ContentLength")
    if isinstance(content_length, int) and content_length > max_bytes:
        raise ValueError(f"{label} is {content_length} bytes; maximum allowed is {max_bytes}")
    body = response["Body"]
    try:
        data = body.read(max_bytes + 1)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte maximum")
    return data


def _create_or_verify_source_object(s3, bucket: str, key: str, data: bytes, content_type: str | None = None):
    """Create a review source once, or verify an identical prior partial upload."""
    for attempt in range(3):
        request = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "IfNoneMatch": "*",
        }
        if content_type:
            request["ContentType"] = content_type
        try:
            return s3.put_object(**request).get("VersionId")
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                response = s3.get_object(Bucket=bucket, Key=key)
                try:
                    existing = _read_s3_body(response, len(data), f"s3://{bucket}/{key}")
                except ValueError as mismatch:
                    raise RuntimeError(
                        f"source artifact conflict at s3://{bucket}/{key}; bump the version to publish different bytes"
                    ) from mismatch
                if existing != data:
                    raise RuntimeError(
                        f"source artifact conflict at s3://{bucket}/{key}; bump the version to publish different bytes"
                    ) from error
                return response.get("VersionId")
            if (code == "ConditionalRequestConflict" or status == 409) and attempt < 2:
                continue
            raise
    raise RuntimeError(f"source artifact reservation retries exhausted for s3://{bucket}/{key}")


def _list_s3_keys(s3, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    token = None
    while True:
        request = {"Bucket": bucket, "Prefix": prefix}
        if token:
            request["ContinuationToken"] = token
        response = s3.list_objects_v2(**request)
        keys.update(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not token:
            raise RuntimeError("ListObjectsV2 returned a truncated page without NextContinuationToken")


def _parse_lock_entry(entry: dict, bucket: str) -> tuple[str, str, str, str, str]:
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid approved skill entry in lock file: {entry!r}")
    name, version = entry.get("name"), entry.get("version")
    digest, version_id = entry.get("sha256"), entry.get("version_id")
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid approved skill entry in lock file: {entry!r}")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ValueError(f"Invalid approved skill entry in lock file: {entry!r}")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError(f"Invalid SHA-256 for {name}@{version} in skills lock")
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise ValueError(f"Missing immutable S3 version for {name}@{version} in skills lock")

    uri = entry.get("s3_uri")
    parsed = urlparse(uri) if isinstance(uri, str) else None
    expected_key = f"{APPROVED_ARTIFACT_PREFIX}{digest}.zip"
    if (
        parsed is None
        or parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.path.lstrip("/") != expected_key
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Untrusted approved artifact URI for {name}@{version}: {uri!r}")
    return name, version, expected_key, version_id, digest


def _safe_archive_path(info: zipfile.ZipInfo) -> tuple[PurePosixPath, str]:
    raw_name = info.filename
    raw_parts = raw_name.split("/")
    if raw_name.endswith("/"):
        raw_parts = raw_parts[:-1]
    rel = PurePosixPath(raw_name)
    if (
        not raw_name
        or "\x00" in raw_name
        or "\\" in raw_name
        or len(raw_name.encode("utf-8")) > MAX_SKILL_ARCHIVE_PATH_BYTES
        or rel.is_absolute()
        or not raw_parts
        or any(part in {"", ".", ".."} or ":" in part for part in raw_parts)
        or any(part.endswith((" ", ".")) for part in raw_parts)
        or any(part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES for part in raw_parts)
    ):
        raise ValueError(f"Unsafe path in skill archive: {raw_name!r}")
    normalized = unicodedata.normalize("NFC", "/".join(raw_parts)).casefold()
    return PurePosixPath(*raw_parts), normalized


def _validate_skill_archive(
    archive: bytes,
    expected_name: str,
    expected_version: str,
) -> list[tuple[PurePosixPath, bytes]]:
    if len(archive) > MAX_SKILL_ARCHIVE_BYTES:
        raise ValueError(f"Skill archive is {len(archive)} bytes; maximum allowed is {MAX_SKILL_ARCHIVE_BYTES}")
    if not zipfile.is_zipfile(BytesIO(archive)):
        raise ValueError("Skill artifact is not a valid ZIP archive")

    try:
        with zipfile.ZipFile(BytesIO(archive)) as source:
            infos = source.infolist()
            if not infos:
                raise ValueError("Skill archive is empty")
            if len(infos) > MAX_SKILL_ARCHIVE_MEMBERS:
                raise ValueError(
                    f"Skill archive has {len(infos)} members; maximum allowed is {MAX_SKILL_ARCHIVE_MEMBERS}"
                )

            entries: list[tuple[zipfile.ZipInfo, PurePosixPath, str]] = []
            seen: dict[str, bool] = {}
            expanded_size = 0
            compressed_size = 0
            for info in infos:
                rel, normalized = _safe_archive_path(info)
                if normalized in seen:
                    raise ValueError(f"Duplicate path in skill archive: {info.filename!r}")
                seen[normalized] = not info.is_dir()
                if info.flag_bits & 0x1:
                    raise ValueError(f"Encrypted skill archive member is not supported: {info.filename!r}")
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                expected_types = {0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}
                if stat.S_ISLNK(mode) or file_type not in expected_types:
                    raise ValueError(f"Unsupported file type in skill archive: {info.filename!r}")
                if info.is_dir():
                    continue
                expanded_size += info.file_size
                compressed_size += info.compress_size
                if expanded_size > MAX_SKILL_EXPANDED_BYTES:
                    raise ValueError(f"Skill archive expands beyond {MAX_SKILL_EXPANDED_BYTES} bytes")
                if info.file_size and (
                    info.compress_size <= 0 or info.file_size / info.compress_size > MAX_SKILL_COMPRESSION_RATIO
                ):
                    raise ValueError(
                        f"Skill archive member exceeds {MAX_SKILL_COMPRESSION_RATIO}:1 "
                        f"compression ratio: {info.filename!r}"
                    )
                entries.append((info, rel, normalized))

            if expanded_size and (
                compressed_size <= 0 or expanded_size / compressed_size > MAX_SKILL_COMPRESSION_RATIO
            ):
                raise ValueError(f"Skill archive exceeds {MAX_SKILL_COMPRESSION_RATIO}:1 total compression ratio")

            file_paths = {normalized for _, _, normalized in entries}
            for _, rel, _ in entries:
                parent_parts = rel.parts[:-1]
                for index in range(1, len(parent_parts) + 1):
                    parent = unicodedata.normalize("NFC", "/".join(parent_parts[:index])).casefold()
                    if parent in file_paths:
                        raise ValueError(f"File/directory path conflict in skill archive: {str(rel)!r}")

            members = []
            # The cross-platform scanner treats any `.open()` call as text I/O.
            open_member = source.open
            for info, rel, _ in entries:
                with open_member(info) as member:
                    data = member.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise ValueError(f"Invalid expanded size for skill archive member: {info.filename!r}")
                members.append((rel, data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError) as error:
        raise ValueError(f"Skill ZIP validation failed: {error}") from error

    by_name = {path.as_posix(): data for path, data in members}
    missing = [required for required in ("SKILL.md", META_FILE) if required not in by_name]
    if missing:
        raise ValueError(f"Skill archive is missing required root file(s): {', '.join(missing)}")
    try:
        skill_markdown = by_name["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Skill archive SKILL.md is not UTF-8") from error
    if not skill_markdown.startswith("---") or len(skill_markdown.split("---", 2)) < 3:
        raise ValueError("Skill archive SKILL.md is missing complete YAML frontmatter")
    try:
        archive_meta = json.loads(by_name[META_FILE])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Skill archive {META_FILE} is invalid JSON") from error
    if not isinstance(archive_meta, dict) or (
        archive_meta.get("name") != expected_name or archive_meta.get("version") != expected_version
    ):
        raise ValueError(f"Skill archive {META_FILE} does not match {expected_name}@{expected_version}")
    return members


def _extract_skill_archive(
    members: list[tuple[PurePosixPath, bytes]],
    destination: Path,
) -> None:
    destination_resolved = destination.resolve()
    for rel, data in members:
        target = (destination / Path(*rel.parts)).resolve()
        if not target.is_relative_to(destination_resolved):
            raise ValueError(f"Unsafe path in skill archive: {str(rel)!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _commit_staged_installs(installs: list[tuple[Path, Path]]) -> None:
    committed: list[tuple[Path, Path | None]] = []
    try:
        for stage, destination in installs:
            backup = stage.with_name(f"{stage.name}.previous")
            had_destination = destination.exists() or destination.is_symlink()
            if had_destination:
                os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except Exception:
                if had_destination:
                    os.replace(backup, destination)
                raise
            committed.append((destination, backup if had_destination else None))
    except Exception:
        for destination, backup in reversed(committed):
            _remove_path(destination)
            if backup is not None:
                os.replace(backup, destination)
        raise
    for _, backup in committed:
        if backup is not None:
            try:
                _remove_path(backup)
            except Exception as error:
                print(f"WARNING: skill update committed, but backup cleanup failed for {backup}: {error}")


def build_record_definition(meta: dict, artifact: dict, published_by: str, published_at: str) -> dict:
    """Build the AgentSkills record definition carrying the _meta payload."""
    payload = dict(meta)
    payload["artifact"] = artifact
    payload["lifecycle"] = {"published_by": published_by, "published_at": published_at}
    return {"schemaVersion": "0.1.0", "_meta": {META_NAMESPACE: payload}}


def wait_for_record_ready(
    registry, registry_id: str, record_id: str, timeout_seconds: int = 60, poll_seconds: float = 2.0
):
    """Wait until CreateRegistryRecord leaves CREATING before approval."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        record = registry.get_record(registry_id, record_id)
        status = record.get("status") if isinstance(record, dict) else None
        if status in {"DRAFT", "PENDING_APPROVAL", "APPROVED"}:
            return status
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "REJECTED", "DEPRECATED"}:
            raise RuntimeError(f"registry record {record_id} entered {status}: {record.get('statusReason', '')}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"registry record {record_id} did not become approvable (last status: {status})")
        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Shared context resolution
# ---------------------------------------------------------------------------


def resolve_skills_context(profile) -> tuple[dict | None, str | None]:
    """Resolve registry id / artifact bucket / distributor ARN.

    Profile fields (populated by ``gip deploy skills``) win; stack outputs
    are the fallback. Returns (context, error_message).
    """
    registry_id = getattr(profile, "skills_registry_id", "") or ""
    bucket = getattr(profile, "skills_artifact_bucket", "") or ""
    distributor = getattr(profile, "skills_distributor_function", "") or ""

    if not (registry_id and bucket and distributor):
        stack_name = profile.stack_names.get("skills", f"{profile.identity_pool_name}-skills")
        outputs = get_stack_outputs(stack_name, profile.aws_region) or {}
        registry_id = registry_id or outputs.get("RegistryId", "")
        bucket = bucket or outputs.get("ArtifactBucket", "")
        distributor = distributor or outputs.get("DistributorFunctionArn", "")

    if not (registry_id and bucket):
        return None, (
            "Skills registry is not deployed for this profile. "
            "Enable it ('gip init' → Skills Registry) and run 'gip deploy skills'."
        )
    return {
        "registry_id": registry_id,
        "artifact_bucket": bucket,
        "distributor_arn": distributor,
        "region": profile.aws_region,
    }, None


def _load_profile(command) -> tuple[object | None, str | None]:
    config = Config.load()
    profile_name = command.option("profile") or config.active_profile
    profile = config.get_profile(profile_name)
    if not profile:
        return None, "No profile found. Run 'gip init' first."
    return profile, None


def _caller_identity() -> str:
    try:
        import boto3

        return boto3.client("sts").get_caller_identity().get("Arn", "unknown")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class SkillsCommand(Command):
    name = "skills"
    description = "Manage the governed skills registry (publish, list, approve, sync)"

    def handle(self) -> int:
        console = Console()
        console.print("[bold]Skills registry commands[/bold]\n")
        console.print("  gip skills publish <dir>          Validate, upload, and submit a skill for approval")
        console.print("  gip skills list                   List registry records and their status")
        console.print("  gip skills approve <name>@<ver>   Approve (or --reject) a pending skill [curator]")
        console.print("  gip skills sync                   Pull approved skills into local harness dirs")
        console.print("  gip skills eval <dir>             Run the deterministic eval suite (eval.yaml)")
        console.print("\n[dim]See assets/docs/SKILLS_REGISTRY.md for the governance model.[/dim]")
        return 0


class SkillsPublishCommand(Command):
    name = "skills publish"
    description = "Validate SKILL.md + _meta.json, upload the artifact, and submit for approval"

    arguments = [argument("directory", description="Skill directory containing SKILL.md and _meta.json")]
    options = [
        option("profile", description="Configuration profile to use", flag=False, default=None),
    ]

    def handle(self) -> int:
        console = Console()
        profile, err = _load_profile(self)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1
        context, err = resolve_skills_context(profile)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1

        skill_dir = Path(self.argument("directory")).expanduser()
        if not skill_dir.is_dir():
            console.print(f"[red]Not a directory: {skill_dir}[/red]")
            return 1

        skill_md_path = skill_dir / "SKILL.md"
        meta_path = skill_dir / META_FILE
        errors: list[str] = []
        if not skill_md_path.is_file():
            console.print(f"[red]SKILL.md not found in {skill_dir}[/red]")
            return 1
        if not meta_path.is_file():
            console.print(f"[red]{META_FILE} not found in {skill_dir} (see assets/docs/SKILLS_REGISTRY.md)[/red]")
            return 1

        skill_md = skill_md_path.read_text(encoding="utf-8")
        frontmatter, fm_errors = parse_skill_frontmatter(skill_md)
        errors.extend(fm_errors)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[red]{META_FILE}: invalid JSON: {e}[/red]")
            return 1
        errors.extend(validate_meta(meta, frontmatter or {}))
        if errors:
            console.print("[red]Validation failed:[/red]")
            for e in errors:
                console.print(f"  [red]✗[/red] {e}")
            return 1

        name, version = meta["name"], meta["version"]
        registry = AgentRegistryClient(region=context["region"])

        # Write-time invariant (R15 §4.2): one default_for_families owner per
        # family per skill, checked against all existing records.
        existing = registry.list_records(context["registry_id"])
        try:
            ownership_errors = check_family_ownership(existing, meta)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if ownership_errors:
            for e in ownership_errors:
                console.print(f"  [red]✗[/red] {e}")
            return 1

        import boto3

        s3 = boto3.client("s3", region_name=context["region"])
        bucket = context["artifact_bucket"]
        prefix = f"skills/{name}/{version}/"
        zip_key = f"skills/{name}/{version}.zip"

        zip_bytes = build_artifact_zip(skill_dir)
        sha256 = hashlib.sha256(zip_bytes).hexdigest()
        try:
            members = _validate_skill_archive(zip_bytes, name, version)
        except ValueError as error:
            console.print(f"[red]Artifact validation failed: {error}[/red]")
            return 1

        try:
            # Reserve the canonical ZIP first. A retry may resume only when
            # every object already present has exactly the same bytes.
            source_version_id = _create_or_verify_source_object(
                s3,
                bucket,
                zip_key,
                zip_bytes,
                "application/zip",
            )
        except (ClientError, RuntimeError, ValueError) as error:
            console.print(f"[red]Artifact upload failed: {error}[/red]")
            return 1
        if not source_version_id or source_version_id == "null":
            console.print(
                "[red]S3 did not return an immutable version for the canonical ZIP; "
                "the skill was not registered. Confirm bucket versioning and publish a new version.[/red]"
            )
            return 1

        source_objects = {f"{prefix}{path.as_posix()}": data for path, data in members}
        try:
            for key, data in source_objects.items():
                _create_or_verify_source_object(s3, bucket, key, data)
            observed_keys = _list_s3_keys(s3, bucket, prefix)
            if observed_keys != set(source_objects):
                missing = sorted(set(source_objects) - observed_keys)
                unexpected = sorted(observed_keys - set(source_objects))
                raise RuntimeError(
                    f"review-source directory s3://{bucket}/{prefix} does not exactly match the canonical ZIP "
                    f"(missing={missing}, unexpected={unexpected})"
                )
        except (ClientError, RuntimeError, ValueError) as error:
            console.print(f"[red]Artifact upload failed: {error}[/red]")
            return 1
        console.print(
            f"[green]✓[/green] Reserved or verified {len(source_objects)} file(s) + canonical zip "
            f"at s3://{bucket}/{prefix}"
        )

        from datetime import datetime, timezone

        region = context["region"]
        artifact = {
            "s3_uri": f"s3://{bucket}/{prefix}",
            "source_directory_s3_uri": f"s3://{bucket}/{prefix}",
            "source_s3_uri": f"s3://{bucket}/{zip_key}",
            "source_version_id": source_version_id,
            "zip_url": f"https://{bucket}.s3.{region}.amazonaws.com/{zip_key}",
            "sha256": sha256,
            "size_bytes": len(zip_bytes),
            "signature": None,  # hook: {"type": "kms", "key_arn": ..., "sig": ...}
        }
        definition = build_record_definition(meta, artifact, _caller_identity(), datetime.now(timezone.utc).isoformat())

        record_id = registry.create_record(
            context["registry_id"],
            name=name,
            record_version=version,
            skill_markdown=skill_md,
            definition=definition,
            description=meta.get("description") or (frontmatter or {}).get("description", ""),
        )
        try:
            ready_status = wait_for_record_ready(registry, context["registry_id"], record_id)
        except (RuntimeError, TimeoutError) as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if ready_status == "DRAFT":
            registry.submit_for_approval(context["registry_id"], record_id)
        console.print(f"[green]✓[/green] Registered {name}@{version} (record {record_id}) — submitted for approval")
        console.print("[dim]A curator must approve it: gip skills approve " + f"{name}@{version}[/dim]")
        return 0


class SkillsListCommand(Command):
    name = "skills list"
    description = "List skills registry records and their approval status"

    options = [
        option("profile", description="Configuration profile to use", flag=False, default=None),
        option("status", description="Filter by status (e.g. APPROVED, PENDING_APPROVAL)", flag=False, default=None),
    ]

    def handle(self) -> int:
        console = Console()
        profile, err = _load_profile(self)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1
        context, err = resolve_skills_context(profile)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1

        registry = AgentRegistryClient(region=context["region"])
        records = registry.list_records(context["registry_id"], status=self.option("status"))

        table = Table(title=f"Skills registry ({context['registry_id']})")
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Status")
        table.add_column("Families (default)")
        table.add_column("Artifact")
        for record in records:
            try:
                meta = extract_record_meta(record) or {}
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                return 1
            table.add_row(
                meta.get("name") or record.get("name", "?"),
                meta.get("version") or record.get("recordVersion", "?"),
                record.get("status", "?"),
                ", ".join(meta.get("default_for_families") or []),
                (meta.get("artifact") or {}).get("s3_uri", ""),
            )
        console.print(table)
        return 0


class SkillsApproveCommand(Command):
    name = "skills approve"
    description = "Approve (or reject) a pending skill record and trigger distribution [curator]"

    arguments = [argument("skill", description="Skill to decide, as <name>@<version>")]
    options = [
        option("profile", description="Configuration profile to use", flag=False, default=None),
        option("reject", description="Reject instead of approve", flag=True),
        option("reason", description="Decision reason (recorded in the registry)", flag=False, default=None),
        option("no-distribute", description="Approve atomically but defer distribution output refresh", flag=True),
    ]

    def handle(self) -> int:
        console = Console()
        profile, err = _load_profile(self)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1
        context, err = resolve_skills_context(profile)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1

        spec = self.argument("skill")
        if "@" not in spec:
            console.print("[red]Expected <name>@<version> (e.g. code-review@1.2.0)[/red]")
            return 1
        name, _, version = spec.partition("@")

        registry = AgentRegistryClient(region=context["region"])
        records = registry.list_records(context["registry_id"])
        try:
            record = _find_record(records, name, version)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if not record:
            console.print(f"[red]No registry record found for {name}@{version}[/red]")
            return 1

        status = "REJECTED" if self.option("reject") else "APPROVED"
        distributor = context.get("distributor_arn")
        if not distributor:
            console.print(
                "[red]Distributor ARN unknown — redeploy the skills stack before approving or rejecting records.[/red]"
            )
            return 1

        import boto3

        request = {
            "action": "reject" if status == "REJECTED" else "approve",
            "record_id": record.get("recordId"),
            "reason": self.option("reason") or "",
            "distribute": status == "APPROVED" and not self.option("no-distribute"),
        }
        response = boto3.client("lambda", region_name=context["region"]).invoke(
            FunctionName=distributor,
            InvocationType="RequestResponse",
            Payload=json.dumps(request).encode("utf-8"),
        )
        if not isinstance(response, dict):
            console.print("[red]Distributor invocation returned an unexpected response shape.[/red]")
            return 1
        payload_text = ""
        payload = response.get("Payload")
        if payload is not None and hasattr(payload, "read"):
            payload_text = payload.read().decode("utf-8", errors="replace")
        status_code = response.get("StatusCode")
        if response.get("FunctionError") or (status_code is not None and status_code >= 300):
            console.print(
                "[red]Distributor invocation failed; the requested status transition was not confirmed.[/red]"
            )
            if payload_text:
                console.print(f"[dim]{payload_text[:500]}[/dim]")
            return 1
        try:
            result = json.loads(payload_text) if payload_text else None
        except json.JSONDecodeError as e:
            console.print(f"[red]Distributor returned invalid JSON: {e}[/red]")
            return 1
        if not isinstance(result, dict):
            console.print("[red]Distributor did not return its decision contract.[/red]")
            return 1
        if result.get("status") != status:
            console.print(f"[red]Distributor did not confirm the {status} status transition.[/red]")
            return 1

        console.print(f"[green]✓[/green] {name}@{version} → {status}")
        if status == "REJECTED":
            return 0
        if self.option("no-distribute"):
            console.print(
                "[yellow]⚠ Immutable artifacts were promoted; distribution outputs will refresh on the schedule.[/yellow]"
            )
            return 0
        if result.get("skills", 0) < 1:
            console.print("[red]Distributor wrote zero approved skills; distribution outputs may be stale.[/red]")
            return 1
        distributed = result.get("distributed_skills")
        expected_skill = {"name": name, "version": version}
        if not isinstance(distributed, list):
            console.print(
                "[red]Distributor returned an invalid distributed_skills contract; distribution may be stale.[/red]"
            )
            return 1
        if expected_skill not in distributed:
            approved = result.get("approved_skills")
            if (
                result.get("active_version_rule") == "highest-semver"
                and isinstance(approved, list)
                and expected_skill in approved
            ):
                console.print(
                    f"[yellow]⚠ {name}@{version} is approved but inactive; "
                    "the highest approved semantic version remains distributed.[/yellow]"
                )
            else:
                console.print(f"[red]Distributor did not confirm {name}@{version}; distribution may be stale.[/red]")
                return 1
        written = result.get("written_outputs")
        if not isinstance(written, list) or not written:
            console.print("[red]Distributor did not confirm any written outputs; distribution may be stale.[/red]")
            return 1
        required_outputs = ("distribution/marketplace.json", "distribution/skills-lock.json")
        missing_outputs = [required for required in required_outputs if not any(required in uri for uri in written)]
        if missing_outputs:
            console.print(f"[red]Distributor did not confirm required output(s): {', '.join(missing_outputs)}[/red]")
            return 1
        skipped = result.get("skipped_outputs")
        console.print(f"[green]✓[/green] Distributor wrote: {', '.join(written)}")
        if skipped:
            console.print(f"[yellow]⚠ Distributor skipped optional outputs: {', '.join(skipped)}[/yellow]")
        console.print("[green]✓[/green] Distributor invoked")
        return 0


def _find_record(records: list[dict], name: str, version: str) -> dict | None:
    """Match a record by _meta name/version, falling back to record fields."""
    for record in records:
        meta = extract_record_meta(record) or {}
        if meta.get("name") == name and meta.get("version") == version:
            return record
        if record.get("name") == name and record.get("recordVersion") == version:
            return record
    return None


class SkillsSyncCommand(Command):
    name = "skills sync"
    description = "Pull approved skills into local harness skill directories"

    options = [
        option("profile", description="Configuration profile to use", flag=False, default=None),
        option(
            "harness",
            description=f"Only sync one harness ({'/'.join(HARNESS_SYNC_DIRS)})",
            flag=False,
            default=None,
        ),
    ]

    def handle(self) -> int:
        console = Console()
        profile, err = _load_profile(self)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1
        context, err = resolve_skills_context(profile)
        if err:
            console.print(f"[red]{err}[/red]")
            return 1

        harness = self.option("harness")
        if harness and harness not in HARNESS_SYNC_DIRS:
            console.print(f"[red]Unknown harness '{harness}'. Valid: {', '.join(HARNESS_SYNC_DIRS)}[/red]")
            return 1
        targets = {harness: HARNESS_SYNC_DIRS[harness]} if harness else dict(HARNESS_SYNC_DIRS)

        import boto3

        s3 = boto3.client("s3", region_name=context["region"])
        bucket = context["artifact_bucket"]
        try:
            lock_obj = s3.get_object(Bucket=bucket, Key=SKILLS_LOCK_KEY)
            lock = json.loads(
                _read_s3_body(
                    lock_obj,
                    MAX_SKILLS_LOCK_BYTES,
                    f"s3://{bucket}/{SKILLS_LOCK_KEY}",
                )
            )
        except Exception as e:
            console.print(f"[red]Could not read s3://{bucket}/{SKILLS_LOCK_KEY}: {e}[/red]")
            console.print("[dim]Approve at least one skill first (the distributor writes the lock file).[/dim]")
            return 1

        if not isinstance(lock, dict) or lock.get("schema_version") != SKILLS_LOCK_SCHEMA_VERSION:
            console.print(
                f"[red]Unsupported skills lock schema. Expected {SKILLS_LOCK_SCHEMA_VERSION}; "
                "run the updated distributor. Legacy approvals without a matching source object version require re-review.[/red]"
            )
            return 1
        skills = lock.get("skills", [])
        if not isinstance(skills, list):
            console.print("[red]Invalid skills lock: 'skills' must be a list.[/red]")
            return 1
        if not skills:
            console.print("[yellow]No approved skills to sync.[/yellow]")
            return 0

        home = Path.home()
        staged: list[tuple[Path, Path]] = []
        messages: list[tuple[str, str, Path, str]] = []
        destinations: set[Path] = set()
        try:
            parsed_entries = [_parse_lock_entry(entry, bucket) for entry in skills]
            seen_names = set()
            duplicate_names = set()
            for name, _, _, _, _ in parsed_entries:
                if name in seen_names:
                    duplicate_names.add(name)
                seen_names.add(name)
            if duplicate_names:
                raise ValueError(
                    "Skills lock contains multiple active versions for: "
                    f"{', '.join(sorted(duplicate_names))}; "
                    "deprecate duplicate approved records and rerun distribution"
                )
            for name, version, key, version_id, digest in parsed_entries:
                artifact = _read_s3_body(
                    s3.get_object(Bucket=bucket, Key=key, VersionId=version_id),
                    MAX_SKILL_ARCHIVE_BYTES,
                    f"s3://{bucket}/{key}?versionId={version_id}",
                )
                observed = hashlib.sha256(artifact).hexdigest()
                if observed != digest:
                    raise ValueError(f"SHA-256 mismatch for {name}@{version} (lock {digest}, downloaded {observed})")
                members = _validate_skill_archive(artifact, name, version)
                for harness_name, rel_dir in targets.items():
                    sync_root = (home / rel_dir).resolve()
                    destination = (sync_root / name).resolve()
                    if not destination.is_relative_to(sync_root):
                        raise ValueError(f"Invalid sync destination for {name}@{version}: {destination}")
                    if destination in destinations:
                        raise ValueError(f"Skills lock contains multiple versions for destination {destination}")
                    destinations.add(destination)
                    sync_root.mkdir(parents=True, exist_ok=True)
                    stage = Path(tempfile.mkdtemp(prefix=f".{name}.gip-", dir=sync_root))
                    try:
                        _extract_skill_archive(members, stage)
                    except Exception:
                        _remove_path(stage)
                        raise
                    staged.append((stage, destination))
                    messages.append((name, version, destination, harness_name))
            _commit_staged_installs(staged)
        except Exception as e:
            for stage, _ in staged:
                _remove_path(stage)
            console.print(f"[red]Skill sync failed; existing installations were preserved: {e}[/red]")
            return 1

        for name, version, destination, harness_name in messages:
            console.print(f"[green]✓[/green] {name}@{version} → {destination} ({harness_name})")
        console.print(f"\n[bold]{len(skills)}/{len(skills)} skill(s) synced.[/bold]")
        return 0
