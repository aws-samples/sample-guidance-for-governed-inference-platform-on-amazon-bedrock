# ABOUTME: `gip skills eval` deterministic skill eval runner and eval.yaml schema validation.
# ABOUTME: Fixture-based hard assertions only; no live model calls by default.

"""Deterministic skill eval runner."""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from cleo.commands.command import Command
from cleo.helpers import argument, option
from rich.console import Console
from rich.table import Table

from governed_inference_platform.cli.commands.skills_cmd import (
    META_FILE,
    SEMVER_RE,
    SKILL_NAME_RE,
    parse_skill_frontmatter,
)
from governed_inference_platform.cli.utils.proc import run_checked
from governed_inference_platform.models import get_effective_models

EVAL_FILE = "eval.yaml"
EVAL_SCHEMA_VERSION = 1

HARNESSES = ("claude-code", "codex", "opencode")

CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

ALLOWED_MANIFEST_KEYS = {
    "schema_version",
    "skill",
    "harness",
    "timeout_seconds",
    "trials",
    "pass_threshold",
    "baseline_model",
    "models",
    "cases",
}
ALLOWED_CASE_KEYS = {"id", "prompt", "fixtures", "live", "command", "expect", "judge"}
ALLOWED_EXPECT_KEYS = {"exit_code", "files", "forbidden_paths"}
ALLOWED_FILE_KEYS = {"path", "must_exist", "contains", "json_schema"}
ALLOWED_JUDGE_KEYS = {"criterion", "min_score"}

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_TRIALS = 1
DEFAULT_PASS_THRESHOLD = 1.0

RESULTS_DIR = Path("evals") / "results"

_DATE_VERSION_RE = re.compile(r"-\d{8}(-v\d+(:\d+)?)?$")
_VERSION_RE = re.compile(r"-v\d+(:\d+)?$")


def model_family(model_id: str) -> str:
    """Collapse a model id to its family stem."""
    stem = model_id.split(".")[-1]
    stem = _DATE_VERSION_RE.sub("", stem)
    return _VERSION_RE.sub("", stem)


def known_model_ids() -> set[str]:
    """All model ids the models.py catalog knows: base ids and profile ids."""
    ids: set[str] = set()
    for model in get_effective_models().values():
        ids.add(model.base_model_id)
        for profile in model.profiles.values():
            ids.add(profile.model_id)
    return ids


