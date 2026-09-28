# ABOUTME: Tests for the skills distributor Lambda (registry -> per-harness outputs)
# ABOUTME: Pins legacy plugins-registry.json plus active marketplace and sync formats

"""Distributor Lambda tests.

The distributor renders APPROVED AgentSkills records into three documents:

1. plugins-registry.json — dormant compatibility output for legacy raw-CFN
   consumers. New Desktop deployments follow the mirrored upstream contract.
2. marketplace.json — Claude Code plugin marketplace shape, mirroring the
   vendored .claude-plugin/marketplace.json.
3. skills-lock.json — the `gip skills sync` feed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions" / "skills_registry"
)

MARKETPLACE_REFERENCE = Path(__file__).resolve().parents[2] / ".claude-plugin" / "marketplace.json"


def _skill_zip(name="code-review", version="1.2.0", extra_files=None):
    files = {
        "SKILL.md": (f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n").encode(),
        "_meta.json": json.dumps({"name": name, "version": version}).encode(),
        "scripts/check.py": b"print('ok')\n",
    }
    files.update(extra_files or {})
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _archive_members(archive):
    with zipfile.ZipFile(BytesIO(archive)) as source:
        return {info.filename: source.read(info) for info in source.infolist() if not info.is_dir()}


def _skill_zip_with_symlink():
    buffer = BytesIO(_skill_zip())
    with zipfile.ZipFile(buffer, "a") as archive:
        info = zipfile.ZipInfo("unsafe-link", date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "SKILL.md")
    return buffer.getvalue()


SOURCE_BYTES = _skill_zip()
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()


def _load(name: str):
    """Load a module from the Lambda dir with the dir on sys.path (so
    distributor.py can import registry_client)."""
    if str(LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location(f"skills_registry_{name}", LAMBDA_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"skills_registry_{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def distributor(monkeypatch):
    monkeypatch.setenv("ARTIFACT_BUCKET", "skills-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return _load("distributor")


def _record(
    name="code-review",
    version="1.2.0",
    status="APPROVED",
    meta_overrides=None,
    source_bytes=None,
    **record_overrides,
):
    source_bytes = source_bytes if source_bytes is not None else _skill_zip(name, version)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    meta = {
        "name": name,
        "version": version,
        "description": f"{name} skill",
        "category": "workflow",
        "model_compat": [{"model_family": "claude-sonnet-4-5", "status": "verified"}],
        "default_for_families": ["claude-sonnet-4-5"],
        "fork_of": None,
        "evals": {"suite_ref": None, "latest_score_ref": None, "gate": {"min_score": None}},
        "artifact": {
            "s3_uri": f"s3://skills-bucket/approved/sha256/{source_sha256}/",
            "source_directory_s3_uri": f"s3://skills-bucket/skills/{name}/{version}/",
            "source_s3_uri": f"s3://skills-bucket/skills/{name}/{version}.zip",
            "source_version_id": "source-v1",
            "zip_url": f"https://skills-bucket.s3.us-east-1.amazonaws.com/skills/{name}/{version}.zip",
            "sha256": source_sha256,
            "size_bytes": len(source_bytes),
            "signature": None,
        },
        "lifecycle": {"published_by": "idp:jorge@example.com", "published_at": "2026-07-08T00:00:00Z"},
    }
    if meta_overrides:
        meta.update(meta_overrides)
    record = {
        "recordId": f"rec-{name}-{version}",
        "name": name,
        "recordVersion": version,
        "status": status,
        "descriptors": {
            "agentSkills": {
                "skillMarkdown": f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}",
                "definition": {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": meta}},
            }
        },
    }
    record.update(record_overrides)
    return record


@pytest.fixture()
def legacy_approved_state():
    """Exact pre-A1 registry metadata and schema-v1 consumer lock shapes."""
    artifact = {
        "s3_uri": "s3://skills-bucket/skills/code-review/1.2.0/",
        "zip_url": "https://skills-bucket.s3.us-east-1.amazonaws.com/skills/code-review/1.2.0.zip",
        "sha256": SOURCE_SHA256,
        "size_bytes": len(SOURCE_BYTES),
        "signature": None,
    }
    meta = {
        "name": "code-review",
        "version": "1.2.0",
        "description": "code-review skill",
        "category": "workflow",
        "model_compat": [{"model_family": "claude-sonnet-4-5", "status": "verified"}],
        "default_for_families": ["claude-sonnet-4-5"],
        "fork_of": None,
        "evals": {"suite_ref": None, "latest_score_ref": None, "gate": {"min_score": None}},
        "artifact": artifact,
        "lifecycle": {"published_by": "idp:jorge@example.com", "published_at": "2026-07-08T00:00:00Z"},
    }
    record = {
        "recordId": "rec-code-review-1.2.0",
        "name": "code-review",
        "recordVersion": "1.2.0",
        "status": "APPROVED",
        "descriptors": {
            "agentSkills": {
                "skillMarkdown": "---\nname: code-review\ndescription: code-review skill\n---\n# code-review",
                "definition": {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": meta}},
            }
        },
    }
    lock = {
        "schema_version": 1,
        "generated_at": "2026-07-08T00:00:00Z",
        "skills": [
            {
                "name": "code-review",
                "version": "1.2.0",
                "s3_uri": "s3://skills-bucket/skills/code-review/1.2.0/",
                "sha256": SOURCE_SHA256,
            }
        ],
    }
    return {"record": record, "lock": lock, "source": SOURCE_BYTES}


# ---------------------------------------------------------------------------
# plugins-registry.json — the CoWork bootstrap format (REGRESSION-CRITICAL)
# ---------------------------------------------------------------------------


class TestPluginsRegistryFormat:
    def test_exact_bootstrap_consumed_shape(self, distributor):
        """Must be {"plugins":[{"name","version","url"}]} — nothing else."""
        skills = [distributor.extract_skill(_record())]
        doc = distributor.build_plugins_registry(skills)
        assert set(doc.keys()) == {"plugins"}
        assert len(doc["plugins"]) == 1
        entry = doc["plugins"][0]
        assert set(entry.keys()) == {"name", "version", "url"}
        assert entry["name"] == "code-review"
        assert entry["version"] == "1.2.0"
        assert entry["url"].startswith("https://")

    def test_empty_registry_serializes_to_empty_plugins_list(self, distributor):
        doc = distributor.build_plugins_registry([])
        assert doc == {"plugins": []}

    def test_url_falls_back_to_canonical_zip(self, distributor, monkeypatch):
        skill = distributor.extract_skill(
            _record(
                meta_overrides={
                    "artifact": {
                        "s3_uri": f"s3://skills-bucket/approved/sha256/{'b' * 64}/",
                        "sha256": "b" * 64,
                    }
                }
            )
        )
        assert skill["zip_url"] == "https://skills-bucket.s3.us-east-1.amazonaws.com/skills/code-review/1.2.0.zip"


# ---------------------------------------------------------------------------
# marketplace.json — Claude Code plugin marketplace format
# ---------------------------------------------------------------------------


class TestMarketplaceFormat:
    def test_shape_matches_vendored_reference(self, distributor):
        """Top-level keys and per-plugin keys mirror .claude-plugin/marketplace.json."""
        reference = json.loads(MARKETPLACE_REFERENCE.read_text(encoding="utf-8"))
        generated = distributor.build_marketplace([distributor.extract_skill(_record())])

        assert set(generated.keys()) == set(reference.keys())
        assert generated["$schema"] == reference["$schema"]
        assert set(generated["owner"].keys()) == set(reference["owner"].keys())
        ref_plugin_keys = set(reference["plugins"][0].keys())
        assert set(generated["plugins"][0].keys()) == ref_plugin_keys

    def test_plugin_entry_content(self, distributor):
        generated = distributor.build_marketplace([distributor.extract_skill(_record())])
        entry = generated["plugins"][0]
        assert entry["name"] == "code-review"
        assert entry["version"] == "1.2.0"
        assert entry["category"] == "workflow"
        assert entry["description"] == "code-review skill"


# ---------------------------------------------------------------------------
# skills-lock.json — the sync feed
# ---------------------------------------------------------------------------


class TestSkillsLock:
    def test_lock_entries_pin_uri_and_hash(self, distributor):
        skill = distributor.extract_skill(_record())
        skill.update(
            {
                "s3_uri": f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}/",
                "zip_s3_uri": f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}.zip",
                "version_id": "approved-v1",
            }
        )
        skills = [skill]
        lock = distributor.build_skills_lock(skills, generated_at="2026-07-08T00:00:00Z")
        assert lock["schema_version"] == 2
        assert lock["generated_at"] == "2026-07-08T00:00:00Z"
        assert lock["skills"] == [
            {
                "name": "code-review",
                "version": "1.2.0",
                "s3_uri": f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}.zip",
                "version_id": "approved-v1",
                "sha256": SOURCE_SHA256,
            }
        ]


# ---------------------------------------------------------------------------
# Record extraction robustness
# ---------------------------------------------------------------------------


class TestExtractSkill:
    def test_record_without_meta_is_skipped_not_fatal(self, distributor):
        record = _record()
        record["descriptors"]["agentSkills"]["definition"] = {}
        assert distributor.extract_skill(record) is None

    def test_record_missing_artifact_is_skipped(self, distributor):
        record = _record(meta_overrides={"artifact": {}})
        assert distributor.extract_skill(record) is None

    def test_record_with_invalid_digest_is_skipped(self, distributor):
        artifact = dict(_record()["descriptors"]["agentSkills"]["definition"]["_meta"]["io.gip.skill/v1"]["artifact"])
        artifact["sha256"] = "not-a-digest"
        assert distributor.extract_skill(_record(meta_overrides={"artifact": artifact})) is None

    def test_record_with_noncanonical_semver_is_skipped(self, distributor):
        assert distributor.extract_skill(_record(version="01.2.0")) is None

    def test_meta_namespace_is_pinned(self, distributor):
        """The frozen namespace string — bumping it is a schema version change."""
        assert distributor.META_NAMESPACE == "io.gip.skill/v1"

    def test_accepts_agent_registry_inline_content_shape(self, distributor):
        record = _record()
        definition = record["descriptors"]["agentSkills"].pop("definition")
        record["descriptors"]["agentSkills"]["skillDefinition"] = {
            "schemaVersion": "0.1.0",
            "inlineContent": json.dumps(definition),
        }

        assert distributor.extract_skill(record)["name"] == "code-review"

    def test_record_exposes_digest_addressed_directory_for_agentcore(self, distributor):
        skill = distributor.extract_skill(_record())

        assert skill["s3_uri"] == f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}/"
        assert skill["source_directory_s3_uri"] == "s3://skills-bucket/skills/code-review/1.2.0/"

    def test_invalid_inline_content_is_skipped_with_warning(self, distributor, capsys):
        record = _record()
        record["descriptors"]["agentSkills"].pop("definition")
        record["descriptors"]["agentSkills"]["skillDefinition"] = {"inlineContent": "{"}

        assert distributor.extract_skill(record) is None
        assert "invalid skillDefinition JSON" in capsys.readouterr().out

    def test_non_object_inline_content_is_skipped(self, distributor):
        record = _record()
        record["descriptors"]["agentSkills"].pop("definition")
        record["descriptors"]["agentSkills"]["skillDefinition"] = {"inlineContent": "[]"}

        assert distributor.extract_skill(record) is None

    def test_accepts_ga_agent_skills_definition_shape(self, distributor):
        # GA descriptor shape (registry-faq "Change 3", retrieved 2026-07-29)
        record = _record()
        definition = record["descriptors"].pop("agentSkills")["definition"]
        record["recordType"] = "SKILL"
        record["descriptors"]["agentSkillsDefinition"] = {
            "dataSchemaVersion": "0.1.0",
            "data": json.dumps(definition),
            "additionalData": {"skillMd": {"data": "# skill"}},
        }

        assert distributor.extract_skill(record)["name"] == "code-review"

    def test_invalid_ga_data_json_is_skipped_with_warning(self, distributor, capsys):
        record = _record()
        record["descriptors"].pop("agentSkills")
        record["descriptors"]["agentSkillsDefinition"] = {"data": "{"}

        assert distributor.extract_skill(record) is None
        assert "invalid skillDefinition JSON" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Registry mutation shape
# ---------------------------------------------------------------------------


def test_registry_client_patches_only_agent_skill_definition():
    registry_client = _load("registry_client")
    boto = MagicMock()
    client = registry_client.RegistryClient(client=boto, surface=registry_client.PREVIEW_SURFACE)
    definition = {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}}

    client.update_skill_definition("reg-123", "rec-123", definition)

    kwargs = boto.update_registry_record.call_args.kwargs
    assert set(kwargs) == {"registryId", "recordId", "descriptors"}
    skill_definition = kwargs["descriptors"]["optionalValue"]["agentSkills"]["optionalValue"]["skillDefinition"]
    assert skill_definition["optionalValue"]["schemaVersion"] == "0.1.0"
    assert json.loads(skill_definition["optionalValue"]["inlineContent"]) == definition


def test_registry_client_ga_patches_only_agent_skill_definition():
    """GA UpdateRegistryRecord shape (agent-registry-control/2025-12-01
    botocore model, verified 2026-08-12 — ADR-0029 day-one step 3)."""
    registry_client = _load("registry_client")
    boto = MagicMock()
    client = registry_client.RegistryClient(client=boto, surface=registry_client.GA_SURFACE)
    definition = {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}}

    client.update_skill_definition("reg-123", "rec-123", definition)

    kwargs = boto.update_registry_record.call_args.kwargs
    assert set(kwargs) == {"registryId", "recordId", "descriptors"}
    fields = kwargs["descriptors"]["optionalValue"]["agentSkillsDefinition"]["optionalValue"]
    assert fields["dataSchemaVersion"] == {"optionalValue": "0.1.0"}
    assert json.loads(fields["data"]["optionalValue"]) == definition
    assert "additionalData" not in fields


# ---------------------------------------------------------------------------
# Handler end-to-end (mocked registry + S3)
# ---------------------------------------------------------------------------


class TestHandler:
    def _run(
        self,
        distributor,
        monkeypatch,
        records,
        plugins_bucket="",
        mock_s3=None,
        get_object=None,
        put_object=None,
        list_objects_v2=None,
        event=None,
        get_records=None,
        mock_registry=None,
    ):
        monkeypatch.setenv("REGISTRY_ID", "reg-123")
        monkeypatch.setenv("ARTIFACT_BUCKET", "skills-bucket")
        monkeypatch.setenv("PLUGINS_S3_BUCKET", plugins_bucket)

        mock_registry = mock_registry or MagicMock()
        mock_registry.list_records.return_value = records
        if get_records is not None:
            mock_registry.get_record.side_effect = get_records
        monkeypatch.setattr(distributor, "RegistryClient", lambda *a, **k: mock_registry)

        mock_s3 = mock_s3 or MagicMock()

        source_by_key = {}
        directory_keys_by_prefix = {}
        for record in records:
            definition = record.get("descriptors", {}).get("agentSkills", {}).get("definition", {})
            meta = (definition.get("_meta") or {}).get("io.gip.skill/v1") or {}
            name = meta.get("name")
            version = meta.get("version")
            artifact = meta.get("artifact") or {}
            if not (name and version):
                continue
            source_uri = artifact.get("source_s3_uri") or f"s3://skills-bucket/skills/{name}/{version}.zip"
            source_key = source_uri.removeprefix("s3://skills-bucket/")
            archive = _skill_zip(name, version)
            source_by_key[source_key] = archive
            source_prefix = f"skills/{name}/{version}/"
            members = _archive_members(archive)
            source_by_key.update({f"{source_prefix}{path}": data for path, data in members.items()})
            directory_keys_by_prefix[source_prefix] = {f"{source_prefix}{path}" for path in members}
            digest = artifact.get("sha256")
            if digest:
                prefix = f"approved/sha256/{digest}/"
                directory_keys_by_prefix[prefix] = {f"{prefix}{path}" for path in members}

        def default_get_object(Bucket, Key, VersionId=None):
            assert Bucket == "skills-bucket"
            assert Key in source_by_key
            if Key.endswith(".zip"):
                assert VersionId == "source-v1"
            else:
                assert VersionId is None
            body = MagicMock()
            body.read.return_value = source_by_key[Key]
            return {
                "Body": body,
                "ContentLength": len(source_by_key[Key]),
                "VersionId": VersionId,
            }

        def default_put_object(**kwargs):
            if kwargs["Key"].startswith("approved/sha256/"):
                return {"VersionId": "approved-v1"}
            return {}

        def default_list_objects_v2(Bucket, Prefix, ContinuationToken=None):
            assert Bucket == "skills-bucket"
            assert ContinuationToken is None
            return {
                "IsTruncated": False,
                "Contents": [{"Key": key} for key in sorted(directory_keys_by_prefix.get(Prefix, set()))],
            }

        mock_s3.get_object.side_effect = get_object or default_get_object
        mock_s3.put_object.side_effect = put_object or default_put_object
        mock_s3.list_objects_v2.side_effect = list_objects_v2 or default_list_objects_v2
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

        result = distributor.handler(event or {}, None)
        return result, mock_registry, mock_s3

    def _written(self, mock_s3):
        writes = {}
        for call in mock_s3.put_object.call_args_list:
            kwargs = call.kwargs
            if kwargs["Key"].startswith("approved/sha256/"):
                continue
            writes[(kwargs["Bucket"], kwargs["Key"])] = json.loads(kwargs["Body"])
        return writes

    def test_writes_all_three_outputs_with_plugins_bucket(self, distributor, monkeypatch):
        result, mock_registry, mock_s3 = self._run(distributor, monkeypatch, [_record()], plugins_bucket="plugins-b")
        assert result["skills"] == 1
        assert {"name": "code-review", "version": "1.2.0"} in result["distributed_skills"]
        assert "s3://plugins-b/plugins-registry.json" in result["written_outputs"]
        mock_registry.list_records.assert_called_once_with("reg-123", status="APPROVED")
        writes = self._written(mock_s3)
        assert ("plugins-b", "plugins-registry.json") in writes
        assert ("skills-bucket", "distribution/marketplace.json") in writes
        assert ("skills-bucket", "distribution/skills-lock.json") in writes
        plugins_doc = writes[("plugins-b", "plugins-registry.json")]
        assert set(plugins_doc.keys()) == {"plugins"}
        assert set(plugins_doc["plugins"][0].keys()) == {"name", "version", "url"}
        assert plugins_doc["plugins"][0]["url"].endswith(f"approved/sha256/{SOURCE_SHA256}.zip")
        lock = writes[("skills-bucket", "distribution/skills-lock.json")]
        assert lock["schema_version"] == 2
        assert lock["skills"][0]["s3_uri"] == f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}.zip"
        assert lock["skills"][0]["version_id"] == "approved-v1"

        promotion = next(
            call.kwargs for call in mock_s3.put_object.call_args_list if call.kwargs["Key"].startswith("approved/")
        )
        assert promotion["IfNoneMatch"] == "*"
        assert promotion["Body"] == SOURCE_BYTES
        approved_writes = {
            call.kwargs["Key"]: call.kwargs
            for call in mock_s3.put_object.call_args_list
            if call.kwargs["Key"].startswith("approved/sha256/")
        }
        expected_directory_keys = {f"approved/sha256/{SOURCE_SHA256}/{path}" for path in _archive_members(SOURCE_BYTES)}
        assert expected_directory_keys < set(approved_writes)
        assert all(approved_writes[key]["IfNoneMatch"] == "*" for key in expected_directory_keys)
        assert mock_s3.put_object.call_args_list[-1].kwargs["Key"] == "distribution/skills-lock.json"

    def test_handler_uses_bounded_timeouts_for_registry_and_s3_clients(self, distributor, monkeypatch):
        self._run(distributor, monkeypatch, [_record()])

        client_calls = sys.modules["boto3"].client.call_args_list
        assert [call.args[0] for call in client_calls] == [distributor.SERVICE_NAME, "s3"]
        assert all(call.kwargs["config"] is distributor.AWS_CLIENT_CONFIG for call in client_calls)
        assert distributor.AWS_CLIENT_CONFIG.connect_timeout < 300
        assert distributor.AWS_CLIENT_CONFIG.read_timeout < 300

    def test_skips_plugins_output_without_bucket(self, distributor, monkeypatch):
        result, _, mock_s3 = self._run(distributor, monkeypatch, [_record()], plugins_bucket="")
        assert result["skipped_outputs"] == ["plugins-registry.json"]
        writes = self._written(mock_s3)
        assert all(key != "plugins-registry.json" for (_, key) in writes)
        assert ("skills-bucket", "distribution/marketplace.json") in writes

    def test_malformed_approved_record_fails_distribution(self, distributor, monkeypatch):
        bad = _record(name="broken")
        bad["descriptors"]["agentSkills"]["definition"] = {"_meta": {}}
        try:
            self._run(distributor, monkeypatch, [bad, _record()], plugins_bucket="plugins-b")
        except RuntimeError as e:
            assert "Malformed approved skill" in str(e)
        else:
            raise AssertionError("expected RuntimeError")

    def test_highest_semver_is_the_only_active_version_per_name(self, distributor, monkeypatch):
        older = _record(version="1.9.0")
        active = _record(version="1.10.0")

        result, _, mock_s3 = self._run(distributor, monkeypatch, [active, older])

        assert result["active_version_rule"] == "highest-semver"
        assert result["approved_skills"] == [
            {"name": "code-review", "version": "1.9.0"},
            {"name": "code-review", "version": "1.10.0"},
        ]
        assert result["promoted_skills"] == result["approved_skills"]
        assert result["distributed_skills"] == [{"name": "code-review", "version": "1.10.0"}]
        source_reads = [
            call.kwargs["Key"] for call in mock_s3.get_object.call_args_list if call.kwargs["Key"].endswith(".zip")
        ]
        assert source_reads == [
            "skills/code-review/1.9.0.zip",
            "skills/code-review/1.10.0.zip",
        ]
        lock = self._written(mock_s3)[("skills-bucket", "distribution/skills-lock.json")]
        assert [(skill["name"], skill["version"]) for skill in lock["skills"]] == [("code-review", "1.10.0")]

    def test_duplicate_approved_record_version_requires_deprecation(self, distributor, monkeypatch):
        duplicate = _record(recordId="rec-duplicate")
        mock_s3 = MagicMock()

        with pytest.raises(RuntimeError, match="Deprecate all but one record"):
            self._run(distributor, monkeypatch, [_record(), duplicate], mock_s3=mock_s3)

        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()

    def test_digest_mismatch_writes_nothing_and_requires_review(self, distributor, monkeypatch):
        mock_s3 = MagicMock()

        def get_object(**kwargs):
            body = MagicMock()
            body.read.return_value = b"publisher changed the source"
            return {"Body": body, "VersionId": kwargs["VersionId"]}

        with pytest.raises(RuntimeError, match="SHA-256 mismatch.*re-review required"):
            self._run(distributor, monkeypatch, [_record()], mock_s3=mock_s3, get_object=get_object)

        mock_s3.put_object.assert_not_called()

    def test_review_source_bytes_must_match_pinned_canonical_zip(self, distributor, monkeypatch):
        source_prefix = "skills/code-review/1.2.0/"
        source_members = _archive_members(SOURCE_BYTES)
        mock_s3 = MagicMock()

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            if Key == "skills/code-review/1.2.0.zip":
                body.read.return_value = SOURCE_BYTES
            else:
                path = Key.removeprefix(source_prefix)
                body.read.return_value = b"print('no')\n" if path == "scripts/check.py" else source_members[path]
            return {"Body": body, "VersionId": VersionId}

        with pytest.raises(RuntimeError, match="differs from the canonical ZIP.*re-review required"):
            self._run(
                distributor,
                monkeypatch,
                [_record()],
                mock_s3=mock_s3,
                get_object=get_object,
            )

        mock_s3.put_object.assert_not_called()

    @pytest.mark.parametrize("difference", ["missing", "unexpected"])
    def test_review_source_paths_must_exactly_match_pinned_canonical_zip(
        self,
        distributor,
        monkeypatch,
        difference,
    ):
        source_prefix = "skills/code-review/1.2.0/"
        source_keys = {f"{source_prefix}{path}" for path in _archive_members(SOURCE_BYTES)}
        if difference == "missing":
            source_keys.remove(f"{source_prefix}scripts/check.py")
        else:
            source_keys.add(f"{source_prefix}unreviewed.py")

        def list_objects_v2(Bucket, Prefix, ContinuationToken=None):
            assert Prefix == source_prefix
            return {
                "IsTruncated": False,
                "Contents": [{"Key": key} for key in sorted(source_keys)],
            }

        mock_s3 = MagicMock()
        with pytest.raises(RuntimeError, match="does not exactly match the canonical ZIP.*re-review required"):
            self._run(
                distributor,
                monkeypatch,
                [_record()],
                mock_s3=mock_s3,
                list_objects_v2=list_objects_v2,
            )

        mock_s3.put_object.assert_not_called()

    @pytest.mark.parametrize(
        ("archive", "message"),
        [
            (b"not a ZIP", "not a valid ZIP archive"),
            (_skill_zip(extra_files={"../escape.py": b"bad"}), "unsafe path in skill archive"),
            (_skill_zip_with_symlink(), "unsupported file type"),
            (_skill_zip(extra_files={"payload.txt": b"A" * 200_000}), "compression ratio"),
        ],
    )
    def test_invalid_archive_is_rejected_before_promotion(
        self,
        distributor,
        monkeypatch,
        archive,
        message,
    ):
        record = _record(source_bytes=archive)
        mock_s3 = MagicMock()

        def get_object(**kwargs):
            body = MagicMock()
            body.read.return_value = archive
            return {"Body": body, "VersionId": kwargs["VersionId"]}

        with pytest.raises(RuntimeError, match=message):
            self._run(
                distributor,
                monkeypatch,
                [record],
                mock_s3=mock_s3,
                get_object=get_object,
            )

        mock_s3.put_object.assert_not_called()

    def test_archive_member_limit_is_enforced(self, distributor, monkeypatch):
        archive = _skill_zip(extra_files={"extra.txt": b"extra"})
        record = _record(source_bytes=archive)
        mock_s3 = MagicMock()

        def get_object(**kwargs):
            body = MagicMock()
            body.read.return_value = archive
            return {"Body": body, "VersionId": kwargs["VersionId"]}

        monkeypatch.setattr(distributor, "MAX_SKILL_ARCHIVE_MEMBERS", 3)
        with pytest.raises(RuntimeError, match="maximum allowed is 3"):
            self._run(
                distributor,
                monkeypatch,
                [record],
                mock_s3=mock_s3,
                get_object=get_object,
            )
        mock_s3.put_object.assert_not_called()

    def test_archive_expanded_size_limit_is_enforced(self, distributor, monkeypatch):
        archive = _skill_zip(extra_files={"extra.txt": b"expanded"})
        record = _record(source_bytes=archive)
        mock_s3 = MagicMock()

        def get_object(**kwargs):
            body = MagicMock()
            body.read.return_value = archive
            return {"Body": body, "VersionId": kwargs["VersionId"]}

        monkeypatch.setattr(distributor, "MAX_SKILL_EXPANDED_BYTES", 10)
        with pytest.raises(RuntimeError, match="archive expands beyond 10 bytes"):
            self._run(
                distributor,
                monkeypatch,
                [record],
                mock_s3=mock_s3,
                get_object=get_object,
            )
        mock_s3.put_object.assert_not_called()

    def test_existing_approved_object_is_verified_not_overwritten(self, distributor, monkeypatch):
        approved_key = f"approved/sha256/{SOURCE_SHA256}.zip"
        source_prefix = "skills/code-review/1.2.0/"
        source_members = _archive_members(SOURCE_BYTES)

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            if Key == approved_key:
                assert VersionId is None
                body.read.return_value = SOURCE_BYTES
                return {"Body": body, "VersionId": "approved-existing-v1"}
            if Key == "skills/code-review/1.2.0.zip":
                body.read.return_value = SOURCE_BYTES
            else:
                body.read.return_value = source_members[Key.removeprefix(source_prefix)]
            return {"Body": body, "VersionId": VersionId}

        def put_object(**kwargs):
            if kwargs["Key"] == approved_key:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            if kwargs["Key"].startswith("approved/sha256/"):
                return {"VersionId": "approved-new-v1"}
            return {}

        result, _, mock_s3 = self._run(
            distributor,
            monkeypatch,
            [_record()],
            get_object=get_object,
            put_object=put_object,
        )

        assert result["skills"] == 1
        approved_attempts = [c.kwargs for c in mock_s3.put_object.call_args_list if c.kwargs["Key"] == approved_key]
        assert len(approved_attempts) == 1
        assert approved_attempts[0]["IfNoneMatch"] == "*"
        lock = self._written(mock_s3)[("skills-bucket", "distribution/skills-lock.json")]
        assert lock["skills"][0]["version_id"] == "approved-existing-v1"

    def test_existing_approved_directory_is_verified_idempotently(self, distributor, monkeypatch):
        approved_zip_key = f"approved/sha256/{SOURCE_SHA256}.zip"
        directory_prefix = f"approved/sha256/{SOURCE_SHA256}/"
        source_prefix = "skills/code-review/1.2.0/"
        source_members = _archive_members(SOURCE_BYTES)
        existing = {
            approved_zip_key: SOURCE_BYTES,
            **{f"{directory_prefix}{path}": data for path, data in source_members.items()},
        }

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            if Key == "skills/code-review/1.2.0.zip":
                body.read.return_value = SOURCE_BYTES
                return {"Body": body, "VersionId": VersionId}
            if Key.startswith(source_prefix):
                body.read.return_value = source_members[Key.removeprefix(source_prefix)]
                return {"Body": body}
            body.read.return_value = existing[Key]
            return {"Body": body, "VersionId": f"existing-{Path(Key).name}"}

        def put_object(**kwargs):
            if kwargs["Key"].startswith("approved/sha256/"):
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            return {}

        result, _, mock_s3 = self._run(
            distributor,
            monkeypatch,
            [_record()],
            get_object=get_object,
            put_object=put_object,
        )

        assert result["skills"] == 1
        approved_attempts = [
            call.kwargs
            for call in mock_s3.put_object.call_args_list
            if call.kwargs["Key"].startswith("approved/sha256/")
        ]
        assert {attempt["Key"] for attempt in approved_attempts} == set(existing)
        assert all(attempt["IfNoneMatch"] == "*" for attempt in approved_attempts)

    def test_existing_approved_collision_fails_without_replacing_outputs(self, distributor, monkeypatch):
        approved_key = f"approved/sha256/{SOURCE_SHA256}.zip"
        source_prefix = "skills/code-review/1.2.0/"
        source_members = _archive_members(SOURCE_BYTES)

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            if Key == approved_key:
                body.read.return_value = b"different bytes"
            elif Key == "skills/code-review/1.2.0.zip":
                body.read.return_value = SOURCE_BYTES
            else:
                body.read.return_value = source_members[Key.removeprefix(source_prefix)]
            return {"Body": body, "VersionId": VersionId or "approved-existing-v1"}

        def put_object(**kwargs):
            if kwargs["Key"] == approved_key:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            return {}

        mock_s3 = MagicMock()
        with pytest.raises(RuntimeError, match="content-address collision.*refusing to overwrite"):
            self._run(
                distributor,
                monkeypatch,
                [_record()],
                mock_s3=mock_s3,
                get_object=get_object,
                put_object=put_object,
            )

        written_keys = [call.kwargs["Key"] for call in mock_s3.put_object.call_args_list]
        assert written_keys == [approved_key]

    def test_legacy_approval_migrates_only_matching_immutable_version(
        self, distributor, monkeypatch, legacy_approved_state
    ):
        source_prefix = "skills/code-review/1.2.0/"
        source_members = _archive_members(SOURCE_BYTES)

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            if VersionId is not None:
                body.read.return_value = SOURCE_BYTES if VersionId == "legacy-good" else b"wrong"
            else:
                body.read.return_value = source_members[Key.removeprefix(source_prefix)]
            return {"Body": body, "VersionId": VersionId}

        mock_s3 = MagicMock()
        mock_s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [
                {"Key": "skills/code-review/1.2.0.zip", "VersionId": "legacy-bad"},
                {"Key": "skills/code-review/1.2.0.zip", "VersionId": "legacy-good"},
                {"Key": "skills/code-review/1.2.0.zip", "VersionId": "null"},
            ],
        }

        result, _, mock_s3 = self._run(
            distributor,
            monkeypatch,
            [legacy_approved_state["record"]],
            mock_s3=mock_s3,
            get_object=get_object,
        )

        assert result["skills"] == 1
        mock_s3.list_object_versions.assert_called_once_with(
            Bucket="skills-bucket", Prefix="skills/code-review/1.2.0.zip"
        )
        assert legacy_approved_state["lock"] == {
            "schema_version": 1,
            "generated_at": "2026-07-08T00:00:00Z",
            "skills": [
                {
                    "name": "code-review",
                    "version": "1.2.0",
                    "s3_uri": "s3://skills-bucket/skills/code-review/1.2.0/",
                    "sha256": SOURCE_SHA256,
                }
            ],
        }
        source_reads = [
            c.kwargs["VersionId"] for c in mock_s3.get_object.call_args_list if c.kwargs.get("VersionId") is not None
        ]
        assert source_reads == ["legacy-bad", "legacy-good"]
        migrated_lock = self._written(mock_s3)[("skills-bucket", "distribution/skills-lock.json")]
        assert migrated_lock["schema_version"] == 2
        assert migrated_lock["skills"][0]["s3_uri"] == f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}.zip"

    def test_legacy_approval_without_matching_version_preserves_outputs(
        self, distributor, monkeypatch, legacy_approved_state
    ):
        mock_s3 = MagicMock()
        mock_s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [{"Key": "skills/code-review/1.2.0.zip", "VersionId": "legacy-bad"}],
        }

        def get_object(Bucket, Key, VersionId=None):
            body = MagicMock()
            body.read.return_value = b"wrong"
            return {"Body": body, "VersionId": VersionId}

        with pytest.raises(RuntimeError, match="no immutable source object version matching.*re-review required"):
            self._run(
                distributor,
                monkeypatch,
                [legacy_approved_state["record"]],
                mock_s3=mock_s3,
                get_object=get_object,
            )

        mock_s3.put_object.assert_not_called()

    def test_unexpected_artifact_location_requires_review(self, distributor, monkeypatch):
        artifact = dict(_record()["descriptors"]["agentSkills"]["definition"]["_meta"]["io.gip.skill/v1"]["artifact"])
        artifact["s3_uri"] = "s3://skills-bucket/skills/other/1.2.0/"
        record = _record(meta_overrides={"artifact": artifact})
        mock_s3 = MagicMock()

        with pytest.raises(RuntimeError, match="unexpected artifact location.*re-review required"):
            self._run(distributor, monkeypatch, [record], mock_s3=mock_s3)

        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()

    def test_no_distribute_approval_promotes_and_publishes_before_status(self, distributor, monkeypatch):
        artifact = dict(_record()["descriptors"]["agentSkills"]["definition"]["_meta"]["io.gip.skill/v1"]["artifact"])
        artifact["s3_uri"] = "s3://skills-bucket/skills/code-review/1.2.0/"
        artifact["zip_url"] = "https://skills-bucket.s3.us-east-1.amazonaws.com/skills/code-review/1.2.0.zip"
        record = _record(status="PENDING_APPROVAL", meta_overrides={"artifact": artifact})
        draft = {**record, "status": "DRAFT"}
        pending = {**record, "status": "PENDING_APPROVAL"}
        registry = MagicMock()
        mock_s3 = MagicMock()

        def approve_after_publication(*args, **kwargs):
            assert registry.update_skill_definition.called
            assert mock_s3.put_object.call_count > 0

        registry.update_record_status.side_effect = approve_after_publication

        result, registry, mock_s3 = self._run(
            distributor,
            monkeypatch,
            [record],
            mock_registry=registry,
            mock_s3=mock_s3,
            event={
                "action": "approve",
                "record_id": record["recordId"],
                "reason": "reviewed",
                "distribute": False,
            },
            get_records=[record, draft, pending],
        )

        assert result["status"] == "APPROVED"
        assert result["written_outputs"] == []
        assert all(call.kwargs["Key"].startswith("approved/sha256/") for call in mock_s3.put_object.call_args_list)
        definition = registry.update_skill_definition.call_args.args[2]
        promoted_artifact = definition["_meta"]["io.gip.skill/v1"]["artifact"]
        assert promoted_artifact["s3_uri"] == f"s3://skills-bucket/approved/sha256/{SOURCE_SHA256}/"
        assert promoted_artifact["source_version_id"] == "source-v1"
        assert promoted_artifact["zip_url"].endswith(f"approved/sha256/{SOURCE_SHA256}.zip")
        workflow_calls = [
            call[0]
            for call in registry.method_calls
            if call[0]
            in {
                "get_record",
                "update_skill_definition",
                "submit_for_approval",
                "update_record_status",
            }
        ]
        assert workflow_calls == [
            "get_record",
            "update_skill_definition",
            "get_record",
            "submit_for_approval",
            "get_record",
            "update_record_status",
        ]

    def test_approval_promotion_failure_leaves_record_pending(self, distributor, monkeypatch):
        artifact = dict(_record()["descriptors"]["agentSkills"]["definition"]["_meta"]["io.gip.skill/v1"]["artifact"])
        artifact["s3_uri"] = "s3://skills-bucket/skills/code-review/1.2.0/"
        record = _record(status="PENDING_APPROVAL", meta_overrides={"artifact": artifact})
        mock_registry = MagicMock()
        mock_registry.get_record.return_value = record
        mock_s3 = MagicMock()

        def get_object(**kwargs):
            body = MagicMock()
            body.read.return_value = b"not the reviewed bytes"
            return {"Body": body, "VersionId": kwargs["VersionId"]}

        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            self._run(
                distributor,
                monkeypatch,
                [record],
                mock_s3=mock_s3,
                mock_registry=mock_registry,
                get_object=get_object,
                event={
                    "action": "approve",
                    "record_id": record["recordId"],
                    "reason": "reviewed",
                },
            )

        mock_registry.update_skill_definition.assert_not_called()
        mock_registry.submit_for_approval.assert_not_called()
        mock_registry.update_record_status.assert_not_called()
        mock_s3.put_object.assert_not_called()

    def test_rejection_is_mediated_without_s3_writes(self, distributor, monkeypatch):
        record = _record(status="PENDING_APPROVAL")

        result, registry, mock_s3 = self._run(
            distributor,
            monkeypatch,
            [record],
            event={
                "action": "reject",
                "record_id": record["recordId"],
                "reason": "not ready",
            },
            get_records=[record],
        )

        assert result == {"record_id": record["recordId"], "status": "REJECTED"}
        registry.update_record_status.assert_called_once_with(
            "reg-123", record["recordId"], "REJECTED", reason="not ready"
        )
        mock_s3.put_object.assert_not_called()

    def test_partial_output_failure_does_not_replace_consumer_lock(self, distributor, monkeypatch):
        def put_object(**kwargs):
            if kwargs["Key"].startswith("approved/sha256/"):
                return {"VersionId": "approved-v1"}
            if kwargs["Key"] == "distribution/marketplace.json":
                raise RuntimeError("simulated output failure")
            return {}

        mock_s3 = MagicMock()
        with pytest.raises(RuntimeError, match="simulated output failure"):
            self._run(
                distributor,
                monkeypatch,
                [_record()],
                mock_s3=mock_s3,
                put_object=put_object,
            )

        written_keys = [call.kwargs["Key"] for call in mock_s3.put_object.call_args_list]
        assert "distribution/skills-lock.json" not in written_keys
