# ABOUTME: Distributor Lambda verifies and promotes APPROVED skills to content-addressed S3 objects
# ABOUTME: Emits plugins-registry.json (CoWork), marketplace.json (Claude Code), skills-lock.json (sync)

"""Skills distributor.

The registry is the approval source of truth. Curator requests are mediated by
this function so the exact versioned source ZIP is verified and promoted before
the immutable descriptor revision becomes APPROVED. Scheduled runs verify all
approved records before rendering outputs. Any verification or write failure
leaves the prior skills lock unchanged.

PINNED OUTPUT FORMATS (regression-critical):
1. plugins-registry.json  -> {"plugins": [{"name","version","url"}]}
   The exact document the (now-removed) legacy bootstrap device-code
   Lambda served verbatim at /plugins/index.json (that template is not
   included in this sample).
2. marketplace.json       -> Claude Code plugin marketplace shape, mirroring
   the vendored .claude-plugin/marketplace.json:
   {"$schema","name","version","description","owner":{"name"},
    "plugins":[{"name","version","source","description","category"}]}
3. skills-lock.json       -> {"schema_version",2,"generated_at",
   "skills":[{"name","version","s3_uri","version_id","sha256"}]}
   — version-pinned feed for `gip skills sync`.
"""

import datetime
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
import zipfile
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urlparse

from botocore.config import Config
from botocore.exceptions import ClientError
from registry_client import RegistryClient, SERVICE_NAME

# Frozen _meta extension namespace (R13 §5). Version bump = new namespace.
META_NAMESPACE = "io.gip.skill/v1"

MARKETPLACE_SCHEMA = "https://anthropic.com/claude-code/marketplace.schema.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
APPROVED_PREFIX = "approved/sha256/"
ACTIVE_VERSION_RULE = "highest-semver"
MAX_SKILL_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SKILL_ARCHIVE_MEMBERS = 1_000
MAX_SKILL_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_SKILL_COMPRESSION_RATIO = 100
MAX_SKILL_ARCHIVE_PATH_BYTES = 1_024
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
AWS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"total_max_attempts": 3, "mode": "standard"},
)


def _region():
    return os.environ.get("AWS_REGION", "us-east-1")


def _record_definition(record):
    """Accepts both descriptor shapes: preview ``agentSkills.skillDefinition
    .inlineContent`` and GA ``agentSkillsDefinition.data`` (registry-faq
    "Change 3", retrieved 2026-07-29)."""
    descriptors = record.get("descriptors", {})
    agent_skills = descriptors.get("agentSkills", {})
    definition = agent_skills.get("definition", {}) or record.get("definition", {})
    inline = None
    if not definition and isinstance(agent_skills.get("skillDefinition"), dict):
        inline = agent_skills["skillDefinition"].get("inlineContent")
    if (
        not definition
        and inline is None
        and isinstance(descriptors.get("agentSkillsDefinition"), dict)
    ):
        inline = descriptors["agentSkillsDefinition"].get("data")
    if not definition and inline is not None:
        if isinstance(inline, str):
            try:
                definition = json.loads(inline)
            except json.JSONDecodeError as e:
                print(
                    f"WARNING: record {record.get('recordId', record.get('name'))} has invalid "
                    f"skillDefinition JSON: {e} — skipped"
                )
                return None
        elif isinstance(inline, dict):
            definition = inline
    return definition