def load_eval_manifest(path: Path) -> tuple[dict | None, list[str]]:
    """Parse eval.yaml. Returns (manifest, errors)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, [f"{EVAL_FILE}: cannot read: {e}"]
    try:
        manifest = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return None, [f"{EVAL_FILE}: invalid YAML: {e}"]
    if not isinstance(manifest, dict):
        return None, [f"{EVAL_FILE}: must be a YAML mapping"]
    return manifest, []


def _validate_case(i: int, case, skill_dir: Path | None) -> list[str]:
    errors: list[str] = []
    prefix = f"{EVAL_FILE}: cases[{i}]"
    if not isinstance(case, dict):
        return [f"{prefix} must be a mapping"]
    unknown = set(case) - ALLOWED_CASE_KEYS
    if unknown:
        errors.append(f"{prefix}: unknown keys {sorted(unknown)}")

    case_id = case.get("id")
    if not case_id or not isinstance(case_id, str):
        errors.append(f"{prefix}.id is required")
    elif not CASE_ID_RE.match(case_id):
        errors.append(f"{prefix}.id must match {CASE_ID_RE.pattern} (got '{case_id}')")

    if "prompt" in case and not isinstance(case["prompt"], str):
        errors.append(f"{prefix}.prompt must be a string")
    if "live" in case and not isinstance(case["live"], bool):
        errors.append(f"{prefix}.live must be a boolean")

    fixtures = case.get("fixtures")
    if fixtures is not None:
        if not isinstance(fixtures, str):
            errors.append(f"{prefix}.fixtures must be a path string")
        elif Path(fixtures).is_absolute() or ".." in Path(fixtures).parts:
            errors.append(f"{prefix}.fixtures must be relative to the skill directory (no '..')")
        elif skill_dir is not None and not (skill_dir / fixtures).is_dir():
            errors.append(f"{prefix}.fixtures directory not found: {fixtures}")

    command = case.get("command")
    if command is not None:
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            errors.append(f"{prefix}.command must be a non-empty list of strings (argv)")

    expect = case.get("expect")
    if not isinstance(expect, dict) or not expect:
        errors.append(f"{prefix}.expect is required (hard assertions)")
        expect = {}
    unknown_expect = set(expect) - ALLOWED_EXPECT_KEYS
    if unknown_expect:
        errors.append(
            f"{prefix}.expect: unknown keys {sorted(unknown_expect)} (v1 supports {sorted(ALLOWED_EXPECT_KEYS)})"
        )

    exit_code = expect.get("exit_code")
    if exit_code is not None:
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            errors.append(f"{prefix}.expect.exit_code must be an integer")
        if command is None:
            errors.append(f"{prefix}.expect.exit_code requires a command")

    files = expect.get("files", [])
    if not isinstance(files, list):
        errors.append(f"{prefix}.expect.files must be a list")
        files = []
    for j, spec in enumerate(files):
        fprefix = f"{prefix}.expect.files[{j}]"
        if not isinstance(spec, dict):
            errors.append(f"{fprefix} must be a mapping")
            continue
        unknown_file = set(spec) - ALLOWED_FILE_KEYS
        if unknown_file:
            errors.append(f"{fprefix}: unknown keys {sorted(unknown_file)}")
        path_value = spec.get("path")
        if not path_value or not isinstance(path_value, str):
            errors.append(f"{fprefix}.path is required")
        elif Path(path_value).is_absolute() or ".." in Path(path_value).parts:
            errors.append(f"{fprefix}.path must be relative to the workspace (no '..')")
        must_exist = spec.get("must_exist", True)
        if not isinstance(must_exist, bool):
            errors.append(f"{fprefix}.must_exist must be a boolean")
        contains = spec.get("contains")
        if contains is not None and (
            not isinstance(contains, list) or not all(isinstance(a, str) and a for a in contains)
        ):
            errors.append(f"{fprefix}.contains must be a list of non-empty strings")
        schema_ref = spec.get("json_schema")
        if schema_ref is not None:
            if not isinstance(schema_ref, str):
                errors.append(f"{fprefix}.json_schema must be a path string")
            elif Path(schema_ref).is_absolute() or ".." in Path(schema_ref).parts:
                errors.append(f"{fprefix}.json_schema must be relative to the skill directory (no '..')")
            elif skill_dir is not None and not (skill_dir / schema_ref).is_file():
                errors.append(f"{fprefix}.json_schema file not found: {schema_ref}")
        if must_exist is False and (contains or schema_ref):
            errors.append(f"{fprefix}: contains/json_schema make no sense with must_exist: false")

    forbidden = expect.get("forbidden_paths", [])
    if not isinstance(forbidden, list) or not all(isinstance(p, str) and p for p in forbidden):
        errors.append(f"{prefix}.expect.forbidden_paths must be a list of glob strings")
        forbidden = []
    else:
        for j, pattern in enumerate(forbidden):
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                errors.append(f"{prefix}.expect.forbidden_paths[{j}] must be relative to the workspace (no '..')")

    has_assertions = exit_code is not None or bool(files) or bool(forbidden)
    if expect and not has_assertions:
        errors.append(f"{prefix}.expect must include at least one assertion (exit_code, files, or forbidden_paths)")

    judge = case.get("judge")
    if judge is not None:
        if not isinstance(judge, list):
            errors.append(f"{prefix}.judge must be a list (recorded only; never gates in v1)")
        else:
            for j, criterion in enumerate(judge):
                jprefix = f"{prefix}.judge[{j}]"
                if not isinstance(criterion, dict):
                    errors.append(f"{jprefix} must be a mapping")
                    continue
                unknown_judge = set(criterion) - ALLOWED_JUDGE_KEYS
                if unknown_judge:
                    errors.append(f"{jprefix}: unknown keys {sorted(unknown_judge)}")
                if not criterion.get("criterion") or not isinstance(criterion.get("criterion"), str):
                    errors.append(f"{jprefix}.criterion is required")
                min_score = criterion.get("min_score")
                if min_score is not None and (isinstance(min_score, bool) or not isinstance(min_score, int | float)):
                    errors.append(f"{jprefix}.min_score must be a number")
    return errors


def validate_eval_manifest(
    manifest: dict,
    skill_dir: Path | None = None,
    frontmatter: dict | None = None,
    catalog_ids: set[str] | None = None,
) -> list[str]:
    """Validate an eval.yaml document."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"{EVAL_FILE}: must be a YAML mapping"]

    unknown = set(manifest) - ALLOWED_MANIFEST_KEYS
    if unknown:
        errors.append(f"{EVAL_FILE}: unknown keys {sorted(unknown)} (schema_version {EVAL_SCHEMA_VERSION})")

    if manifest.get("schema_version") != EVAL_SCHEMA_VERSION:
        errors.append(f"{EVAL_FILE}: schema_version must be {EVAL_SCHEMA_VERSION}")

    skill = manifest.get("skill")
    if not skill or not isinstance(skill, str):
        errors.append(f"{EVAL_FILE}: 'skill' is required")
    elif not SKILL_NAME_RE.match(skill):
        errors.append(f"{EVAL_FILE}: 'skill' must match {SKILL_NAME_RE.pattern} (got '{skill}')")
    elif frontmatter is not None and frontmatter.get("name") and frontmatter["name"] != skill:
        errors.append(f"{EVAL_FILE}: 'skill' ('{skill}') does not match SKILL.md frontmatter ('{frontmatter['name']}')")

    harness = manifest.get("harness", "claude-code")
    if harness not in HARNESSES:
        errors.append(f"{EVAL_FILE}: 'harness' must be one of {HARNESSES} (got '{harness}')")

    timeout = manifest.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        errors.append(f"{EVAL_FILE}: 'timeout_seconds' must be a positive integer")

    trials = manifest.get("trials", DEFAULT_TRIALS)
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        errors.append(f"{EVAL_FILE}: 'trials' must be an integer >= 1")

    threshold = manifest.get("pass_threshold", DEFAULT_PASS_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not 0 < threshold <= 1:
        errors.append(f"{EVAL_FILE}: 'pass_threshold' must be a fraction in (0, 1]")

    models = manifest.get("models")
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
        errors.append(f"{EVAL_FILE}: 'models' must be a non-empty list of model ids")
        models = []
    if len(models) != len(set(models)):
        errors.append(f"{EVAL_FILE}: 'models' contains duplicates")
    if catalog_ids is not None:
        for model_id in models:
            if model_id not in catalog_ids:
                errors.append(
                    f"{EVAL_FILE}: model '{model_id}' is not in the models.py catalog "
                    "(run 'gip models' to list known ids)"
                )

    baseline = manifest.get("baseline_model")
    if baseline is not None:
        if not isinstance(baseline, str):
            errors.append(f"{EVAL_FILE}: 'baseline_model' must be a model id string")
        elif models and baseline not in models:
            errors.append(f"{EVAL_FILE}: 'baseline_model' must be one of 'models' (drift is computed per run)")

    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{EVAL_FILE}: 'cases' must be a non-empty list")
        cases = []
    seen_ids: set[str] = set()
    for i, case in enumerate(cases):
        errors.extend(_validate_case(i, case, skill_dir))
        if isinstance(case, dict) and isinstance(case.get("id"), str):
            if case["id"] in seen_ids:
                errors.append(f"{EVAL_FILE}: duplicate case id '{case['id']}'")
            seen_ids.add(case["id"])
    return errors


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matching_paths(workspace: Path, patterns: list[str]) -> dict[str, Path]:
    """Map posix relpath to Path for every workspace file matching a pattern."""
    matches: dict[str, Path] = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(fnmatch.fnmatch(rel, pattern) or rel == pattern for pattern in patterns):
            matches[rel] = path
    return matches


def snapshot_forbidden(workspace: Path, patterns: list[str]) -> dict[str, str]:
    """sha256 of every file matching the forbidden patterns."""
    return {rel: _sha256_file(path) for rel, path in _matching_paths(workspace, patterns).items()}


def _substitute(argv: list[str], model_id: str, prompt: str, skill_dir: Path) -> list[str]:
    skill_dir_value = str(skill_dir)
    return [
        arg.replace("{model}", model_id).replace("{prompt}", prompt).replace("{skill_dir}", skill_dir_value)
        for arg in argv
    ]


def _assert_files(workspace: Path, skill_dir: Path, specs: list[dict]) -> list[dict]:
    results = []
    for spec in specs:
        rel = spec["path"]
        target = workspace / Path(*rel.split("/"))
        must_exist = spec.get("must_exist", True)
        if not must_exist:
            results.append(
                {
                    "assertion": f"file:{rel}:absent",
                    "ok": not target.exists(),
                    "detail": "" if not target.exists() else "file exists",
                }
            )
            continue
        if not target.is_file():
            results.append({"assertion": f"file:{rel}:exists", "ok": False, "detail": "file not found"})
            continue
        results.append({"assertion": f"file:{rel}:exists", "ok": True, "detail": ""})
        text = None
        for anchor in spec.get("contains") or []:
            if text is None:
                text = target.read_text(encoding="utf-8", errors="replace")
            ok = anchor in text
            results.append(
                {"assertion": f"file:{rel}:contains:{anchor}", "ok": ok, "detail": "" if ok else "anchor not found"}
            )
        schema_ref = spec.get("json_schema")
        if schema_ref:
            results.append(_assert_json_schema(target, skill_dir / schema_ref, rel))
    return results


def _assert_json_schema(target: Path, schema_path: Path, rel: str) -> dict:
    assertion = f"file:{rel}:json_schema"
    try:
        import jsonschema
    except ImportError:
        return {"assertion": assertion, "ok": False, "detail": "jsonschema is not installed (pip install jsonschema)"}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"assertion": assertion, "ok": False, "detail": f"target is not valid JSON: {e}"}
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"assertion": assertion, "ok": False, "detail": f"schema is not valid JSON: {e}"}
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError as e:
        return {"assertion": assertion, "ok": False, "detail": f"schema violation: {e.message}"}
    return {"assertion": assertion, "ok": True, "detail": ""}


