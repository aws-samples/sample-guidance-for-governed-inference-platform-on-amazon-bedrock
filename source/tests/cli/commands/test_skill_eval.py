# ABOUTME: Tests for the deterministic skill eval runner.
# ABOUTME: Covers eval.yaml schema validation, hard assertions, drift scoring, and command wiring.

import json
import sys
from pathlib import Path
from unittest.mock import patch

import yaml
from cleo.testers.command_tester import CommandTester

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.skill_eval import (  # noqa: E402
    EVAL_FILE,
    SkillsEvalCommand,
    known_model_ids,
    load_eval_manifest,
    model_family,
    run_case_trial,
    run_eval,
    snapshot_forbidden,
    validate_eval_manifest,
)

SONNET_45 = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
SONNET_5 = "global.anthropic.claude-sonnet-5"

SKILL_MD = """---
name: terraform-review
description: Reviews terraform plans
---

# Terraform review
"""

PY = sys.executable


def _manifest(**overrides) -> dict:
    manifest = {
        "schema_version": 1,
        "skill": "terraform-review",
        "harness": "claude-code",
        "timeout_seconds": 30,
        "trials": 1,
        "pass_threshold": 1.0,
        "baseline_model": SONNET_45,
        "models": [SONNET_45, SONNET_5],
        "cases": [
            {
                "id": "basic",
                "prompt": "Review the plan",
                "expect": {"files": [{"path": "review.md", "contains": ["## Findings"]}]},
            }
        ],
    }
    manifest.update(overrides)
    return manifest


def _skill_dir(tmp_path: Path, manifest: dict | None = None, skill_md: str = SKILL_MD) -> Path:
    d = tmp_path / "terraform-review"
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if manifest is not None:
        (d / EVAL_FILE).write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return d


def _write_fixture(skill_dir: Path, rel: str, content: str = "fixture") -> Path:
    path = skill_dir / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _case(command: list[str], expect: dict, **overrides) -> dict:
    case = {"id": "case-1", "command": command, "expect": expect}
    case.update(overrides)
    return case


def _py_case(code: str, expect: dict, **overrides) -> dict:
    return _case([PY, "-c", code], expect, **overrides)