def extract_skill(record):
    """Pull the gip _meta payload out of a registry record.

    Returns normalized review-source metadata or None when the record does not
    carry valid _meta. The handler fails the run if any approved record is
    malformed so callers do not see a false refreshed success.
    """
    definition = _record_definition(record)
    if not isinstance(definition, dict):
        print(
            f"WARNING: record {record.get('recordId', record.get('name'))} skillDefinition is not an object — skipped"
        )
        return None
    meta = (definition.get("_meta") or {}).get(META_NAMESPACE)
    if not isinstance(meta, dict):
        print(
            f"WARNING: record {record.get('recordId', record.get('name'))} has no {META_NAMESPACE} _meta — skipped"
        )
        return None

    name = meta.get("name")
    version = meta.get("version")
    artifact = meta.get("artifact") or {}
    s3_uri = artifact.get("s3_uri")
    digest = artifact.get("sha256")
    if not (
        isinstance(name, str)
        and SKILL_NAME_RE.fullmatch(name)
        and isinstance(version, str)
        and SEMVER_RE.fullmatch(version)
        and s3_uri
        and isinstance(digest, str)
        and SHA256_RE.fullmatch(digest)
    ):
        print(
            f"WARNING: record {record.get('recordId', name)} _meta missing "
            "name/version/artifact.s3_uri/valid sha256 — skipped"
        )
        return None

    source_version_id = artifact.get("source_version_id")
    if source_version_id is not None and (
        not isinstance(source_version_id, str)
        or not source_version_id
        or source_version_id == "null"
    ):
        print(
            f"WARNING: record {record.get('recordId', name)} has invalid source_version_id — skipped"
        )
        return None

    size_bytes = artifact.get("size_bytes")
    if size_bytes is not None and (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
    ):
        print(
            f"WARNING: record {record.get('recordId', name)} has invalid size_bytes — skipped"
        )
        return None

    return {
        "name": name,
        "version": version,
        "description": meta.get("description") or record.get("description", ""),
        "category": meta.get("category") or "skill",
        "s3_uri": s3_uri,
        "sha256": digest,
        "size_bytes": size_bytes,
        "source_directory_s3_uri": artifact.get("source_directory_s3_uri"),
        "source_s3_uri": artifact.get("source_s3_uri"),
        "source_version_id": source_version_id,
        "zip_url": artifact.get("zip_url") or _default_zip_url(name, version),
        "record_id": record.get("recordId") or record.get("recordArn"),
    }


def _default_zip_url(name, version):
    """HTTPS URL of the canonical zip uploaded by `gip skills publish`."""
    bucket = os.environ.get("ARTIFACT_BUCKET", "")
    return f"https://{bucket}.s3.{_region()}.amazonaws.com/skills/{name}/{version}.zip"


def _parse_s3_uri(uri):
    parsed = urlparse(uri)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or not parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"invalid S3 URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _read_object(s3, bucket, key, version_id=None, max_bytes=None):
    kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = s3.get_object(**kwargs)
    content_length = response.get("ContentLength")
    if (
        max_bytes is not None
        and isinstance(content_length, int)
        and content_length > max_bytes
    ):
        raise RuntimeError(
            f"s3://{bucket}/{key} is {content_length} bytes; maximum allowed is {max_bytes}"
        )
    body = response["Body"]
    try:
        data = body.read(max_bytes + 1) if max_bytes is not None else body.read()
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    if max_bytes is not None and len(data) > max_bytes:
        raise RuntimeError(f"s3://{bucket}/{key} exceeds the {max_bytes}-byte maximum")
    return data, response.get("VersionId") or version_id


def _bytes_match(skill, data):
    if hashlib.sha256(data).hexdigest() != skill["sha256"]:
        return False
    expected_size = skill.get("size_bytes")
    return expected_size is None or expected_size == len(data)


def _safe_archive_path(info):
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
        or any(
            part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in raw_parts
        )
    ):
        raise RuntimeError(f"unsafe path in skill archive: {raw_name!r}")
    normalized = unicodedata.normalize("NFC", "/".join(raw_parts)).casefold()
    return PurePosixPath(*raw_parts), normalized


