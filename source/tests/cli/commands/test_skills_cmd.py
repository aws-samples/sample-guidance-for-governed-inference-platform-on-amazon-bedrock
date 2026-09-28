# ABOUTME: Tests for the `gip skills` command group (publish/list/approve/sync)
# ABOUTME: Covers _meta validation incl. rejection cases, artifact upload, curator flow, and deploy dispatch

"""Tests for the skills registry CLI (lane E-S1).

All AWS interaction is mocked: S3/STS/Lambda via boto3.client patching, the
Agent Registry via a mocked AgentRegistryClient (the single isolation module
for the bedrock-agentcore -> agent-registry namespace migration).
"""

import hashlib
import json
import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from cleo.testers.command_tester import CommandTester

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands import skills_cmd as skills_cmd_module
from governed_inference_platform.cli.commands.deploy import VALID_STACKS, DeployCommand, build_skills_params
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.cli.commands.init_answers import check_structure, validate_config
from governed_inference_platform.cli.commands.skills_cmd import (
    META_NAMESPACE,
    SkillsApproveCommand,
    SkillsListCommand,
    SkillsPublishCommand,
    SkillsSyncCommand,
    build_artifact_zip,
    check_family_ownership,
    extract_record_meta,
    parse_skill_frontmatter,
    validate_meta,
    wait_for_record_ready,
)
from governed_inference_platform.config import Profile

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SKILL_MD = """---
name: code-review
description: Reviews code for org standards
---

# Code review

Do the review.
"""

VALID_META = {
    "name": "code-review",
    "version": "1.2.0",
    "description": "Reviews code for org standards",
    "category": "workflow",
    "model_compat": [
        {
            "model_family": "claude-sonnet-4-5",
            "model_ids": ["us.anthropic.claude-sonnet-4-5-20250929-v1:0"],
            "status": "verified",
        }
    ],
    "fork_of": None,
    "default_for_families": ["claude-sonnet-4-5"],
    "evals": {"suite_ref": None, "latest_score_ref": None, "gate": {"min_score": None}},
}


def _profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip",
        "provider_type": "okta",
        "skills_registry_enabled": True,
        "skills_registry_id": "reg-123",
        "skills_artifact_bucket": "skills-bucket",
        "skills_distributor_function": "arn:aws:lambda:us-east-1:111122223333:function:gip-skills-distributor",
    }
    data.update(overrides)
    return Profile.from_dict(data)