class TestModelFamily:
    def test_cris_profile_with_date_and_version_collapses(self):
        assert model_family(SONNET_45) == "claude-sonnet-4-5"

    def test_global_profile_without_date(self):
        assert model_family(SONNET_5) == "claude-sonnet-5"

    def test_base_model_id_with_bare_version_suffix(self):
        assert model_family("anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"

    def test_regional_variants_collapse_to_one_family(self):
        ids = [SONNET_45, "eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"]
        assert {model_family(i) for i in ids} == {"claude-sonnet-4-5"}

    def test_catalog_contains_profile_and_base_ids(self):
        ids = known_model_ids()
        assert SONNET_45 in ids
        assert "anthropic.claude-sonnet-4-5-20250929-v1:0" in ids


class TestManifestValidation:
    def test_valid_manifest_passes(self):
        assert validate_eval_manifest(_manifest()) == []

    def test_load_rejects_non_mapping(self, tmp_path):
        path = tmp_path / EVAL_FILE
        path.write_text("- just\n- a list\n", encoding="utf-8")
        manifest, errors = load_eval_manifest(path)
        assert manifest is None
        assert any("mapping" in e for e in errors)

    def test_wrong_schema_version_rejected(self):
        errors = validate_eval_manifest(_manifest(schema_version=2))
        assert any("schema_version" in e for e in errors)

    def test_unknown_top_level_key_rejected(self):
        errors = validate_eval_manifest(_manifest(variance={"normalize": []}))
        assert any("unknown keys" in e and "variance" in e for e in errors)

    def test_missing_skill_rejected(self):
        manifest = _manifest()
        del manifest["skill"]
        assert any("'skill' is required" in e for e in validate_eval_manifest(manifest))

    def test_skill_frontmatter_mismatch_rejected(self):
        errors = validate_eval_manifest(_manifest(), frontmatter={"name": "other-skill"})
        assert any("does not match SKILL.md" in e for e in errors)

    def test_unknown_harness_rejected(self):
        errors = validate_eval_manifest(_manifest(harness="cursor"))
        assert any("'harness'" in e for e in errors)

    def test_models_required_non_empty(self):
        errors = validate_eval_manifest(_manifest(models=[]))
        assert any("'models'" in e for e in errors)

    def test_unknown_model_id_rejected_against_catalog(self):
        manifest = _manifest(models=[SONNET_45, "us.anthropic.claude-nonexistent-9"], baseline_model=SONNET_45)
        errors = validate_eval_manifest(manifest, catalog_ids=known_model_ids())
        assert any("not in the models.py catalog" in e for e in errors)

    def test_catalog_model_ids_accepted(self):
        assert validate_eval_manifest(_manifest(), catalog_ids=known_model_ids()) == []

    def test_baseline_must_be_in_models(self):
        errors = validate_eval_manifest(_manifest(baseline_model="anthropic.claude-opus-4-6-v1"))
        assert any("'baseline_model'" in e for e in errors)

    def test_pass_threshold_bounds(self):
        assert any("pass_threshold" in e for e in validate_eval_manifest(_manifest(pass_threshold=0)))
        assert any("pass_threshold" in e for e in validate_eval_manifest(_manifest(pass_threshold=1.5)))

    def test_trials_must_be_positive_int(self):
        assert any("trials" in e for e in validate_eval_manifest(_manifest(trials=0)))
        assert any("trials" in e for e in validate_eval_manifest(_manifest(trials=True)))

    def test_cases_required(self):
        assert any("'cases'" in e for e in validate_eval_manifest(_manifest(cases=[])))

    def test_duplicate_case_ids_rejected(self):
        case = {"id": "dup", "expect": {"files": [{"path": "a.md"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case, dict(case)]))
        assert any("duplicate case id" in e for e in errors)

    def test_case_unknown_key_rejected(self):
        case = {"id": "c", "expect": {"files": [{"path": "a.md"}]}, "tool_calls": {}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("cases[0]" in e and "tool_calls" in e for e in errors)

    def test_case_requires_expect(self):
        errors = validate_eval_manifest(_manifest(cases=[{"id": "c"}]))
        assert any("expect is required" in e for e in errors)

    def test_case_rejects_vacuous_expect(self):
        case = {"id": "c", "expect": {"files": [], "forbidden_paths": []}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("at least one assertion" in e for e in errors)

    def test_trial_without_assertions_fails_closed(self, tmp_path):
        skill_dir = _skill_dir(tmp_path, None)
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        result = run_case_trial(skill_dir, {"id": "c", "expect": {"files": []}}, SONNET_45, 30, ws_root)
        assert result["passed"] is False
        assert any(a["assertion"] == "hard_assertions" for a in result["assertions"])

    def test_exit_code_requires_command(self):
        case = {"id": "c", "expect": {"exit_code": 0}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("exit_code requires a command" in e for e in errors)

    def test_command_must_be_argv_list(self):
        case = {"id": "c", "command": "echo hi", "expect": {"exit_code": 0}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("command must be a non-empty list" in e for e in errors)

    def test_file_path_traversal_rejected(self):
        case = {"id": "c", "expect": {"files": [{"path": "../escape.md"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("no '..'" in e for e in errors)

    def test_fixtures_path_traversal_rejected(self):
        case = {"id": "c", "fixtures": "../outside", "expect": {"files": [{"path": "a.md"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("fixtures" in e and "no '..'" in e for e in errors)

    def test_json_schema_path_traversal_rejected(self):
        case = {"id": "c", "expect": {"files": [{"path": "a.json", "json_schema": "/tmp/schema.json"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("json_schema" in e and "no '..'" in e for e in errors)

    def test_forbidden_path_traversal_rejected(self):
        case = {"id": "c", "expect": {"forbidden_paths": ["../*.tfstate"]}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("forbidden_paths" in e and "no '..'" in e for e in errors)

    def test_must_exist_false_with_contains_rejected(self):
        case = {"id": "c", "expect": {"files": [{"path": "a.md", "must_exist": False, "contains": ["x"]}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("must_exist: false" in e for e in errors)

    def test_missing_fixtures_dir_rejected_with_skill_dir(self, tmp_path):
        skill_dir = _skill_dir(tmp_path, None)
        case = {"id": "c", "fixtures": "evals/fixtures/none", "expect": {"files": [{"path": "a.md"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]), skill_dir=skill_dir)
        assert any("fixtures directory not found" in e for e in errors)

    def test_missing_json_schema_file_rejected_with_skill_dir(self, tmp_path):
        skill_dir = _skill_dir(tmp_path, None)
        case = {"id": "c", "expect": {"files": [{"path": "a.json", "json_schema": "evals/nope.json"}]}}
        errors = validate_eval_manifest(_manifest(cases=[case]), skill_dir=skill_dir)
        assert any("json_schema file not found" in e for e in errors)

    def test_judge_is_validated_but_allowed(self):
        case = {
            "id": "c",
            "expect": {"files": [{"path": "a.md"}]},
            "judge": [{"criterion": "Findings are actionable", "min_score": 0.7}],
        }
        assert validate_eval_manifest(_manifest(cases=[case])) == []

    def test_judge_unknown_key_rejected(self):
        case = {"id": "c", "expect": {"files": [{"path": "a.md"}]}, "judge": [{"criterion": "x", "gates": True}]}
        errors = validate_eval_manifest(_manifest(cases=[case]))
        assert any("judge[0]" in e and "gates" in e for e in errors)


class TestTrialAssertions:
    def _run(self, tmp_path, case, fixtures: dict[str, str] | None = None):
        skill_dir = _skill_dir(tmp_path, None)
        if fixtures:
            for rel, content in fixtures.items():
                _write_fixture(skill_dir, rel, content)
        ws_root = tmp_path / "ws"
        ws_root.mkdir(exist_ok=True)
        return run_case_trial(skill_dir, case, SONNET_45, timeout_seconds=30, workspace_root=ws_root)

    def test_exit_code_pass_and_fail(self, tmp_path):
        ok = self._run(tmp_path, _py_case("raise SystemExit(0)", {"exit_code": 0}))
        assert ok["passed"] is True
        bad = self._run(tmp_path, _py_case("raise SystemExit(3)", {"exit_code": 0}))
        assert bad["passed"] is False
        assert any(a["assertion"] == "exit_code" and "got 3" in a["detail"] for a in bad["assertions"])

    def test_expected_file_with_contains_anchor(self, tmp_path):
        code = "open('review.md','w').write('## Findings\\nplan.json looks fine')"
        case = _py_case(
            code, {"exit_code": 0, "files": [{"path": "review.md", "contains": ["## Findings", "plan.json"]}]}
        )
        result = self._run(tmp_path, case)
        assert result["passed"] is True

    def test_missing_anchor_fails(self, tmp_path):
        case = _py_case(
            "open('review.md','w').write('empty')", {"files": [{"path": "review.md", "contains": ["## Findings"]}]}
        )
        result = self._run(tmp_path, case)
        assert result["passed"] is False
        assert any("anchor not found" in a["detail"] for a in result["assertions"])

    def test_missing_file_fails(self, tmp_path):
        result = self._run(tmp_path, _py_case("pass", {"files": [{"path": "review.md"}]}))
        assert result["passed"] is False
        assert any("file not found" in a["detail"] for a in result["assertions"])

    def test_must_exist_false_passes_when_absent_fails_when_present(self, tmp_path):
        absent = self._run(tmp_path, _py_case("pass", {"files": [{"path": "out.md", "must_exist": False}]}))
        assert absent["passed"] is True
        present = self._run(
            tmp_path,
            _py_case("open('out.md','w').write('x')", {"files": [{"path": "out.md", "must_exist": False}]}),
        )
        assert present["passed"] is False

    def test_forbidden_path_untouched_passes(self, tmp_path):
        case = _py_case("pass", {"forbidden_paths": ["*.tfstate", ".env"]})
        case["fixtures"] = "evals/fixtures/basic"
        result = self._run(tmp_path, case, fixtures={"evals/fixtures/basic/prod.tfstate": "state"})
        assert result["passed"] is True

    def test_forbidden_path_modified_fails(self, tmp_path):
        case = _py_case("open('prod.tfstate','w').write('tampered')", {"forbidden_paths": ["*.tfstate"]})
        case["fixtures"] = "evals/fixtures/basic"
        result = self._run(tmp_path, case, fixtures={"evals/fixtures/basic/prod.tfstate": "state"})
        assert result["passed"] is False
        assert any("forbidden paths touched" in a["detail"] for a in result["assertions"])

    def test_forbidden_path_created_fails(self, tmp_path):
        result = self._run(tmp_path, _py_case("open('.env','w').write('SECRET=1')", {"forbidden_paths": [".env"]}))
        assert result["passed"] is False

    def test_forbidden_path_deleted_fails(self, tmp_path):
        case = _py_case("import os; os.remove('.env')", {"forbidden_paths": [".env"]})
        case["fixtures"] = "evals/fixtures/basic"
        result = self._run(tmp_path, case, fixtures={"evals/fixtures/basic/.env": "SECRET=1"})
        assert result["passed"] is False

    def test_json_schema_assertion(self, tmp_path):
        schema = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array"}}}
        skill_dir = _skill_dir(tmp_path, None)
        _write_fixture(skill_dir, "evals/schemas/findings.schema.json", json.dumps(schema))
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        good = _py_case(
            'open("findings.json","w").write(\'{"findings": []}\')',
            {"files": [{"path": "findings.json", "json_schema": "evals/schemas/findings.schema.json"}]},
        )
        assert run_case_trial(skill_dir, good, SONNET_45, 30, ws_root)["passed"] is True
        bad = _py_case(
            'open("findings.json","w").write(\'{"other": 1}\')',
            {"files": [{"path": "findings.json", "json_schema": "evals/schemas/findings.schema.json"}]},
        )
        result = run_case_trial(skill_dir, bad, SONNET_45, 30, ws_root)
        assert result["passed"] is False
        assert any("schema violation" in a["detail"] for a in result["assertions"])

    def test_command_timeout_fails_trial(self, tmp_path):
        case = _py_case("import time; time.sleep(30)", {"exit_code": 0})
        skill_dir = _skill_dir(tmp_path, None)
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        result = run_case_trial(skill_dir, case, SONNET_45, timeout_seconds=1, workspace_root=ws_root)
        assert result["passed"] is False
        assert any("timed out" in a["detail"] for a in result["assertions"])

    def test_command_not_found_fails_trial(self, tmp_path):
        result = self._run(tmp_path, _case(["gip-eval-no-such-binary"], {"exit_code": 0}))
        assert result["passed"] is False
        assert any("failed to start" in a["detail"] for a in result["assertions"])

    def test_model_prompt_and_skill_dir_placeholders_substituted(self, tmp_path):
        code = "import sys; open('out.txt','w').write(sys.argv[1] + '|' + sys.argv[2])"
        skill_dir = _skill_dir(tmp_path, None)
        case = {
            "id": "case-1",
            "prompt": "Review the plan",
            "command": [PY, "-c", code, "{model}", "{prompt}|{skill_dir}"],
            "expect": {"files": [{"path": "out.txt", "contains": [SONNET_45, "Review the plan", str(skill_dir)]}]},
        }
        ws_root = tmp_path / "ws"
        ws_root.mkdir(exist_ok=True)
        assert run_case_trial(skill_dir, case, SONNET_45, timeout_seconds=30, workspace_root=ws_root)["passed"] is True

    def test_bundled_script_can_run_from_skill_dir_placeholder(self, tmp_path):
        skill_dir = _skill_dir(tmp_path, None)
        _write_fixture(skill_dir, "scripts/review.py", "open('review.md', 'w').write('## Findings')")
        ws_root = tmp_path / "ws"
        ws_root.mkdir(exist_ok=True)
        case = _case(
            [PY, "{skill_dir}/scripts/review.py"],
            {"exit_code": 0, "files": [{"path": "review.md", "contains": ["## Findings"]}]},
        )
        assert run_case_trial(skill_dir, case, SONNET_45, timeout_seconds=30, workspace_root=ws_root)["passed"] is True

    def test_fixtures_copied_into_isolated_workspace(self, tmp_path):
        case = _py_case(
            "content = open('plan.json').read(); open('echo.txt','w').write(content)",
            {"exit_code": 0, "files": [{"path": "echo.txt", "contains": ["aws_instance"]}]},
        )
        case["fixtures"] = "evals/fixtures/basic"
        result = self._run(tmp_path, case, fixtures={"evals/fixtures/basic/plan.json": '{"aws_instance": {}}'})
        assert result["passed"] is True

    def test_snapshot_forbidden_glob_and_exact(self, tmp_path):
        ws = tmp_path / "ws1"
        (ws / "sub").mkdir(parents=True)
        (ws / ".env").write_text("a", encoding="utf-8")
        (ws / "sub" / "x.tfstate").write_text("b", encoding="utf-8")
        (ws / "keep.md").write_text("c", encoding="utf-8")
        snap = snapshot_forbidden(ws, [".env", "*.tfstate", "sub/*.tfstate"])
        assert set(snap) == {".env", "sub/x.tfstate"}


class TestRunScoring:
    def _run_manifest(self, tmp_path, cases, **overrides):
        manifest = _manifest(cases=cases, **overrides)
        skill_dir = _skill_dir(tmp_path, manifest)
        return run_eval(skill_dir, manifest, "1.4.0")

    def test_record_has_r15_shape(self, tmp_path):
        run = self._run_manifest(tmp_path, [_py_case("raise SystemExit(0)", {"exit_code": 0})])
        record = run["records"][0]
        for field in (
            "run_id",
            "skill",
            "version",
            "model_id",
            "model_family",
            "harness",
            "harness_version",
            "verdict",
            "pass_rate",
            "case_results",
            "drift_vs_baseline",
            "artifacts_uri",
            "started_at",
        ):
            assert field in record
        assert record["skill"] == "terraform-review"
        assert record["version"] == "1.4.0"
        assert record["model_family"] in ("claude-sonnet-4-5", "claude-sonnet-5")
        assert len(run["eval_manifest_sha"]) == 64

    def test_all_pass_verdicts(self, tmp_path):
        run = self._run_manifest(tmp_path, [_py_case("raise SystemExit(0)", {"exit_code": 0})])
        assert [r["verdict"] for r in run["records"]] == ["pass", "pass"]
        assert all(r["pass_rate"] == 1.0 for r in run["records"])
        assert run["records"][1]["drift_vs_baseline"] == 0.0

    def test_model_conditional_failure_scores_drift(self, tmp_path):
        code = f"import os; raise SystemExit(0 if os.environ['GIP_EVAL_MODEL'] == '{SONNET_45}' else 1)"
        run = self._run_manifest(tmp_path, [_py_case(code, {"exit_code": 0})])
        baseline, candidate = run["records"]
        assert baseline["verdict"] == "pass"
        assert candidate["verdict"] == "drift"
        assert candidate["case_flips"] == ["case-1"]
        assert candidate["drift_vs_baseline"] == 1.0

    def test_failure_on_all_models_is_fail_not_drift(self, tmp_path):
        run = self._run_manifest(tmp_path, [_py_case("raise SystemExit(1)", {"exit_code": 0})])
        assert [r["verdict"] for r in run["records"]] == ["fail", "fail"]
        assert run["records"][1]["case_flips"] == []

    def test_live_cases_skipped_by_default(self, tmp_path):
        cases = [
            _py_case("raise SystemExit(0)", {"exit_code": 0}),
            {"id": "live-case", "live": True, "command": ["claude", "-p", "{prompt}"], "expect": {"exit_code": 0}},
        ]
        run = self._run_manifest(tmp_path, cases)
        record = run["records"][0]
        statuses = {c["case"]: c["status"] for c in record["case_results"]}
        assert statuses == {"case-1": "pass", "live-case": "skipped"}
        assert record["pass_rate"] == 1.0
        assert record["verdict"] == "pass"

    def test_all_cases_skipped_is_unverified(self, tmp_path):
        cases = [{"id": "live-case", "live": True, "command": ["claude"], "expect": {"exit_code": 0}}]
        run = self._run_manifest(tmp_path, cases)
        assert all(r["verdict"] == "unverified" for r in run["records"])
        assert all(r["pass_rate"] is None for r in run["records"])

    def test_pass_threshold_over_trials(self, tmp_path):
        run = self._run_manifest(
            tmp_path, [_py_case("raise SystemExit(1)", {"exit_code": 0})], trials=2, pass_threshold=0.5
        )
        assert run["records"][0]["verdict"] == "fail"
        run = self._run_manifest(
            tmp_path, [_py_case("raise SystemExit(0)", {"exit_code": 0})], trials=2, pass_threshold=0.5
        )
        assert run["records"][0]["verdict"] == "pass"
        assert run["records"][0]["case_results"][0]["pass_trials"] == 2

    def test_model_subset_run(self, tmp_path):
        manifest = _manifest(cases=[_py_case("raise SystemExit(0)", {"exit_code": 0})])
        skill_dir = _skill_dir(tmp_path, manifest)
        run = run_eval(skill_dir, manifest, None, models=[SONNET_5])
        assert [r["model_id"] for r in run["records"]] == [SONNET_5]
        assert run["records"][0]["drift_vs_baseline"] is None


class TestEvalCommand:
    def _execute(self, args: str, capsys=None):
        tester = CommandTester(SkillsEvalCommand())
        code = tester.execute(args)
        output = tester.io.fetch_output() + tester.io.fetch_error()
        if capsys is not None:
            output += capsys.readouterr().out
        return code, output

    def test_validate_only_ok(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, _manifest())
        code, output = self._execute(f"{skill_dir} --validate-only", capsys)
        assert code == 0
        assert "is valid" in output

    def test_validate_only_reports_schema_errors(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, _manifest(schema_version=99, models=["bogus-model"]))
        code, output = self._execute(f"{skill_dir} --validate-only", capsys)
        assert code == 1
        assert "Validation failed" in output
        assert "schema_version" in output
        assert "not in the models.py catalog" in output

    def test_missing_eval_yaml_explains(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, None)
        code, output = self._execute(str(skill_dir), capsys)
        assert code == 1
        assert "eval.yaml not found" in output

    def test_frontmatter_mismatch_fails(self, tmp_path, capsys):
        skill_md = SKILL_MD.replace("terraform-review", "other-name")
        skill_dir = _skill_dir(tmp_path, _manifest(), skill_md=skill_md)
        code, output = self._execute(f"{skill_dir} --validate-only", capsys)
        assert code == 1
        assert "does not match SKILL.md" in output

    def test_run_writes_result_record_and_passes(self, tmp_path, capsys):
        manifest = _manifest(cases=[_py_case("raise SystemExit(0)", {"exit_code": 0})])
        skill_dir = _skill_dir(tmp_path, manifest)
        (skill_dir / "_meta.json").write_text(
            json.dumps({"name": "terraform-review", "version": "1.4.0"}), encoding="utf-8"
        )
        code, _ = self._execute(str(skill_dir), capsys)
        assert code == 0
        results = list((skill_dir / "evals" / "results").glob("*.json"))
        assert len(results) == 1
        run = json.loads(results[0].read_text(encoding="utf-8"))
        assert run["skill"] == "terraform-review"
        assert run["version"] == "1.4.0"
        assert {r["model_id"] for r in run["records"]} == {SONNET_45, SONNET_5}
        assert all(r["verdict"] == "pass" for r in run["records"])

    def test_run_failure_returns_nonzero(self, tmp_path, capsys):
        manifest = _manifest(cases=[_py_case("raise SystemExit(1)", {"exit_code": 0})])
        skill_dir = _skill_dir(tmp_path, manifest)
        code, _ = self._execute(str(skill_dir), capsys)
        assert code == 1

    def test_model_option_filters_matrix(self, tmp_path, capsys):
        manifest = _manifest(cases=[_py_case("raise SystemExit(0)", {"exit_code": 0})])
        skill_dir = _skill_dir(tmp_path, manifest)
        code, _ = self._execute(f"{skill_dir} --model {SONNET_5}", capsys)
        assert code == 0
        run = json.loads(next((skill_dir / "evals" / "results").glob("*.json")).read_text(encoding="utf-8"))
        assert [r["model_id"] for r in run["records"]] == [SONNET_5]

    def test_model_option_must_be_in_manifest(self, tmp_path, capsys):
        skill_dir = _skill_dir(tmp_path, _manifest(cases=[_py_case("raise SystemExit(0)", {"exit_code": 0})]))
        code, output = self._execute(f"{skill_dir} --model anthropic.claude-opus-4-6-v1", capsys)
        assert code == 1
        assert "is not in eval.yaml models" in output

    def test_live_flag_executes_live_cases(self, tmp_path, capsys):
        cases = [_py_case("raise SystemExit(0)", {"exit_code": 0}, live=True)]
        skill_dir = _skill_dir(tmp_path, _manifest(cases=cases))
        code, output = self._execute(str(skill_dir), capsys)
        assert code == 0
        assert "skipped" in output
        code, _ = self._execute(f"{skill_dir} --live", capsys)
        assert code == 0
        runs = sorted((skill_dir / "evals" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
        live_run = json.loads(runs[-1].read_text(encoding="utf-8"))
        assert all(r["verdict"] == "pass" for r in live_run["records"])

    def test_results_dir_option(self, tmp_path, capsys):
        manifest = _manifest(cases=[_py_case("raise SystemExit(0)", {"exit_code": 0})])
        skill_dir = _skill_dir(tmp_path, manifest)
        out_dir = tmp_path / "records"
        code, _ = self._execute(f"{skill_dir} --results-dir {out_dir}", capsys)
        assert code == 0
        assert len(list(out_dir.glob("*.json"))) == 1

    def test_unknown_model_in_manifest_blocks_run(self, tmp_path, capsys):
        manifest = _manifest(models=["made.up.model"], baseline_model=None)
        manifest.pop("baseline_model")
        skill_dir = _skill_dir(tmp_path, manifest)
        code, output = self._execute(str(skill_dir), capsys)
        assert code == 1
        assert "not in the models.py catalog" in output

    def test_command_registered(self):
        from governed_inference_platform.cli import create_application

        app = create_application()
        assert app.find("skills eval") is not None

    def test_json_schema_missing_dependency_fails_closed(self, tmp_path):
        from governed_inference_platform.cli.commands.skill_eval import _assert_json_schema

        target = tmp_path / "findings.json"
        target.write_text("{}", encoding="utf-8")
        schema_path = tmp_path / "schema.json"
        schema_path.write_text("{}", encoding="utf-8")
        with patch.dict(sys.modules, {"jsonschema": None}):
            result = _assert_json_schema(target, schema_path, "findings.json")
        assert result["ok"] is False
        assert "jsonschema is not installed" in result["detail"]