def _validated_archive_members(archive, skill):
    if len(archive) > MAX_SKILL_ARCHIVE_BYTES:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} archive is {len(archive)} bytes; "
            f"maximum allowed is {MAX_SKILL_ARCHIVE_BYTES}"
        )
    if not zipfile.is_zipfile(BytesIO(archive)):
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} artifact is not a valid ZIP archive"
        )

    try:
        with zipfile.ZipFile(BytesIO(archive)) as source:
            infos = source.infolist()
            if not infos:
                raise RuntimeError(
                    f"{skill['name']}@{skill['version']} archive is empty"
                )
            if len(infos) > MAX_SKILL_ARCHIVE_MEMBERS:
                raise RuntimeError(
                    f"{skill['name']}@{skill['version']} archive has {len(infos)} members; "
                    f"maximum allowed is {MAX_SKILL_ARCHIVE_MEMBERS}"
                )

            entries = []
            seen = {}
            expanded_size = 0
            compressed_size = 0
            for info in infos:
                rel, normalized = _safe_archive_path(info)
                if normalized in seen:
                    raise RuntimeError(
                        f"duplicate path in skill archive: {info.filename!r}"
                    )
                seen[normalized] = not info.is_dir()
                if info.flag_bits & 0x1:
                    raise RuntimeError(
                        f"encrypted skill archive member is not supported: {info.filename!r}"
                    )
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                expected_types = (
                    {0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}
                )
                if stat.S_ISLNK(mode) or file_type not in expected_types:
                    raise RuntimeError(
                        f"unsupported file type in skill archive: {info.filename!r}"
                    )
                if info.is_dir():
                    continue
                expanded_size += info.file_size
                compressed_size += info.compress_size
                if expanded_size > MAX_SKILL_EXPANDED_BYTES:
                    raise RuntimeError(
                        f"{skill['name']}@{skill['version']} archive expands beyond "
                        f"{MAX_SKILL_EXPANDED_BYTES} bytes"
                    )
                if info.file_size and (
                    info.compress_size <= 0
                    or info.file_size / info.compress_size > MAX_SKILL_COMPRESSION_RATIO
                ):
                    raise RuntimeError(
                        f"skill archive member exceeds {MAX_SKILL_COMPRESSION_RATIO}:1 "
                        f"compression ratio: {info.filename!r}"
                    )
                entries.append((info, rel, normalized))

            if expanded_size and (
                compressed_size <= 0
                or expanded_size / compressed_size > MAX_SKILL_COMPRESSION_RATIO
            ):
                raise RuntimeError(
                    f"{skill['name']}@{skill['version']} archive exceeds "
                    f"{MAX_SKILL_COMPRESSION_RATIO}:1 total compression ratio"
                )

            file_paths = {normalized for _, _, normalized in entries}
            for _, rel, _ in entries:
                parent_parts = rel.parts[:-1]
                for index in range(1, len(parent_parts) + 1):
                    parent = unicodedata.normalize(
                        "NFC", "/".join(parent_parts[:index])
                    ).casefold()
                    if parent in file_paths:
                        raise RuntimeError(
                            f"file/directory path conflict in skill archive: {str(rel)!r}"
                        )

            members = []
            for info, rel, _ in entries:
                with source.open(info) as member:
                    data = member.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise RuntimeError(
                        f"invalid expanded size for skill archive member: {info.filename!r}"
                    )
                members.append((rel.as_posix(), data))
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        NotImplementedError,
        ValueError,
    ) as error:
        if isinstance(error, RuntimeError) and str(error).startswith(
            f"{skill['name']}@"
        ):
            raise
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} ZIP validation failed: {error}"
        ) from error

    by_name = dict(members)
    missing = [
        required for required in ("SKILL.md", "_meta.json") if required not in by_name
    ]
    if missing:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} archive is missing required root file(s): "
            f"{', '.join(missing)}"
        )
    try:
        skill_markdown = by_name["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} SKILL.md is not UTF-8"
        ) from error
    if not skill_markdown.startswith("---") or len(skill_markdown.split("---", 2)) < 3:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} SKILL.md is missing complete YAML frontmatter"
        )
    try:
        archive_meta = json.loads(by_name["_meta.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} _meta.json is invalid JSON"
        ) from error
    if not isinstance(archive_meta, dict) or (
        archive_meta.get("name") != skill["name"]
        or archive_meta.get("version") != skill["version"]
    ):
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} archive _meta.json does not match the approved name/version"
        )
    return members