def run_case_trial(
    skill_dir: Path,
    case: dict,
    model_id: str,
    timeout_seconds: int,
    workspace_root: Path,
) -> dict:
    """Run one trial of one case in a fresh isolated workspace."""
    workspace = workspace_root / f"{case['id']}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    fixtures = case.get("fixtures")
    if fixtures:
        shutil.copytree(skill_dir / fixtures, workspace, dirs_exist_ok=True)

    expect = case["expect"]
    forbidden = expect.get("forbidden_paths") or []
    before = snapshot_forbidden(workspace, forbidden)

    assertions: list[dict] = []
    command = case.get("command")
    if command:
        argv = _substitute(command, model_id, case.get("prompt") or "", skill_dir)
        env = dict(os.environ)
        env.update(
            {
                "GIP_EVAL_MODEL": model_id,
                "GIP_EVAL_PROMPT": case.get("prompt") or "",
                "GIP_EVAL_SKILL_DIR": str(skill_dir),
            }
        )
        try:
            completed = run_checked(  # nosec B603 — operator-authored eval command, no shell
                argv,
                cwd=workspace,
                env=env,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            returncode: int | None = completed.returncode
            command_detail = ""
        except subprocess.TimeoutExpired:
            returncode = None
            command_detail = f"timed out after {timeout_seconds}s"
        except OSError as e:
            returncode = None
            command_detail = f"command failed to start: {e}"
        expected_exit = expect.get("exit_code")
        if returncode is None:
            assertions.append({"assertion": "exit_code", "ok": False, "detail": command_detail})
        elif expected_exit is not None:
            ok = returncode == expected_exit
            assertions.append(
                {
                    "assertion": "exit_code",
                    "ok": ok,
                    "detail": "" if ok else f"expected {expected_exit}, got {returncode}",
                }
            )

    assertions.extend(_assert_files(workspace, skill_dir, expect.get("files") or []))

    if forbidden:
        after = snapshot_forbidden(workspace, forbidden)
        ok = before == after
        detail = ""
        if not ok:
            changed = sorted(
                (set(before) ^ set(after)) | {k for k in before.keys() & after.keys() if before[k] != after[k]}
            )
            detail = f"forbidden paths touched: {changed}"
        assertions.append({"assertion": "forbidden_paths", "ok": ok, "detail": detail})

    if not assertions:
        assertions.append({"assertion": "hard_assertions", "ok": False, "detail": "case produced no assertions"})

    return {"passed": all(a["ok"] for a in assertions), "assertions": assertions}


def run_eval(
    skill_dir: Path,
    manifest: dict,
    skill_version: str | None,
    models: list[str] | None = None,
    live: bool = False,
) -> dict:
    """Execute the eval matrix and return the run document."""
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    manifest_sha = hashlib.sha256((skill_dir / EVAL_FILE).read_bytes()).hexdigest()
    timeout_seconds = manifest.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    trials = manifest.get("trials", DEFAULT_TRIALS)
    threshold = manifest.get("pass_threshold", DEFAULT_PASS_THRESHOLD)
    baseline = manifest.get("baseline_model")
    matrix = models if models is not None else manifest["models"]

    records = []
    case_pass: dict[str, dict[str, bool]] = {}
    with tempfile.TemporaryDirectory(prefix="gip-skill-eval-") as tmp:
        workspace_root = Path(tmp)
        for model_id in matrix:
            case_results = []
            case_pass[model_id] = {}
            for case in manifest["cases"]:
                if case.get("live") and not live:
                    case_results.append(
                        {
                            "case": case["id"],
                            "status": "skipped",
                            "reason": "live case (re-run with --live)",
                            "trials": [],
                        }
                    )
                    continue
                trial_results = [
                    run_case_trial(skill_dir, case, model_id, timeout_seconds, workspace_root) for _ in range(trials)
                ]
                passed_trials = sum(1 for t in trial_results if t["passed"])
                passed = passed_trials / trials >= threshold
                case_pass[model_id][case["id"]] = passed
                case_results.append(
                    {
                        "case": case["id"],
                        "status": "pass" if passed else "fail",
                        "pass_trials": passed_trials,
                        "trials": trial_results,
                    }
                )
            evaluated = case_pass[model_id]
            pass_rate = (sum(evaluated.values()) / len(evaluated)) if evaluated else None
            records.append(
                {
                    "run_id": run_id,
                    "skill": manifest["skill"],
                    "version": skill_version,
                    "model_id": model_id,
                    "model_family": model_family(model_id),
                    "harness": manifest.get("harness", "claude-code"),
                    "harness_version": None,
                    "pass_rate": pass_rate,
                    "case_results": case_results,
                    "artifacts_uri": None,
                    "started_at": started_at,
                }
            )

    baseline_pass = case_pass.get(baseline, {}) if baseline else {}
    for record in records:
        model_id = record["model_id"]
        drift = None
        flips: list[str] = []
        if baseline and baseline in case_pass and record["pass_rate"] is not None and case_pass[baseline]:
            baseline_rate = sum(baseline_pass.values()) / len(baseline_pass)
            drift = round(baseline_rate - record["pass_rate"], 6)
            flips = sorted(
                case_id
                for case_id, passed in baseline_pass.items()
                if passed and case_pass[model_id].get(case_id) is False
            )
        record["drift_vs_baseline"] = drift
        record["case_flips"] = flips
        if record["pass_rate"] is None:
            record["verdict"] = "unverified"
        elif record["pass_rate"] == 1.0:
            record["verdict"] = "pass"
        elif flips or (drift is not None and drift > 0.1):
            record["verdict"] = "drift"
        else:
            record["verdict"] = "fail"

    return {
        "run_id": run_id,
        "skill": manifest["skill"],
        "version": skill_version,
        "eval_manifest_sha": manifest_sha,
        "baseline_model": baseline,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "records": records,
    }


class SkillsEvalCommand(Command):
    name = "skills eval"
    description = "Run the deterministic eval suite (eval.yaml) for a skill directory"

    arguments = [argument("directory", description="Skill directory containing SKILL.md and eval.yaml")]
    options = [
        option("validate-only", description="Validate eval.yaml and exit without running cases", flag=True),
        option(
            "model",
            description="Run only this model id (repeatable; must be in eval.yaml models)",
            flag=False,
            multiple=True,
        ),
        option("live", description="Also run cases marked 'live: true' (may call real harnesses/models)", flag=True),
        option(
            "results-dir",
            description="Directory for result records (default: <skill>/evals/results)",
            flag=False,
            default=None,
        ),
    ]

    def handle(self) -> int:
        console = Console()
        skill_dir = Path(self.argument("directory")).expanduser()
        if not skill_dir.is_dir():
            console.print(f"[red]Not a directory: {skill_dir}[/red]")
            return 1
        skill_md_path = skill_dir / "SKILL.md"
        eval_path = skill_dir / EVAL_FILE
        if not skill_md_path.is_file():
            console.print(f"[red]SKILL.md not found in {skill_dir}[/red]")
            return 1
        if not eval_path.is_file():
            console.print(f"[red]{EVAL_FILE} not found in {skill_dir} (see assets/docs/SKILLS_REGISTRY.md)[/red]")
            return 1

        frontmatter, fm_errors = parse_skill_frontmatter(skill_md_path.read_text(encoding="utf-8"))
        manifest, errors = load_eval_manifest(eval_path)
        errors = fm_errors + errors
        if manifest is not None:
            errors.extend(
                validate_eval_manifest(
                    manifest,
                    skill_dir=skill_dir,
                    frontmatter=frontmatter,
                    catalog_ids=known_model_ids(),
                )
            )
        if errors:
            console.print("[red]Validation failed:[/red]")
            for e in errors:
                console.print(f"  [red]x[/red] {e}")
            return 1
        if self.option("validate-only"):
            console.print(
                f"[green]OK[/green] {EVAL_FILE} is valid ({len(manifest['cases'])} case(s), {len(manifest['models'])} model(s))"
            )
            return 0

        selected = list(self.option("model") or [])
        for model_id in selected:
            if model_id not in manifest["models"]:
                console.print(f"[red]--model '{model_id}' is not in {EVAL_FILE} models[/red]")
                return 1
        matrix = list(dict.fromkeys(selected)) or None

        skill_version = self._read_version(skill_dir, console)
        run = run_eval(skill_dir, manifest, skill_version, models=matrix, live=self.option("live"))

        results_dir = (
            Path(self.option("results-dir")).expanduser() if self.option("results-dir") else skill_dir / RESULTS_DIR
        )
        results_dir.mkdir(parents=True, exist_ok=True)
        results_path = results_dir / f"{run['run_id']}.json"
        results_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

        table = Table(title=f"Skill eval: {run['skill']}@{skill_version or 'unpublished'} (run {run['run_id'][:8]})")
        table.add_column("Model", style="cyan")
        table.add_column("Family")
        table.add_column("Verdict")
        table.add_column("Pass rate")
        table.add_column("Drift")
        table.add_column("Flips")
        for record in run["records"]:
            verdict = record["verdict"]
            style = {"pass": "green", "drift": "yellow"}.get(verdict, "red" if verdict == "fail" else "dim")
            rate = "-" if record["pass_rate"] is None else f"{record['pass_rate']:.2f}"
            drift = "-" if record["drift_vs_baseline"] is None else f"{record['drift_vs_baseline']:+.2f}"
            table.add_row(
                record["model_id"],
                record["model_family"],
                f"[{style}]{verdict}[/{style}]",
                rate,
                drift,
                ", ".join(record["case_flips"]) or "-",
            )
        console.print(table)
        skipped = sum(1 for r in run["records"] for c in r["case_results"] if c["status"] == "skipped")
        if skipped:
            console.print(f"[yellow]{skipped} live case run(s) skipped. Re-run with --live to execute them.[/yellow]")
        console.print(f"[dim]Result records: {results_path}[/dim]")

        return 0 if all(r["verdict"] in ("pass", "unverified") for r in run["records"]) else 1

    @staticmethod
    def _read_version(skill_dir: Path, console: Console) -> str | None:
        """Skill version from _meta.json, if present."""
        meta_path = skill_dir / META_FILE
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            console.print(f"[yellow]{META_FILE} is not valid JSON. Recording version as unpublished.[/yellow]")
            return None
        version = meta.get("version")
        return version if isinstance(version, str) and SEMVER_RE.match(version) else None