def _skill_dir(tmp_path: Path, meta: dict | None = None, skill_md: str = SKILL_MD) -> Path:
    d = tmp_path / "code-review"
    d.mkdir()
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if meta is not None:
        (d / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")
    return d


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _skill_archive(
    name="code-review",
    version="1.2.0",
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    skill_md = SKILL_MD if name == "code-review" else f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n"
    files = {
        "SKILL.md": skill_md.encode(),
        "_meta.json": json.dumps({"name": name, "version": version}).encode(),
    }
    files.update(extra_files or {})
    return _zip_bytes(files)


def _sync_lock(archive: bytes, name="code-review", version="1.2.0", schema_version=2) -> dict:
    digest = hashlib.sha256(archive).hexdigest()
    return {
        "schema_version": schema_version,
        "generated_at": "2026-07-08T00:00:00Z",
        "skills": [
            {
                "name": name,
                "version": version,
                "s3_uri": f"s3://skills-bucket/approved/sha256/{digest}.zip",
                "version_id": "approved-v1",
                "sha256": digest,
            }
        ],
    }


def _mock_sync_s3(lock: dict, artifact: bytes) -> MagicMock:
    mock_s3 = MagicMock()

    def get_object(Bucket, Key, VersionId=None):
        assert Bucket == "skills-bucket"
        body = MagicMock()
        if Key == "distribution/skills-lock.json":
            assert VersionId is None
            body.read.return_value = json.dumps(lock).encode()
            return {"Body": body}
        assert VersionId == "approved-v1"
        body.read.return_value = artifact
        return {"Body": body, "VersionId": VersionId}

    mock_s3.get_object.side_effect = get_object
    return mock_s3


def _run(command, args, profile, capsys=None):
    """Execute a command; rich Console writes to real stdout, so callers that
    assert on output pass pytest's capsys (repo pattern — see
    test_deploy_s3bucket_dispatch.py)."""
    with patch("governed_inference_platform.cli.commands.skills_cmd.Config") as MockConfig:
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        tester = CommandTester(command)
        code = tester.execute(args)
    output = tester.io.fetch_output() + tester.io.fetch_error()
    if capsys is not None:
        output += capsys.readouterr().out
    return code, output


# ---------------------------------------------------------------------------
# SKILL.md frontmatter validation
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_valid_frontmatter_parses(self):
        fm, errors = parse_skill_frontmatter(SKILL_MD)
        assert errors == []
        assert fm["name"] == "code-review"

    def test_missing_frontmatter_rejected(self):
        fm, errors = parse_skill_frontmatter("# no frontmatter\n")
        assert fm is None
        assert any("frontmatter" in e for e in errors)

    def test_missing_required_fields_rejected(self):
        _, errors = parse_skill_frontmatter("---\nfoo: bar\n---\nbody")
        assert any("'name' is required" in e for e in errors)
        assert any("'description' is required" in e for e in errors)


# ---------------------------------------------------------------------------
# _meta validation — rejection cases (frozen schema io.gip.skill/v1)
# ---------------------------------------------------------------------------


class TestMetaValidation:
    def test_valid_meta_passes(self):
        assert validate_meta(VALID_META, {"name": "code-review"}) == []

    def test_missing_name_rejected(self):
        meta = {k: v for k, v in VALID_META.items() if k != "name"}
        assert any("'name' is required" in e for e in validate_meta(meta, {}))

    def test_missing_version_rejected(self):
        meta = {k: v for k, v in VALID_META.items() if k != "version"}
        assert any("'version' is required" in e for e in validate_meta(meta, {}))

    def test_non_semver_version_rejected(self):
        meta = dict(VALID_META, version="1.2")
        assert any("semver" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_semver_with_leading_zero_is_rejected(self):
        meta = dict(VALID_META, version="01.2.0")
        assert any("semver" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_name_mismatch_with_frontmatter_rejected(self):
        errors = validate_meta(VALID_META, {"name": "other-skill"})
        assert any("does not match SKILL.md" in e for e in errors)

    def test_invalid_skill_name_rejected(self):
        meta = dict(VALID_META, name="Code_Review!")
        assert any("must match" in e for e in validate_meta(meta, {}))

    def test_unknown_top_level_keys_rejected(self):
        meta = dict(VALID_META, modelCompat=[])  # camelCase typo of model_compat
        assert any("unknown keys" in e and "modelCompat" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_bad_compat_status_rejected(self):
        meta = dict(VALID_META, model_compat=[{"model_family": "claude-sonnet-4-5", "status": "great"}])
        assert any("status must be one of" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_compat_entry_requires_model_family(self):
        meta = dict(VALID_META, model_compat=[{"status": "verified"}], default_for_families=[])
        assert any("model_family is required" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_default_family_without_compat_entry_rejected(self):
        meta = dict(VALID_META, default_for_families=["claude-sonnet-5"])
        errors = validate_meta(meta, {"name": "code-review"})
        assert any("no matching model_compat entry" in e for e in errors)

    def test_malformed_fork_of_rejected(self):
        meta = dict(VALID_META, fork_of={"skill": "code-review"})  # missing version
        assert any("fork_of" in e for e in validate_meta(meta, {"name": "code-review"}))

    def test_evals_unknown_keys_rejected(self):
        meta = dict(VALID_META, evals={"suite_ref": None, "scores": {}})
        assert any("evals has unknown keys" in e for e in validate_meta(meta, {"name": "code-review"}))


class TestFamilyOwnership:
    def _record_with_meta(self, meta):
        return {"descriptors": {"agentSkills": {"definition": {"_meta": {META_NAMESPACE: meta}}}}}

    def test_conflicting_default_family_rejected(self):
        existing = [self._record_with_meta(dict(VALID_META, version="1.1.0"))]
        errors = check_family_ownership(existing, dict(VALID_META, version="1.2.0"))
        assert any("default_for_families conflict" in e for e in errors)

    def test_other_skill_names_do_not_conflict(self):
        existing = [self._record_with_meta(dict(VALID_META, name="other-skill", version="1.0.0"))]
        assert check_family_ownership(existing, VALID_META) == []

    def test_republish_same_version_does_not_self_conflict(self):
        existing = [self._record_with_meta(dict(VALID_META))]
        assert check_family_ownership(existing, VALID_META) == []

    def test_no_claim_no_conflict(self):
        existing = [self._record_with_meta(dict(VALID_META, version="1.1.0"))]
        meta = dict(VALID_META, version="1.2.0", default_for_families=[])
        assert check_family_ownership(existing, meta) == []


# ---------------------------------------------------------------------------
# Artifact zip
# ---------------------------------------------------------------------------


class TestArtifactZip:
    def test_zip_is_deterministic(self, tmp_path):
        d = _skill_dir(tmp_path, VALID_META)
        assert build_artifact_zip(d) == build_artifact_zip(d)

    def test_zip_excludes_junk(self, tmp_path):
        import zipfile
        from io import BytesIO

        d = _skill_dir(tmp_path, VALID_META)
        (d / "__pycache__").mkdir()
        (d / "__pycache__" / "x.pyc").write_bytes(b"junk")
        (d / ".DS_Store").write_bytes(b"junk")
        names = zipfile.ZipFile(BytesIO(build_artifact_zip(d))).namelist()
        assert "SKILL.md" in names
        assert "scripts/check.py" in names
        assert not any("__pycache__" in n or ".DS_Store" in n for n in names)


# ---------------------------------------------------------------------------
# skills publish
# ---------------------------------------------------------------------------


class TestPublish:
    def _publish(
        self,
        tmp_path,
        capsys=None,
        meta=VALID_META,
        existing_records=None,
        skill_md=SKILL_MD,
        source_version_id: str | None = "source-v1",
        preexisting_keys: set[str] | None = None,
        conflicting_keys: set[str] | None = None,
    ):
        skill_dir = _skill_dir(tmp_path, meta, skill_md=skill_md)
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = existing_records or []
        mock_registry.create_record.return_value = "rec-1"
        mock_registry.get_record.return_value = {"status": "DRAFT"}
        mock_s3 = MagicMock()

        zip_key = "skills/code-review/1.2.0.zip"
        prefix = "skills/code-review/1.2.0/"
        zip_bytes = build_artifact_zip(skill_dir)
        expected_objects = {zip_key: zip_bytes}
        with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
            expected_objects.update(
                {f"{prefix}{info.filename}": archive.read(info) for info in archive.infolist() if not info.is_dir()}
            )
        objects = {key: expected_objects[key] for key in preexisting_keys or set()}
        objects.update(dict.fromkeys(conflicting_keys or set(), b"conflicting bytes"))
        version_ids = {key: f"existing-{index}" for index, key in enumerate(objects, start=1)}

        def put_object(**kwargs):
            key = kwargs["Key"]
            if key in objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            objects[key] = kwargs["Body"]
            if kwargs["Key"].endswith(".zip"):
                if source_version_id:
                    version_ids[key] = source_version_id
                    return {"VersionId": source_version_id}
                return {}
            version_ids[key] = "source-file-v1"
            return {"VersionId": "source-file-v1"}

        mock_s3.put_object.side_effect = put_object

        def get_object(Bucket, Key):
            assert Bucket == "skills-bucket"
            body = MagicMock()
            body.read.return_value = objects[Key]
            return {
                "Body": body,
                "ContentLength": len(objects[Key]),
                "VersionId": version_ids[Key],
            }

        mock_s3.get_object.side_effect = get_object

        def list_objects_v2(Bucket, Prefix, ContinuationToken=None):
            assert Bucket == "skills-bucket"
            assert ContinuationToken is None
            contents = [{"Key": key} for key in sorted(objects) if key.startswith(Prefix)]
            return {"KeyCount": len(contents), "Contents": contents, "IsTruncated": False}

        mock_s3.list_objects_v2.side_effect = list_objects_v2
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Arn": "arn:aws:sts::1:assumed-role/pub/jorge@example.com"}

        def fake_client(service, **kwargs):
            return {"s3": mock_s3, "sts": mock_sts}[service]

        with (
            patch(
                "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
                return_value=mock_registry,
            ),
            patch("boto3.client", side_effect=fake_client),
        ):
            code, output = _run(SkillsPublishCommand(), str(skill_dir), _profile(), capsys)
        return code, output, mock_registry, mock_s3

    def test_publish_happy_path(self, tmp_path):
        code, output, registry, s3 = self._publish(tmp_path)
        assert code == 0
        # The canonical ZIP is reserved before resumable directory writes.
        keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
        assert keys[0] == "skills/code-review/1.2.0.zip"
        assert "skills/code-review/1.2.0/SKILL.md" in keys
        assert "skills/code-review/1.2.0/_meta.json" in keys
        assert "skills/code-review/1.2.0/scripts/check.py" in keys
        assert "skills/code-review/1.2.0.zip" in keys
        assert all(call.kwargs["IfNoneMatch"] == "*" for call in s3.put_object.call_args_list)
        # Record created with the namespaced _meta payload and submitted
        registry.create_record.assert_called_once()
        kwargs = registry.create_record.call_args.kwargs
        assert kwargs["name"] == "code-review"
        assert kwargs["record_version"] == "1.2.0"
        payload = kwargs["definition"]["_meta"][META_NAMESPACE]
        assert payload["artifact"]["s3_uri"] == "s3://skills-bucket/skills/code-review/1.2.0/"
        assert payload["artifact"]["source_directory_s3_uri"] == "s3://skills-bucket/skills/code-review/1.2.0/"
        assert payload["artifact"]["source_s3_uri"] == "s3://skills-bucket/skills/code-review/1.2.0.zip"
        assert payload["artifact"]["source_version_id"] == "source-v1"
        assert len(payload["artifact"]["sha256"]) == 64
        assert payload["lifecycle"]["published_by"].startswith("arn:aws:sts")
        registry.submit_for_approval.assert_called_once_with("reg-123", "rec-1")

    def test_publish_waits_for_record_to_leave_creating(self):
        registry = MagicMock()
        registry.get_record.side_effect = [{"status": "CREATING"}, {"status": "DRAFT"}]

        with patch("governed_inference_platform.cli.commands.skills_cmd.time.sleep") as sleep:
            status = wait_for_record_ready(registry, "reg-123", "rec-1", poll_seconds=0)

        assert status == "DRAFT"
        assert registry.get_record.call_count == 2
        sleep.assert_called_once_with(0)

    def test_publish_rejects_invalid_meta_without_uploading(self, tmp_path, capsys):
        bad_meta = dict(VALID_META, version="not-semver")
        code, output, registry, s3 = self._publish(tmp_path, capsys, meta=bad_meta)
        assert code == 1
        assert "Validation failed" in output
        s3.put_object.assert_not_called()
        registry.create_record.assert_not_called()

    def test_publish_rejects_meta_frontmatter_name_mismatch(self, tmp_path):
        bad_meta = dict(VALID_META, name="not-code-review")
        code, output, registry, s3 = self._publish(tmp_path, meta=bad_meta)
        assert code == 1
        s3.put_object.assert_not_called()

    def test_publish_requires_meta_file(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, meta=None)
        code, output = _run(SkillsPublishCommand(), str(skill_dir), _profile(), capsys)
        assert code == 1
        assert "_meta.json not found" in output

    def test_publish_refuses_conflicting_existing_version(self, tmp_path, capsys):
        code, output, registry, s3 = self._publish(
            tmp_path,
            capsys,
            conflicting_keys={"skills/code-review/1.2.0.zip"},
        )
        assert code == 1
        assert "source artifact conflict" in output
        assert [call.kwargs["Key"] for call in s3.put_object.call_args_list] == ["skills/code-review/1.2.0.zip"]
        registry.create_record.assert_not_called()

    def test_publish_resumes_byte_identical_partial_upload(self, tmp_path):
        code, _, registry, s3 = self._publish(
            tmp_path,
            preexisting_keys={
                "skills/code-review/1.2.0.zip",
                "skills/code-review/1.2.0/SKILL.md",
            },
        )

        assert code == 0
        assert [call.kwargs["Key"] for call in s3.get_object.call_args_list] == [
            "skills/code-review/1.2.0.zip",
            "skills/code-review/1.2.0/SKILL.md",
        ]
        artifact = registry.create_record.call_args.kwargs["definition"]["_meta"][META_NAMESPACE]["artifact"]
        assert artifact["source_version_id"].startswith("existing-")

    def test_publish_requires_immutable_source_version(self, tmp_path, capsys):
        code, output, registry, _ = self._publish(tmp_path, capsys, source_version_id=None)
        assert code == 1
        assert "did not return an immutable version" in output
        registry.create_record.assert_not_called()

    def test_publish_enforces_family_ownership_invariant(self, tmp_path, capsys):
        existing = [
            {
                "descriptors": {
                    "agentSkills": {"definition": {"_meta": {META_NAMESPACE: dict(VALID_META, version="1.1.0")}}}
                }
            }
        ]
        code, output, registry, s3 = self._publish(tmp_path, capsys, existing_records=existing)
        assert code == 1
        assert "default_for_families conflict" in output
        s3.put_object.assert_not_called()

    def test_publish_requires_deployed_stack(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, VALID_META)
        profile = _profile(skills_registry_id="", skills_artifact_bucket="")
        with patch("governed_inference_platform.cli.commands.skills_cmd.get_stack_outputs", return_value={}):
            code, output = _run(SkillsPublishCommand(), str(skill_dir), profile, capsys)
        assert code == 1
        assert "gip deploy skills" in output

    def test_context_backfills_distributor_for_existing_profiles(self):
        profile = _profile(skills_distributor_function="")
        with patch.object(
            skills_cmd_module,
            "get_stack_outputs",
            return_value={"DistributorFunctionArn": "arn:aws:lambda:us-east-1:1:function:skills"},
        ):
            context, error = skills_cmd_module.resolve_skills_context(profile)

        assert error is None
        assert context is not None
        assert context["distributor_arn"] == "arn:aws:lambda:us-east-1:1:function:skills"


# ---------------------------------------------------------------------------
# skills approve / list
# ---------------------------------------------------------------------------


def _approved_record(name="code-review", version="1.2.0", status="PENDING_APPROVAL"):
    return {
        "recordId": f"rec-{name}",
        "name": name,
        "recordVersion": version,
        "status": status,
        "descriptors": {
            "agentSkills": {
                "definition": {
                    "_meta": {
                        META_NAMESPACE: dict(
                            VALID_META,
                            name=name,
                            version=version,
                            artifact={
                                "s3_uri": f"s3://skills-bucket/skills/{name}/{version}/",
                                "source_directory_s3_uri": f"s3://skills-bucket/skills/{name}/{version}/",
                                "source_s3_uri": f"s3://skills-bucket/skills/{name}/{version}.zip",
                                "source_version_id": "source-v1",
                                "zip_url": f"https://skills-bucket.s3.us-east-1.amazonaws.com/skills/{name}/{version}.zip",
                                "sha256": "a" * 64,
                            },
                        )
                    }
                }
            }
        },
    }


class TestApprove:
    def _approve(self, args, records=None, capsys=None, distributor_result=None):
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = records if records is not None else [_approved_record()]
        mock_lambda = MagicMock()
        payload = MagicMock()
        status = "REJECTED" if "--reject" in args else "APPROVED"
        payload.read.return_value = json.dumps(
            distributor_result
            or (
                {"status": "REJECTED"}
                if status == "REJECTED"
                else {
                    "status": "APPROVED",
                    "skills": 1,
                    "distributed_skills": [{"name": "code-review", "version": "1.2.0"}],
                    "written_outputs": [
                        "s3://skills-bucket/distribution/marketplace.json",
                        "s3://skills-bucket/distribution/skills-lock.json",
                    ],
                    "skipped_outputs": [],
                }
            )
        ).encode()
        mock_lambda.invoke.return_value = {"StatusCode": 200, "Payload": payload}
        with (
            patch(
                "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
                return_value=mock_registry,
            ),
            patch("boto3.client", return_value=mock_lambda),
        ):
            code, output = _run(SkillsApproveCommand(), args, _profile(), capsys)
        return code, output, mock_registry, mock_lambda

    def test_approve_updates_status_and_distributes(self):
        code, output, registry, lam = self._approve("code-review@1.2.0")
        assert code == 0
        registry.update_record_status.assert_not_called()
        lam.invoke.assert_called_once()
        assert lam.invoke.call_args.kwargs["FunctionName"].endswith("gip-skills-distributor")
        request = json.loads(lam.invoke.call_args.kwargs["Payload"])
        assert request == {
            "action": "approve",
            "record_id": "rec-code-review",
            "reason": "",
            "distribute": True,
        }

    def test_reject_is_mediated_by_distributor(self):
        code, output, registry, lam = self._approve("code-review@1.2.0 --reject --reason nope")
        assert code == 0
        registry.update_record_status.assert_not_called()
        request = json.loads(lam.invoke.call_args.kwargs["Payload"])
        assert request["action"] == "reject"
        assert request["reason"] == "nope"

    def test_distributor_function_error_returns_nonzero(self, capsys):
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = [_approved_record()]
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = b'{"errorMessage":"boom"}'
        mock_lambda.invoke.return_value = {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": payload}
        with (
            patch(
                "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
                return_value=mock_registry,
            ),
            patch("boto3.client", return_value=mock_lambda),
        ):
            code, output = _run(SkillsApproveCommand(), "code-review@1.2.0", _profile(), capsys)

        assert code == 1
        assert "Distributor invocation failed" in output
        mock_registry.update_record_status.assert_not_called()

    def test_distributor_zero_skills_payload_returns_nonzero(self, capsys):
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = [_approved_record()]
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = json.dumps({"status": "APPROVED", "skills": 0, "written_outputs": []}).encode()
        mock_lambda.invoke.return_value = {"StatusCode": 200, "Payload": payload}
        with (
            patch(
                "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
                return_value=mock_registry,
            ),
            patch("boto3.client", return_value=mock_lambda),
        ):
            code, output = _run(SkillsApproveCommand(), "code-review@1.2.0", _profile(), capsys)

        assert code == 1
        assert "zero approved skills" in output

    def test_distributor_payload_must_include_approved_skill(self, capsys):
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = [_approved_record()]
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = json.dumps(
            {
                "status": "APPROVED",
                "skills": 1,
                "distributed_skills": [{"name": "other", "version": "1.0.0"}],
                "written_outputs": [
                    "s3://skills-bucket/distribution/marketplace.json",
                    "s3://skills-bucket/distribution/skills-lock.json",
                ],
            }
        ).encode()
        mock_lambda.invoke.return_value = {"StatusCode": 200, "Payload": payload}
        with (
            patch(
                "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
                return_value=mock_registry,
            ),
            patch("boto3.client", return_value=mock_lambda),
        ):
            code, output = _run(SkillsApproveCommand(), "code-review@1.2.0", _profile(), capsys)

        assert code == 1
        assert "did not confirm code-review@1.2.0" in output

    def test_no_distribute_still_invokes_atomic_approval_workflow(self, capsys):
        code, output, registry, lam = self._approve("code-review@1.2.0 --no-distribute", capsys=capsys)
        assert code == 0
        registry.update_record_status.assert_not_called()
        lam.invoke.assert_called_once()
        request = json.loads(lam.invoke.call_args.kwargs["Payload"])
        assert request["action"] == "approve"
        assert request["distribute"] is False
        assert "Immutable artifacts were promoted" in output

    def test_approving_older_version_reports_inactive_without_false_failure(self, capsys):
        result = {
            "status": "APPROVED",
            "skills": 1,
            "active_version_rule": "highest-semver",
            "approved_skills": [
                {"name": "code-review", "version": "1.2.0"},
                {"name": "code-review", "version": "2.0.0"},
            ],
            "distributed_skills": [{"name": "code-review", "version": "2.0.0"}],
            "written_outputs": [
                "s3://skills-bucket/distribution/marketplace.json",
                "s3://skills-bucket/distribution/skills-lock.json",
            ],
            "skipped_outputs": [],
        }

        code, output, _, _ = self._approve(
            "code-review@1.2.0",
            capsys=capsys,
            distributor_result=result,
        )

        assert code == 0
        assert "approved but inactive" in output

    def test_unknown_record_fails(self, capsys):
        code, output, _, _ = self._approve("nope@9.9.9", capsys=capsys)
        assert code == 1
        assert "No registry record found" in output

    def test_malformed_spec_fails(self, capsys):
        code, output, _, _ = self._approve("code-review", capsys=capsys)
        assert code == 1
        assert "name>@<version" in output


class TestList:
    def test_list_renders_records(self, capsys):
        mock_registry = MagicMock()
        mock_registry.list_records.return_value = [_approved_record(status="APPROVED")]
        with patch(
            "governed_inference_platform.cli.commands.skills_cmd.AgentRegistryClient",
            return_value=mock_registry,
        ):
            code, output = _run(SkillsListCommand(), "", _profile(), capsys)
        assert code == 0
        assert "code-review" in output
        assert "1.2.0" in output
        assert "APPROVED" in output

    def test_extract_record_meta_defensive(self):
        assert extract_record_meta({}) is None
        assert extract_record_meta({"descriptors": {"agentSkills": {"definition": {"_meta": {}}}}}) is None

    def test_extract_record_meta_accepts_agent_registry_inline_content_shape(self):
        record = {
            "descriptors": {
                "agentSkills": {
                    "skillDefinition": {
                        "schemaVersion": "0.1.0",
                        "inlineContent": json.dumps({"_meta": {META_NAMESPACE: VALID_META}}),
                    }
                }
            }
        }

        assert extract_record_meta(record)["name"] == "code-review"

    def test_extract_record_meta_rejects_invalid_inline_content_json(self):
        record = {"recordId": "rec-1", "descriptors": {"agentSkills": {"skillDefinition": {"inlineContent": "{"}}}}

        try:
            extract_record_meta(record)
        except ValueError as e:
            assert "invalid skillDefinition JSON" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_extract_record_meta_skips_non_object_inline_content(self):
        record = {"descriptors": {"agentSkills": {"skillDefinition": {"inlineContent": "[]"}}}}

        assert extract_record_meta(record) is None

    def test_extract_record_meta_accepts_ga_agent_skills_definition_shape(self):
        # GA descriptor shape (registry-faq "Change 3", retrieved 2026-07-29)
        record = {
            "recordType": "SKILL",
            "descriptors": {
                "agentSkillsDefinition": {
                    "dataSchemaVersion": "0.1.0",
                    "data": json.dumps({"_meta": {META_NAMESPACE: VALID_META}}),
                    "additionalData": {"skillMd": {"data": "# skill"}},
                }
            },
        }

        assert extract_record_meta(record)["name"] == "code-review"

    def test_extract_record_meta_rejects_invalid_ga_data_json(self):
        record = {"recordId": "rec-1", "descriptors": {"agentSkillsDefinition": {"data": "{"}}}

        try:
            extract_record_meta(record)
        except ValueError as e:
            assert "invalid skillDefinition JSON" in str(e)
        else:
            raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# skills sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_sync_materializes_skills_into_harness_dirs(self, tmp_path, monkeypatch):
        archive = _skill_archive(extra_files={"scripts/check.py": b"print('ok')\n"})
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile())

        assert code == 0
        # R13 per-harness matrix: ~/.claude/skills (Claude Code + OpenCode reads
        # it natively) and ~/.codex/skills (Codex CLI user dir)
        for harness_dir in (".claude", ".codex"):
            root = fake_home / harness_dir / "skills" / "code-review"
            assert (root / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
            assert (root / "scripts" / "check.py").read_bytes() == b"print('ok')\n"
        artifact_call = mock_s3.get_object.call_args_list[1]
        assert artifact_call.kwargs["VersionId"] == "approved-v1"

    def test_sync_single_harness_option(self, tmp_path, monkeypatch):
        archive = _skill_archive(name="s1", version="1.0.0")
        lock = _sync_lock(archive, name="s1", version="1.0.0")
        mock_s3 = _mock_sync_s3(lock, archive)

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, _ = _run(SkillsSyncCommand(), "--harness codex", _profile())
        assert code == 0
        assert (fake_home / ".codex" / "skills" / "s1" / "SKILL.md").exists()
        assert not (fake_home / ".claude" / "skills" / "s1").exists()

    def test_sync_without_lock_file_explains(self, capsys):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)
        assert code == 1
        assert "skills-lock.json" in output

    def test_sync_rejects_invalid_lock_entry(self, capsys):
        lock = {
            "schema_version": 2,
            "skills": [
                {
                    "name": "../escape",
                    "version": "1.0.0",
                    "s3_uri": f"s3://skills-bucket/approved/sha256/{'a' * 64}.zip",
                    "version_id": "approved-v1",
                    "sha256": "a" * 64,
                }
            ],
        }
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = json.dumps(lock).encode()
        mock_s3.get_object.return_value = {"Body": body}

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Invalid approved skill" in output
        assert "entry in lock file" in output

    def test_sync_rejects_legacy_lock_and_preserves_installation(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive, schema_version=1)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        installed = fake_home / ".claude" / "skills" / "code-review"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Unsupported skills lock schema" in output
        assert (installed / "SKILL.md").read_bytes() == b"old"
        assert mock_s3.get_object.call_count == 1

    def test_sync_rejects_mutable_source_uri_before_download(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        lock["skills"][0]["s3_uri"] = "s3://skills-bucket/skills/code-review/1.2.0.zip"
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        installed = fake_home / ".claude" / "skills" / "code-review"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Untrusted approved" in output
        assert "artifact URI" in output
        assert (installed / "SKILL.md").read_bytes() == b"old"
        assert mock_s3.get_object.call_count == 1

    def test_partial_download_digest_mismatch_preserves_all_installations(self, tmp_path, monkeypatch, capsys):
        complete_archive = _skill_archive()
        lock = _sync_lock(complete_archive)
        mock_s3 = _mock_sync_s3(lock, complete_archive[:10])
        fake_home = tmp_path / "home"
        for harness_dir in (".claude", ".codex"):
            installed = fake_home / harness_dir / "skills" / "code-review"
            installed.mkdir(parents=True)
            (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "SHA-256 mismatch" in output
        for harness_dir in (".claude", ".codex"):
            assert (fake_home / harness_dir / "skills" / "code-review" / "SKILL.md").read_bytes() == b"old"

    def test_sync_rejects_path_traversal_archive_before_install(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive(extra_files={"../evil.py": b"bad"})
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Unsafe path in skill" in output
        assert "archive" in output
        assert not (fake_home / "evil.py").exists()

    def test_sync_rejects_malformed_zip_before_install(self, tmp_path, monkeypatch, capsys):
        archive = b"not a zip archive"
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        installed = fake_home / ".claude" / "skills" / "code-review"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Skill artifact is not" in output
        assert "valid ZIP archive" in output
        assert (installed / "SKILL.md").read_bytes() == b"old"

    def test_sync_rejects_zip_bomb_ratio_before_install(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive(extra_files={"payload.txt": b"A" * 200_000})
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "compression ratio" in output
        assert not (fake_home / ".claude" / "skills" / "code-review").exists()

    def test_sync_enforces_member_count_before_install(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive(extra_files={"extra.txt": b"extra"})
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(skills_cmd_module, "MAX_SKILL_ARCHIVE_MEMBERS", 2)

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "maximum allowed is 2" in output
        assert not (fake_home / ".claude" / "skills" / "code-review").exists()

    def test_sync_enforces_expanded_size_before_install(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(skills_cmd_module, "MAX_SKILL_EXPANDED_BYTES", 10)

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "Skill archive expands" in output
        assert "beyond 10 bytes" in output
        assert not (fake_home / ".claude" / "skills" / "code-review").exists()

    def test_sync_rejects_duplicate_active_name_before_download(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        duplicate = dict(lock["skills"][0], version="2.0.0")
        lock["skills"].append(duplicate)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "multiple active versions" in output
        assert mock_s3.get_object.call_count == 1

    def test_sync_rejects_oversized_archive_before_read(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        mock_s3 = MagicMock()
        lock_body = MagicMock()
        lock_body.read.return_value = json.dumps(lock).encode()
        artifact_body = MagicMock()
        mock_s3.get_object.side_effect = [
            {"Body": lock_body},
            {
                "Body": artifact_body,
                "ContentLength": skills_cmd_module.MAX_SKILL_ARCHIVE_BYTES + 1,
                "VersionId": "approved-v1",
            },
        ]
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        with patch("boto3.client", return_value=mock_s3):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "maximum allowed" in output
        artifact_body.read.assert_not_called()

    def test_atomic_commit_rolls_back_every_harness_on_replace_failure(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        for harness_dir in (".claude", ".codex"):
            installed = fake_home / harness_dir / "skills" / "code-review"
            installed.mkdir(parents=True)
            (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        real_replace = os.replace

        def fail_codex_stage(source, destination):
            source_path = Path(source)
            if (
                ".codex" in source_path.parts
                and source_path.name.startswith(".code-review.gip-")
                and not source_path.name.endswith(".previous")
            ):
                raise OSError("simulated atomic replace failure")
            return real_replace(source, destination)

        with (
            patch("boto3.client", return_value=mock_s3),
            patch.object(skills_cmd_module.os, "replace", side_effect=fail_codex_stage),
        ):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 1
        assert "existing installations were preserved" in output
        for harness_dir in (".claude", ".codex"):
            assert (fake_home / harness_dir / "skills" / "code-review" / "SKILL.md").read_bytes() == b"old"

    def test_backup_cleanup_failure_warns_after_successful_commit(self, tmp_path, monkeypatch, capsys):
        archive = _skill_archive()
        lock = _sync_lock(archive)
        mock_s3 = _mock_sync_s3(lock, archive)
        fake_home = tmp_path / "home"
        for harness_dir in (".claude", ".codex"):
            installed = fake_home / harness_dir / "skills" / "code-review"
            installed.mkdir(parents=True)
            (installed / "SKILL.md").write_bytes(b"old")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        real_remove = skills_cmd_module._remove_path

        def fail_backup_cleanup(path):
            if path.name.endswith(".previous"):
                raise OSError("simulated cleanup failure")
            return real_remove(path)

        with (
            patch("boto3.client", return_value=mock_s3),
            patch.object(skills_cmd_module, "_remove_path", side_effect=fail_backup_cleanup),
        ):
            code, output = _run(SkillsSyncCommand(), "", _profile(), capsys)

        assert code == 0
        assert "backup cleanup failed" in output
        assert "simulated cleanup failure" in output
        for harness_dir in (".claude", ".codex"):
            installed = fake_home / harness_dir / "skills" / "code-review"
            assert (installed / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
            assert list(installed.parent.glob(".code-review.gip-*.previous"))


# ---------------------------------------------------------------------------
# Registration + deploy dispatch
# ---------------------------------------------------------------------------


class TestRegistrationAndDispatch:
    def test_skills_in_valid_and_destroyable_stacks(self):
        assert "skills" in VALID_STACKS
        assert "skills" in DESTROYABLE_STACKS

    def test_deploy_skills_requires_enablement(self, capsys):
        profile = _profile(skills_registry_enabled=False)
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            tester = CommandTester(DeployCommand())
            code = tester.execute("skills")
        assert code == 1
        assert "skills registry is not enabled" in capsys.readouterr().out

    def test_build_skills_params(self):
        profile = _profile(
            skills_publisher_groups=["skill-publishers"],
            skills_curator_groups=["skill-curators"],
            skills_organization_id="o-abc123def456",
        )
        params = build_skills_params(profile)
        assert "RegistryName=gip-skills" in params
        assert "OrganizationId=o-abc123def456" in params
        assert "PublisherGroups=skill-publishers" in params
        assert "CuratorGroups=skill-curators" in params

    def test_build_skills_params_minimal(self):
        params = build_skills_params(_profile())
        assert params == ["RegistryName=gip-skills"]

    def test_full_deploy_schedules_s3bucket_before_skills(self, capsys):
        profile = _profile(monitoring_enabled=False)
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            code = CommandTester(DeployCommand()).execute("--dry-run")

        output = capsys.readouterr().out
        assert code == 0
        assert output.index("s3bucket") < output.index("skills")

    def test_full_deploy_skips_legacy_device_code_bootstrap(self, capsys):
        profile = _profile(monitoring_enabled=False, cowork_config_delivery="bootstrap-device-code")
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            code = CommandTester(DeployCommand()).execute("--dry-run")

        output = capsys.readouterr().out
        assert code == 0
        assert "Skipping legacy bootstrap settings" in output
        assert "Bootstrap Server (" not in output

    def test_skills_commands_registered(self):
        from governed_inference_platform.cli import create_application

        app = create_application()
        for name in ("skills", "skills publish", "skills list", "skills approve", "skills sync"):
            assert app.find(name) is not None


# ---------------------------------------------------------------------------
# Profile fields + init answers schema
# ---------------------------------------------------------------------------


class TestProfileAndAnswers:
    def test_legacy_profile_defaults(self):
        legacy = {
            "name": "legacy",
            "provider_domain": "example.okta.com",
            "client_id": "abc",
            "credential_storage": "session",
            "aws_region": "us-west-2",
            "identity_pool_name": "gip",
        }
        profile = Profile.from_dict(legacy)
        assert profile.skills_registry_enabled is False
        assert profile.skills_registry_name == "gip-skills"
        assert profile.skills_publisher_groups == []
        assert profile.skills_organization_id == ""

    def test_profile_roundtrip(self):
        profile = _profile(skills_publisher_groups=["g1"], skills_organization_id="o-abc123def456")
        restored = Profile.from_dict(profile.to_dict())
        assert restored.skills_registry_enabled is True
        assert restored.skills_publisher_groups == ["g1"]
        assert restored.skills_organization_id == "o-abc123def456"
        assert restored.skills_registry_id == "reg-123"

    def test_answers_schema_accepts_skills_block(self):
        answers = {
            "skills": {
                "enabled": True,
                "publisher_groups": ["skill-publishers"],
                "curator_groups": ["skill-curators"],
                "organization_id": "o-abc123def456",
                "registry_name": "gip-skills",
            }
        }
        assert check_structure(answers) == []

    def test_answers_schema_rejects_unknown_skills_key(self):
        errors = check_structure({"skills": {"enable": True}})
        assert any("skills.enable" in e for e in errors)

    def test_validate_config_rejects_bad_org_id(self):
        from governed_inference_platform.cli.commands.init_answers import DEFAULTS, deep_merge

        config = deep_merge(DEFAULTS, {"skills": {"enabled": True, "organization_id": "org-nope"}})
        config.setdefault("okta", {})["domain"] = "example.okta.com"
        config["okta"]["client_id"] = "0oa1example2"
        errors = validate_config(config)
        assert any("skills.organization_id" in e for e in errors)

    def test_validate_config_rejects_non_list_groups(self):
        from governed_inference_platform.cli.commands.init_answers import DEFAULTS, deep_merge

        config = deep_merge(DEFAULTS, {"skills": {"enabled": True, "publisher_groups": "not-a-list"}})
        config.setdefault("okta", {})["domain"] = "example.okta.com"
        config["okta"]["client_id"] = "0oa1example2"
        errors = validate_config(config)
        assert any("skills.publisher_groups" in e for e in errors)