def _source_location(skill, artifact_bucket):
    expected_approved_prefix = (
        f"s3://{artifact_bucket}/{APPROVED_PREFIX}{skill['sha256']}/"
    )
    expected_source_prefix = (
        f"s3://{artifact_bucket}/skills/{skill['name']}/{skill['version']}/"
    )
    if skill["s3_uri"] not in {expected_approved_prefix, expected_source_prefix}:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} has an unexpected artifact location; re-review required"
        )
    source_directory_uri = skill.get("source_directory_s3_uri")
    if (
        source_directory_uri is not None
        and source_directory_uri != expected_source_prefix
    ):
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} has an unexpected review-source directory; re-review required"
        )
    expected_key = f"skills/{skill['name']}/{skill['version']}.zip"
    source_uri = skill.get("source_s3_uri") or f"s3://{artifact_bucket}/{expected_key}"
    source_bucket, source_key = _parse_s3_uri(source_uri)
    if source_bucket != artifact_bucket or source_key != expected_key:
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} has an unexpected canonical ZIP location; re-review required"
        )
    return source_key


def _find_legacy_source_version(s3, artifact_bucket, source_key, skill):
    key_marker = None
    version_marker = None
    while True:
        kwargs = {"Bucket": artifact_bucket, "Prefix": source_key}
        if key_marker:
            kwargs["KeyMarker"] = key_marker
        if version_marker:
            kwargs["VersionIdMarker"] = version_marker
        response = s3.list_object_versions(**kwargs)
        for version in response.get("Versions", []):
            version_id = version.get("VersionId")
            if (
                version.get("Key") != source_key
                or not version_id
                or version_id == "null"
            ):
                continue
            data, _ = _read_object(
                s3,
                artifact_bucket,
                source_key,
                version_id,
                max_bytes=MAX_SKILL_ARCHIVE_BYTES,
            )
            if _bytes_match(skill, data):
                members = _validated_archive_members(data, skill)
                _verify_review_source_directory(s3, artifact_bucket, skill, members)
                return data, version_id, members
        if not response.get("IsTruncated"):
            break
        key_marker = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if not key_marker:
            raise RuntimeError(
                "ListObjectVersions returned a truncated page without NextKeyMarker"
            )
    raise RuntimeError(
        f"{skill['name']}@{skill['version']} has no immutable source object version matching "
        f"reviewed SHA-256 {skill['sha256']}; re-review required"
    )


def _verified_source(s3, artifact_bucket, skill):
    source_key = _source_location(skill, artifact_bucket)
    source_version_id = skill.get("source_version_id")
    if not source_version_id:
        return _find_legacy_source_version(s3, artifact_bucket, source_key, skill)

    data, _ = _read_object(
        s3,
        artifact_bucket,
        source_key,
        source_version_id,
        max_bytes=MAX_SKILL_ARCHIVE_BYTES,
    )
    if not _bytes_match(skill, data):
        actual = hashlib.sha256(data).hexdigest()
        raise RuntimeError(
            f"{skill['name']}@{skill['version']} source version {source_version_id} SHA-256 mismatch "
            f"(reviewed {skill['sha256']}, observed {actual}); re-review required"
        )
    members = _validated_archive_members(data, skill)
    _verify_review_source_directory(s3, artifact_bucket, skill, members)
    return data, source_version_id, members


def _approved_skill(
    skill, artifact_bucket, zip_key, approved_version_id, source_version_id
):
    promoted = dict(skill)
    promoted.update(
        {
            "s3_uri": f"s3://{artifact_bucket}/{APPROVED_PREFIX}{skill['sha256']}/",
            "zip_s3_uri": f"s3://{artifact_bucket}/{zip_key}",
            "version_id": approved_version_id,
            "source_version_id": source_version_id,
            "zip_url": f"https://{artifact_bucket}.s3.{_region()}.amazonaws.com/{zip_key}",
        }
    )
    return promoted


def _create_or_verify_approved_object(
    s3, artifact_bucket, key, data, content_type, digest
):
    for attempt in range(3):
        try:
            response = s3.put_object(
                Bucket=artifact_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"archive-sha256": digest},
                IfNoneMatch="*",
            )
            version_id = response.get("VersionId")
            if not version_id or version_id == "null":
                raise RuntimeError(
                    f"promotion of s3://{artifact_bucket}/{key} did not return an immutable VersionId"
                )
            return version_id
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                existing, version_id = _read_object(
                    s3,
                    artifact_bucket,
                    key,
                    max_bytes=len(data),
                )
                if existing != data:
                    raise RuntimeError(
                        f"content-address collision at s3://{artifact_bucket}/{key}; refusing to overwrite"
                    ) from error
                if not version_id or version_id == "null":
                    raise RuntimeError(
                        f"approved artifact s3://{artifact_bucket}/{key} is not pinned to an immutable version"
                    )
                print(
                    f"INFO: verified existing s3://{artifact_bucket}/{key} version {version_id}"
                )
                return version_id
            if (code == "ConditionalRequestConflict" or status == 409) and attempt < 2:
                continue
            raise
    raise RuntimeError(f"promotion retries exhausted for s3://{artifact_bucket}/{key}")


def _approved_content_type(path):
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith((".yaml", ".yml")):
        return "application/yaml"
    if path.endswith(".py"):
        return "text/x-python; charset=utf-8"
    return "application/octet-stream"


def _list_directory_keys(s3, artifact_bucket, prefix):
    keys = set()
    token = None
    while True:
        kwargs = {"Bucket": artifact_bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = s3.list_objects_v2(**kwargs)
        keys.update(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not token:
            raise RuntimeError(
                "ListObjectsV2 returned a truncated page without NextContinuationToken"
            )


def _verify_review_source_directory(s3, artifact_bucket, skill, members):
    source_prefix = f"skills/{skill['name']}/{skill['version']}/"
    source_uri = f"s3://{artifact_bucket}/{source_prefix}"
    if skill["s3_uri"] != source_uri and skill.get("source_directory_s3_uri") is None:
        return

    expected = {f"{source_prefix}{path}": data for path, data in members}
    observed = _list_directory_keys(s3, artifact_bucket, source_prefix)
    if observed != set(expected):
        missing = sorted(set(expected) - observed)
        unexpected = sorted(observed - set(expected))
        raise RuntimeError(
            f"review-source directory {source_uri} does not exactly match the canonical ZIP "
            f"(missing={missing}, unexpected={unexpected}); re-review required"
        )

    for key in sorted(expected):
        data, _ = _read_object(
            s3,
            artifact_bucket,
            key,
            max_bytes=len(expected[key]),
        )
        if data != expected[key]:
            raise RuntimeError(
                f"review-source object s3://{artifact_bucket}/{key} differs from the canonical ZIP; "
                "re-review required"
            )


def promote_skill(s3, artifact_bucket, skill):
    """Verify reviewed bytes and create-or-verify ZIP and directory artifacts."""
    source_data, source_version_id, members = _verified_source(
        s3, artifact_bucket, skill
    )
    zip_key = f"{APPROVED_PREFIX}{skill['sha256']}.zip"
    zip_version_id = _create_or_verify_approved_object(
        s3,
        artifact_bucket,
        zip_key,
        source_data,
        "application/zip",
        skill["sha256"],
    )
    directory_prefix = f"{APPROVED_PREFIX}{skill['sha256']}/"
    expected_keys = set()
    for path, data in members:
        key = f"{directory_prefix}{path}"
        expected_keys.add(key)
        _create_or_verify_approved_object(
            s3,
            artifact_bucket,
            key,
            data,
            _approved_content_type(path),
            skill["sha256"],
        )
    observed_keys = _list_directory_keys(s3, artifact_bucket, directory_prefix)
    if observed_keys != expected_keys:
        missing = sorted(expected_keys - observed_keys)
        unexpected = sorted(observed_keys - expected_keys)
        raise RuntimeError(
            f"approved directory s3://{artifact_bucket}/{directory_prefix} does not exactly match "
            f"the reviewed archive (missing={missing}, unexpected={unexpected}); refusing distribution"
        )
    print(
        f"INFO: promoted {skill['name']}@{skill['version']} from source version "
        f"{source_version_id} to s3://{artifact_bucket}/{directory_prefix} and "
        f"s3://{artifact_bucket}/{zip_key} version {zip_version_id}"
    )
    return _approved_skill(
        skill,
        artifact_bucket,
        zip_key,
        zip_version_id,
        source_version_id,
    )


def _promoted_definition(record, promoted, artifact_bucket):
    definition = _record_definition(record)
    if not isinstance(definition, dict):
        raise RuntimeError(
            f"registry record {promoted.get('record_id')} has no updateable skill definition"
        )
    updated = json.loads(json.dumps(definition))
    meta = (updated.get("_meta") or {}).get(META_NAMESPACE)
    if not isinstance(meta, dict) or not isinstance(meta.get("artifact"), dict):
        raise RuntimeError(
            f"registry record {promoted.get('record_id')} has no updateable {META_NAMESPACE} artifact"
        )
    artifact = meta["artifact"]
    artifact.update(
        {
            "s3_uri": promoted["s3_uri"],
            "source_directory_s3_uri": (
                f"s3://{artifact_bucket}/skills/{promoted['name']}/{promoted['version']}/"
            ),
            "source_s3_uri": (
                f"s3://{artifact_bucket}/skills/{promoted['name']}/{promoted['version']}.zip"
            ),
            "source_version_id": promoted["source_version_id"],
            "zip_url": promoted["zip_url"],
        }
    )
    return None if updated == definition else updated


def _wait_for_record_status(
    registry,
    registry_id,
    record_id,
    expected,
    operation,
    timeout_seconds=60,
    poll_seconds=1,
):
    deadline = time.monotonic() + timeout_seconds
    while True:
        record = registry.get_record(registry_id, record_id)
        status = record.get("status") if isinstance(record, dict) else None
        if status in expected:
            return record
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DEPRECATED"}:
            raise RuntimeError(
                f"registry record {record_id} entered {status} while {operation}: "
                f"{record.get('statusReason', '')}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"registry record {record_id} did not reach {sorted(expected)} while "
                f"{operation} (last status: {status})"
            )
        time.sleep(poll_seconds)


def _approve_record(registry, s3, registry_id, artifact_bucket, record_id, reason):
    record = registry.get_record(registry_id, record_id)
    status = record.get("status") if isinstance(record, dict) else None
    if status not in {"DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED"}:
        raise RuntimeError(
            f"registry record {record_id} cannot be approved from status {status}"
        )
    skill = extract_skill(record)
    if not skill:
        raise RuntimeError(f"registry record {record_id} has invalid skill metadata")

    promoted = promote_skill(s3, artifact_bucket, skill)
    definition = _promoted_definition(record, promoted, artifact_bucket)
    if definition is not None:
        registry.update_skill_definition(registry_id, record_id, definition)
        record = _wait_for_record_status(
            registry,
            registry_id,
            record_id,
            {"DRAFT"},
            "publishing the immutable artifact URI",
        )
        status = record["status"]

    if status == "DRAFT":
        registry.submit_for_approval(registry_id, record_id)
        record = _wait_for_record_status(
            registry,
            registry_id,
            record_id,
            {"APPROVED", "PENDING_APPROVAL"},
            "resubmitting the promoted revision",
        )
        status = record["status"]
    if status in {"PENDING_APPROVAL", "REJECTED"}:
        registry.update_record_status(registry_id, record_id, "APPROVED", reason=reason)
    elif status != "APPROVED":
        raise RuntimeError(
            f"registry record {record_id} cannot be approved from status {status}"
        )
    return promoted


def _reject_record(registry, registry_id, record_id, reason):
    record = registry.get_record(registry_id, record_id)
    status = record.get("status") if isinstance(record, dict) else None
    if status == "REJECTED":
        return
    if status != "PENDING_APPROVAL":
        raise RuntimeError(
            f"registry record {record_id} cannot be rejected from status {status}"
        )
    registry.update_record_status(registry_id, record_id, "REJECTED", reason=reason)


def _decision_request(event):
    if not isinstance(event, dict) or "action" not in event:
        return None
    action = event.get("action")
    record_id = event.get("record_id")
    reason = event.get("reason", "")
    distribute = event.get("distribute", True)
    if action not in {"approve", "reject"}:
        raise ValueError(f"unsupported skills decision action: {action!r}")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("skills decision requires a non-empty record_id")
    if not isinstance(reason, str) or len(reason) > 255:
        raise ValueError(
            "skills decision reason must be a string of at most 255 characters"
        )
    if not isinstance(distribute, bool):
        raise ValueError("skills decision distribute field must be a boolean")
    return action, record_id, reason, distribute


def select_active_skills(skills):
    """Select one active record per name using the highest strict semver."""
    grouped = {}
    for skill in skills:
        grouped.setdefault(skill["name"], []).append(skill)
    selected = []
    for name in sorted(grouped):
        versions = grouped[name]
        by_version = {}
        for skill in versions:
            by_version.setdefault(skill["version"], []).append(skill)
        duplicates = {
            version: items for version, items in by_version.items() if len(items) > 1
        }
        if duplicates:
            details = [
                f"{name}@{version} ({[item.get('record_id') for item in items]})"
                for version, items in sorted(duplicates.items())
            ]
            raise RuntimeError(
                "Multiple APPROVED records exist for the same skill version: "
                f"{', '.join(details)}. Deprecate all but one record before distribution."
            )
        active = max(
            versions,
            key=lambda skill: tuple(int(part) for part in skill["version"].split(".")),
        )
        if len(versions) > 1:
            ignored = sorted(
                (skill["version"] for skill in versions if skill is not active),
                key=lambda version: tuple(int(part) for part in version.split(".")),
            )
            print(
                f"INFO: {ACTIVE_VERSION_RULE} selected {name}@{active['version']}; "
                f"older approved version(s) not distributed: {ignored}"
            )
        selected.append(active)
    return selected


def build_plugins_registry(skills):
    """CoWork bootstrap format — MUST stay {"plugins":[{"name","version","url"}]}."""
    return {
        "plugins": [
            {"name": s["name"], "version": s["version"], "url": s["zip_url"]}
            for s in skills
        ],
    }


def build_marketplace(skills):
    """Claude Code marketplace format, pinned to the vendored reference shape."""
    return {
        "$schema": MARKETPLACE_SCHEMA,
        "name": os.environ.get("MARKETPLACE_NAME", "gip-skills"),
        "version": "1.0.0",
        "description": "Governed skills published through the gip skills registry",
        "owner": {"name": os.environ.get("MARKETPLACE_OWNER", "gip skills registry")},
        "plugins": [
            {
                "name": s["name"],
                "version": s["version"],
                "source": s["zip_url"],
                "description": s["description"],
                "category": s["category"],
            }
            for s in skills
        ],
    }


def build_skills_lock(skills, generated_at=None):
    """Version-pinned feed for `gip skills sync`."""
    return {
        "schema_version": 2,
        "generated_at": generated_at
        or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "skills": [
            {
                "name": s["name"],
                "version": s["version"],
                "s3_uri": s["zip_s3_uri"],
                "version_id": s["version_id"],
                "sha256": s["sha256"],
            }
            for s in skills
        ],
    }


def _put_json(s3, bucket, key, document):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(document, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"INFO: wrote s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def _render_outputs(
    registry,
    s3,
    registry_id,
    artifact_bucket,
    plugins_bucket,
    plugins_key,
    marketplace_key,
    lock_key,
    prepromoted=None,
):
    records = registry.list_records(registry_id, status="APPROVED")
    skills = []
    skipped = []
    for record in records:
        skill = extract_skill(record)
        if skill:
            skills.append(skill)
        else:
            skipped.append(
                record.get("recordId")
                or record.get("recordArn")
                or record.get("name", "?")
            )
    if skipped:
        raise RuntimeError(f"Malformed approved skill record(s) skipped: {skipped}")
    if prepromoted:
        observed = {(skill["name"], skill["version"]) for skill in skills}
        missing = sorted(set(prepromoted) - observed)
        if missing:
            raise RuntimeError(
                f"newly approved skill record(s) not yet visible to distribution: {missing}"
            )
    approved_skills = sorted(
        ({"name": skill["name"], "version": skill["version"]} for skill in skills),
        key=lambda skill: (
            skill["name"],
            tuple(int(part) for part in skill["version"].split(".")),
        ),
    )
    active_skills = select_active_skills(skills)
    skills.sort(
        key=lambda skill: (
            skill["name"],
            tuple(int(part) for part in skill["version"].split(".")),
        )
    )
    print(
        f"INFO: promoting {len(skills)} approved skill version(s); distributing "
        f"{len(active_skills)} active version(s) using {ACTIVE_VERSION_RULE}"
    )

    promoted_by_version = dict(prepromoted or {})
    for skill in skills:
        key = (skill["name"], skill["version"])
        if key in promoted_by_version:
            if promoted_by_version[key]["sha256"] != skill["sha256"]:
                raise RuntimeError(
                    f"approved metadata changed while distributing {skill['name']}@{skill['version']}"
                )
            continue
        try:
            promoted_by_version[key] = promote_skill(s3, artifact_bucket, skill)
        except Exception as error:
            print(
                f"ERROR: promotion failed for {skill['name']}@{skill['version']}: {error}"
            )
            raise
    promoted = [
        promoted_by_version[(skill["name"], skill["version"])]
        for skill in active_skills
    ]

    written_outputs = []
    skipped_outputs = []
    written_outputs.append(
        _put_json(s3, artifact_bucket, marketplace_key, build_marketplace(promoted))
    )
    if plugins_bucket:
        written_outputs.append(
            _put_json(s3, plugins_bucket, plugins_key, build_plugins_registry(promoted))
        )
    else:
        skipped_outputs.append(plugins_key)
        print("INFO: PLUGINS_S3_BUCKET not set — skipping CoWork plugins-registry.json")
    # The lock is the consumer commit point and is written only after every
    # promotion and optional channel output has completed successfully.
    written_outputs.append(
        _put_json(s3, artifact_bucket, lock_key, build_skills_lock(promoted))
    )

    return {
        "skills": len(promoted),
        "active_version_rule": ACTIVE_VERSION_RULE,
        "approved_skills": approved_skills,
        "promoted_skills": approved_skills,
        "distributed_skills": [
            {"name": s["name"], "version": s["version"]} for s in promoted
        ],
        "written_outputs": written_outputs,
        "skipped_outputs": skipped_outputs,
    }


def handler(event, context):
    """Approve/reject one record or render outputs from all approved records."""
    import boto3

    registry_id = os.environ["REGISTRY_ID"]
    artifact_bucket = os.environ["ARTIFACT_BUCKET"]
    plugins_bucket = os.environ.get("PLUGINS_S3_BUCKET", "")
    plugins_key = os.environ.get("PLUGINS_S3_KEY", "plugins-registry.json")
    marketplace_key = os.environ.get(
        "MARKETPLACE_S3_KEY", "distribution/marketplace.json"
    )
    lock_key = os.environ.get("SKILLS_LOCK_S3_KEY", "distribution/skills-lock.json")
    registry = RegistryClient(
        client=boto3.client(
            SERVICE_NAME,
            region_name=_region(),
            config=AWS_CLIENT_CONFIG,
        )
    )
    decision = _decision_request(event)

    if decision:
        action, record_id, reason, distribute = decision
        if action == "reject":
            _reject_record(registry, registry_id, record_id, reason)
            return {"record_id": record_id, "status": "REJECTED"}

        s3 = boto3.client("s3", config=AWS_CLIENT_CONFIG)
        promoted = _approve_record(
            registry,
            s3,
            registry_id,
            artifact_bucket,
            record_id,
            reason,
        )
        if not distribute:
            identity = {"name": promoted["name"], "version": promoted["version"]}
            return {
                "record_id": record_id,
                "status": "APPROVED",
                "skills": 1,
                "active_version_rule": ACTIVE_VERSION_RULE,
                "approved_skills": [identity],
                "promoted_skills": [identity],
                "distributed_skills": [],
                "written_outputs": [],
                "skipped_outputs": ["distribution deferred by curator"],
            }
        result = _render_outputs(
            registry,
            s3,
            registry_id,
            artifact_bucket,
            plugins_bucket,
            plugins_key,
            marketplace_key,
            lock_key,
            prepromoted={(promoted["name"], promoted["version"]): promoted},
        )
        result.update({"record_id": record_id, "status": "APPROVED"})
        return result

    return _render_outputs(
        registry,
        boto3.client("s3", config=AWS_CLIENT_CONFIG),
        registry_id,
        artifact_bucket,
        plugins_bucket,
        plugins_key,
        marketplace_key,
        lock_key,
    )
