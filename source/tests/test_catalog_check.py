# ABOUTME: Fixture-based tests for catalog drift detection (catalog_check.py + gip models check)
# ABOUTME: No live AWS calls — API responses are synthesized from the catalog itself

"""Tests for model-catalog drift detection against Bedrock API responses.

Fixtures are raw API-response-shaped dicts (ListFoundationModels /
ListInferenceProfiles) built from CLAUDE_MODELS itself, so an unmodified
fixture is in sync with the catalog by construction. Each drift case then
perturbs the fixture.
"""

import json
import sys
from pathlib import Path

import pytest

from tests.support.cli import CliAppTester

sys.path.insert(0, str(Path(__file__).parent.parent))

from governed_inference_platform.catalog_check import (
    base_model_available,
    build_drift_report,
    extract_catalog,
    parse_foundation_model_summaries,
    parse_inference_profile_summaries,
)
from governed_inference_platform.cli import create_application
from governed_inference_platform.models import CLAUDE_MODELS

CHECK_REGION = "us-east-1"

NEW_MODEL_SUMMARY = {
    "modelId": "anthropic.claude-example-9",
    "modelName": "Claude Example 9",
    "providerName": "Anthropic",
    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
    "modelLifecycle": {"status": "ACTIVE"},
}

LEGACY_MODEL_SUMMARY = {
    "modelId": "anthropic.claude-instant-v1",
    "modelName": "Claude Instant",
    "providerName": "Anthropic",
    "inferenceTypesSupported": ["ON_DEMAND"],
    "modelLifecycle": {"status": "LEGACY"},
}

NEW_PROFILE_SUMMARY = {
    "inferenceProfileId": "us.anthropic.claude-example-9",
    "inferenceProfileName": "US Claude Example 9",
    "models": [
        {"modelArn": f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-example-9"}
        for r in ["us-east-1", "us-east-2", "us-west-2"]
    ],
}


def make_live_fixtures(regions=(CHECK_REGION,)):
    """Build raw API responses that exactly mirror the catalog for the given regions.

    Returns (foundation_models_response_by_region, inference_profiles_response).
    """
    catalog = extract_catalog(CLAUDE_MODELS)
    regions = set(regions)

    fm_responses = {}
    for region in regions:
        summaries = []
        for base_id in sorted(catalog.base_model_ids):
            if "gov" in base_id:
                continue
            if region in catalog.base_destination_regions.get(base_id, set()):
                summaries.append(
                    {
                        "modelId": base_id,
                        "modelName": base_id,
                        "providerName": "Anthropic",
                        "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                        "modelLifecycle": {"status": "ACTIVE"},
                    }
                )
        fm_responses[region] = {"modelSummaries": summaries}

    profile_summaries = []
    for pid in sorted(catalog.profiles):
        prof = catalog.profiles[pid]
        if "gov" in pid or not (set(prof.source_regions) & regions):
            continue
        dest = [r for r in sorted(prof.destination_regions) if not r.startswith("all-")]
        profile_summaries.append(
            {
                "inferenceProfileId": pid,
                "inferenceProfileName": pid,
                "models": [{"modelArn": f"arn:aws:bedrock:{r}::foundation-model/{pid}"} for r in dest],
            }
        )
    return fm_responses, {"inferenceProfileSummaries": profile_summaries}


def build_report_from_fixtures(fm_responses, profiles_response, regions=(CHECK_REGION,)):
    """Parse raw fixtures and build a DriftReport (the same path the command uses)."""
    catalog = extract_catalog(CLAUDE_MODELS)
    live_models_by_region = {
        region: parse_foundation_model_summaries(resp.get("modelSummaries", []))
        for region, resp in fm_responses.items()
    }
    live_profiles = parse_inference_profile_summaries(profiles_response.get("inferenceProfileSummaries", []))
    return build_drift_report(catalog, live_models_by_region, live_profiles, list(regions))


class TestParsing:
    """Raw API responses are parsed into normalized structures."""

    def test_parse_foundation_models_filters_non_claude(self):
        summaries = [NEW_MODEL_SUMMARY, {"modelId": "amazon.titan-text-express-v1"}]
        parsed = parse_foundation_model_summaries(summaries)
        assert [m["model_id"] for m in parsed] == ["anthropic.claude-example-9"]
        assert parsed[0]["inference_types"] == ["INFERENCE_PROFILE"]
        assert parsed[0]["lifecycle_status"] == "ACTIVE"

    def test_parse_inference_profiles_extracts_regions_from_arns(self):
        parsed = parse_inference_profile_summaries([NEW_PROFILE_SUMMARY])
        assert parsed["us.anthropic.claude-example-9"]["regions"] == ["us-east-1", "us-east-2", "us-west-2"]

    def test_parse_inference_profiles_filters_non_anthropic(self):
        parsed = parse_inference_profile_summaries([{"inferenceProfileId": "us.meta.llama3-1-70b", "models": []}])
        assert parsed == {}

    def test_base_model_available_matches_context_window_variants(self):
        assert base_model_available("anthropic.claude-example-9", ["anthropic.claude-example-9:0:200k"])
        assert not base_model_available("anthropic.claude-example-9", ["anthropic.claude-example-90"])


class TestDriftDetection:
    """Drift report from fixture API responses."""

    def test_in_sync(self):
        fm, profiles = make_live_fixtures()
        report = build_report_from_fixtures(fm, profiles)
        assert report.in_sync
        assert report.foundation_models.missing_from_catalog == {}
        assert report.profiles.missing_from_catalog == []
        assert report.profiles.region_drift == {}

    def test_live_has_new_anthropic_model(self):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        report = build_report_from_fixtures(fm, profiles)
        assert not report.in_sync
        assert "anthropic.claude-example-9" in report.foundation_models.missing_from_catalog
        entry = report.foundation_models.missing_from_catalog["anthropic.claude-example-9"]
        assert entry["regions"] == [CHECK_REGION]

    def test_live_has_new_cris_profile(self):
        fm, profiles = make_live_fixtures()
        profiles["inferenceProfileSummaries"].append(NEW_PROFILE_SUMMARY)
        report = build_report_from_fixtures(fm, profiles)
        assert not report.in_sync
        assert ("us.anthropic.claude-example-9", "US Claude Example 9") in report.profiles.missing_from_catalog

    def test_legacy_on_demand_model_is_informational_not_drift(self):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(LEGACY_MODEL_SUMMARY)
        report = build_report_from_fixtures(fm, profiles)
        assert report.in_sync
        assert "anthropic.claude-instant-v1" in report.foundation_models.legacy_not_in_catalog
        assert any("claude-instant-v1" in note for note in report.notes)

    def test_catalog_model_removed_from_live(self):
        fm, profiles = make_live_fixtures()
        removed = fm[CHECK_REGION]["modelSummaries"].pop()
        report = build_report_from_fixtures(fm, profiles)
        assert not report.in_sync
        assert removed["modelId"] in report.foundation_models.not_available_live

    def test_catalog_profile_removed_from_live(self):
        fm, profiles = make_live_fixtures()
        removed = profiles["inferenceProfileSummaries"].pop()
        report = build_report_from_fixtures(fm, profiles)
        assert not report.in_sync
        assert removed["inferenceProfileId"] in report.profiles.not_available_live

    def test_cris_destination_region_drift(self):
        fm, profiles = make_live_fixtures()
        target = next(
            p
            for p in profiles["inferenceProfileSummaries"]
            if p["models"] and not p["inferenceProfileId"].startswith("global.")
        )
        target["models"].append(
            {"modelArn": f"arn:aws:bedrock:mx-central-1::foundation-model/{target['inferenceProfileId']}"}
        )
        report = build_report_from_fixtures(fm, profiles)
        assert not report.in_sync
        assert report.profiles.region_drift[target["inferenceProfileId"]]["live_only"] == ["mx-central-1"]

    def test_profiles_outside_checked_geography_are_skipped_not_removed(self):
        """Checking only us-east-1 must not flag EU/APAC catalog profiles as removed."""
        fm, profiles = make_live_fixtures()
        report = build_report_from_fixtures(fm, profiles)
        assert report.profiles.not_available_live == []


class TestCommandExitCodesAndJson:
    """`gip models check` wired to fixture data via a patched fetcher."""

    @pytest.fixture
    def app_tester(self):
        return CliAppTester(create_application())

    def _patch_fetch(self, monkeypatch, fm_responses, profiles_response):
        from governed_inference_platform.cli.commands import models_cmd

        def fake_fetch(regions):
            live_models = {
                r: parse_foundation_model_summaries(fm_responses.get(r, {}).get("modelSummaries", [])) for r in regions
            }
            live_profiles = parse_inference_profile_summaries(profiles_response.get("inferenceProfileSummaries", []))
            return live_models, live_profiles

        monkeypatch.setattr(models_cmd, "fetch_live_data", fake_fetch)

    def test_in_sync_exits_zero(self, app_tester, monkeypatch):
        fm, profiles = make_live_fixtures()
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION}") == 0

    def test_drift_exits_one(self, app_tester, monkeypatch, capsys):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION}") == 1
        assert "anthropic.claude-example-9" in capsys.readouterr().out

    def test_json_output_shape(self, app_tester, monkeypatch):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        exit_code = app_tester.run(f"models check --region {CHECK_REGION} --json")
        assert exit_code == 1
        payload = json.loads(app_tester.io.fetch_output())
        assert payload["in_sync"] is False
        assert payload["regions_checked"] == [CHECK_REGION]
        assert {"missing_from_catalog", "removed_from_live", "region_drift", "notes"} <= set(payload)
        assert {"models", "profiles"} <= set(payload["missing_from_catalog"])
        assert {"models", "profiles"} <= set(payload["removed_from_live"])
        model_ids = [m["model_id"] for m in payload["missing_from_catalog"]["models"]]
        assert "anthropic.claude-example-9" in model_ids

    def test_json_in_sync_shape(self, app_tester, monkeypatch):
        fm, profiles = make_live_fixtures()
        self._patch_fetch(monkeypatch, fm, profiles)
        exit_code = app_tester.run(f"models check --region {CHECK_REGION} --json")
        assert exit_code == 0
        payload = json.loads(app_tester.io.fetch_output())
        assert payload["in_sync"] is True
        assert payload["missing_from_catalog"]["models"] == []

    def test_fetch_error_exits_two_with_actionable_message(self, app_tester, monkeypatch, capsys):
        from governed_inference_platform.cli.commands import models_cmd

        def failing_fetch(regions):
            raise models_cmd.CatalogCheckError("No AWS credentials found. Run 'aws sso login' and retry.")

        monkeypatch.setattr(models_cmd, "fetch_live_data", failing_fetch)
        assert app_tester.run(f"models check --region {CHECK_REGION}") == 2
        assert "credentials" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Lifecycle warnings + --propose (R14 / E-S2)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from governed_inference_platform.catalog_check import (  # noqa: E402
    build_proposals,
    collect_lifecycle_warnings,
    render_models_py_snippet,
    render_overlay_json,
)

# R14 §1.1 live-verified examples (us-east-1, 2026-07-08):
# sonnet-4 hits provider-set premium pricing on 2026-07-14; opus-4-1 went
# Legacy on 2026-07-08. Both are catalog models.
SONNET_4_LEGACY_SUMMARY = {
    "modelId": "anthropic.claude-sonnet-4-20250514-v1:0",
    "modelName": "Claude Sonnet 4",
    "providerName": "Anthropic",
    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
    "modelLifecycle": {
        "status": "LEGACY",
        "legacyTime": "2026-04-14T00:00:00Z",
        "publicExtendedAccessTime": "2026-07-14T00:00:00Z",
        "endOfLifeTime": "2026-10-14T00:00:00Z",
    },
}

OPUS_4_1_LEGACY_SUMMARY = {
    "modelId": "anthropic.claude-opus-4-1-20250805-v1:0",
    "modelName": "Claude Opus 4.1",
    "providerName": "Anthropic",
    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
    "modelLifecycle": {
        "status": "LEGACY",
        "legacyTime": "2026-07-08T00:00:00Z",
        "publicExtendedAccessTime": "2026-10-08T00:00:00Z",
        "endOfLifeTime": "2027-01-08T00:00:00Z",
    },
}

# R14 §1.1: publicExtendedAccessTime is optional even for LEGACY models
# (pre-Feb-2026 policy / provider discretion) — like claude-3-sonnet live.
LEGACY_NO_PREMIUM_SUMMARY = {
    "modelId": "anthropic.claude-opus-4-1-20250805-v1:0",
    "modelName": "Claude Opus 4.1",
    "providerName": "Anthropic",
    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
    "modelLifecycle": {
        "status": "LEGACY",
        "legacyTime": "2026-01-30T00:00:00Z",
        "endOfLifeTime": "2026-07-30T00:00:00Z",
    },
}

NOW = datetime(2026, 7, 9, tzinfo=timezone.utc)


def _replace_summary(fm, summary):
    """Swap the fixture's ACTIVE record for the given model with a perturbed one."""
    fm[CHECK_REGION]["modelSummaries"] = [
        m for m in fm[CHECK_REGION]["modelSummaries"] if m["modelId"] != summary["modelId"]
    ] + [summary]


class TestLifecycleDateParsing:
    """parse_foundation_model_summaries surfaces the three lifecycle dates (R14 §1.1)."""

    def test_dates_extracted_for_legacy_model(self):
        parsed = parse_foundation_model_summaries([SONNET_4_LEGACY_SUMMARY])
        assert parsed[0]["lifecycle_status"] == "LEGACY"
        assert parsed[0]["legacy_time"] == "2026-04-14T00:00:00Z"
        assert parsed[0]["public_extended_access_time"] == "2026-07-14T00:00:00Z"
        assert parsed[0]["end_of_life_time"] == "2026-10-14T00:00:00Z"

    def test_dates_none_for_active_model(self):
        parsed = parse_foundation_model_summaries([NEW_MODEL_SUMMARY])
        assert parsed[0]["legacy_time"] is None
        assert parsed[0]["public_extended_access_time"] is None
        assert parsed[0]["end_of_life_time"] is None

    def test_datetime_objects_normalized_to_iso(self):
        summary = dict(SONNET_4_LEGACY_SUMMARY)
        summary["modelLifecycle"] = {
            "status": "LEGACY",
            "legacyTime": datetime(2026, 4, 14, tzinfo=timezone.utc),
        }
        parsed = parse_foundation_model_summaries([summary])
        assert parsed[0]["legacy_time"] == "2026-04-14T00:00:00+00:00"


class TestLifecycleWarnings:
    """collect_lifecycle_warnings joins live LEGACY status onto catalog models."""

    def test_sonnet_4_premium_imminent(self):
        """R14 real case: sonnet-4 premium starts 2026-07-14 (5 days out at NOW)."""
        fm, profiles = make_live_fixtures()
        _replace_summary(fm, SONNET_4_LEGACY_SUMMARY)
        report = build_report_from_fixtures(fm, profiles)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        warnings = collect_lifecycle_warnings(catalog, live, now=NOW)
        assert len(warnings) == 1
        w = warnings[0]
        assert w["model_id"] == "anthropic.claude-sonnet-4-20250514-v1:0"
        assert w["severity"] == "warning"
        assert any("premium pricing begins 2026-07-14" in m for m in w["messages"])
        assert any("end of life 2026-10-14" in m for m in w["messages"])
        # Lifecycle warnings are informational — drift status is unaffected.
        assert report.in_sync

    def test_opus_4_1_legacy_today(self):
        """R14 real case: opus-4-1 entered Legacy 2026-07-08; premium is >30d out."""
        fm, _profiles = make_live_fixtures()
        _replace_summary(fm, OPUS_4_1_LEGACY_SUMMARY)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        warnings = collect_lifecycle_warnings(catalog, live, now=NOW)
        assert len(warnings) == 1
        w = warnings[0]
        assert w["severity"] == "warning"
        assert any("entered Legacy on 2026-07-08" in m for m in w["messages"])
        assert not any("premium" in m for m in w["messages"])  # 2026-10-08 is >30d away

    def test_premium_active_message(self):
        fm, _profiles = make_live_fixtures()
        _replace_summary(fm, SONNET_4_LEGACY_SUMMARY)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        warnings = collect_lifecycle_warnings(catalog, live, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        assert any("premium pricing ACTIVE since 2026-07-14" in m for m in warnings[0]["messages"])

    def test_eol_within_60_days_is_critical(self):
        fm, _profiles = make_live_fixtures()
        _replace_summary(fm, SONNET_4_LEGACY_SUMMARY)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        warnings = collect_lifecycle_warnings(catalog, live, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert warnings[0]["severity"] == "critical"
        assert any("inference FAILS after 2026-10-14" in m for m in warnings[0]["messages"])

    def test_missing_premium_date_tolerated(self):
        """publicExtendedAccessTime is optional (R14 §1.1) — parser must not require it."""
        fm, _profiles = make_live_fixtures()
        _replace_summary(fm, LEGACY_NO_PREMIUM_SUMMARY)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        warnings = collect_lifecycle_warnings(catalog, live, now=NOW)
        assert len(warnings) == 1
        assert warnings[0]["public_extended_access_time"] is None
        assert warnings[0]["severity"] == "critical"  # EOL 2026-07-30 is <60d from NOW

    def test_uncatalogued_legacy_model_not_warned(self):
        """Uncatalogued legacy models stay in legacy_not_in_catalog notes, not warnings."""
        fm, _profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(LEGACY_MODEL_SUMMARY)
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        assert collect_lifecycle_warnings(catalog, live, now=NOW) == []

    def test_all_active_no_warnings(self):
        fm, _profiles = make_live_fixtures()
        catalog = extract_catalog(CLAUDE_MODELS)
        live = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        assert collect_lifecycle_warnings(catalog, live, now=NOW) == []

    def test_warnings_in_drift_report_dict(self):
        fm, profiles = make_live_fixtures()
        _replace_summary(fm, SONNET_4_LEGACY_SUMMARY)
        report = build_report_from_fixtures(fm, profiles)
        payload = report.to_dict()
        assert "lifecycle_warnings" in payload
        assert payload["lifecycle_warnings"][0]["model_id"] == "anthropic.claude-sonnet-4-20250514-v1:0"


class TestProposals:
    """--propose synthesis: drift → ready-to-paste models.py entries + overlay JSON."""

    def _drifted_fixtures(self):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        profiles["inferenceProfileSummaries"].append(NEW_PROFILE_SUMMARY)
        return fm, profiles

    def _build(self, fm, profiles):
        catalog = extract_catalog(CLAUDE_MODELS)
        live_models = {r: parse_foundation_model_summaries(resp["modelSummaries"]) for r, resp in fm.items()}
        live_profiles = parse_inference_profile_summaries(profiles["inferenceProfileSummaries"])
        for info in live_profiles.values():
            info.setdefault("seen_in_regions", [CHECK_REGION])
        report = build_drift_report(catalog, live_models, live_profiles, [CHECK_REGION])
        return catalog, report, live_profiles

    def test_new_model_proposal_with_profiles(self):
        catalog, report, live_profiles = self._build(*self._drifted_fixtures())
        proposals = build_proposals(catalog, report, live_profiles, [CHECK_REGION])
        new_models = [p for p in proposals if p["kind"] == "new_model"]
        assert len(new_models) == 1
        prop = new_models[0]
        assert prop["model_key"] == "example-9"
        assert prop["base_model_id"] == "anthropic.claude-example-9"
        assert "us" in prop["profiles"]
        us = prop["profiles"]["us"]
        assert us["model_id"] == "us.anthropic.claude-example-9"
        assert us["destination_regions"] == ["us-east-1", "us-east-2", "us-west-2"]
        assert us["source_regions"] == [CHECK_REGION]

    def test_no_drift_no_proposals(self):
        fm, profiles = make_live_fixtures()
        catalog, report, live_profiles = self._build(fm, profiles)
        assert build_proposals(catalog, report, live_profiles, [CHECK_REGION]) == []

    def test_models_py_snippet_is_pasteable(self):
        catalog, report, live_profiles = self._build(*self._drifted_fixtures())
        proposals = build_proposals(catalog, report, live_profiles, [CHECK_REGION])
        snippet = render_models_py_snippet(proposals)
        assert '"example-9": {' in snippet
        assert '"base_model_id": "anthropic.claude-example-9",' in snippet
        assert '"model_id": "us.anthropic.claude-example-9",' in snippet
        # Human-review TODOs are mandatory (proposals never self-place into tiers).
        assert "TODO(review): tier placement" in snippet
        assert "ADR-0018" in snippet

    def test_overlay_json_validates_against_schema(self):
        """The rendered extra_models block must pass the overlay's own strict validation."""
        from governed_inference_platform.models import validate_extra_models

        catalog, report, live_profiles = self._build(*self._drifted_fixtures())
        proposals = build_proposals(catalog, report, live_profiles, [CHECK_REGION])
        overlay = render_overlay_json(proposals)
        assert set(overlay) == {"extra_models"}
        assert validate_extra_models(overlay["extra_models"]) == []

    def test_overlay_excludes_new_profile_proposals(self):
        """Additive-only: a new profile on an existing catalog entry can't be overlaid."""
        overlay = render_overlay_json(
            [{"kind": "new_profile", "model_key": "sonnet-4-5", "name": "x", "base_model_id": "y", "profiles": {}}]
        )
        assert overlay == {"extra_models": {}}

    def test_empty_proposals_render_noop_snippet(self):
        assert "No proposals" in render_models_py_snippet([])


class TestProposeCommand(TestCommandExitCodesAndJson):
    """`gip models check --propose` CLI surface."""

    def test_propose_prints_snippet_on_drift(self, app_tester, monkeypatch, capsys):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        profiles["inferenceProfileSummaries"].append(NEW_PROFILE_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION} --propose") == 1
        out = capsys.readouterr().out + app_tester.io.fetch_output()
        assert '"example-9": {' in out
        assert "extra_models" in out

    def test_propose_in_sync_reports_nothing_to_propose(self, app_tester, monkeypatch, capsys):
        fm, profiles = make_live_fixtures()
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION} --propose") == 0
        assert "nothing to propose" in capsys.readouterr().out.lower()

    def test_propose_json_payload_includes_proposals(self, app_tester, monkeypatch):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        profiles["inferenceProfileSummaries"].append(NEW_PROFILE_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION} --propose --json") == 1
        payload = json.loads(app_tester.io.fetch_output())
        assert payload["proposals"][0]["model_key"] == "example-9"
        assert '"example-9"' in payload["proposal_text"]

    def test_propose_output_writes_file(self, app_tester, monkeypatch, tmp_path):
        fm, profiles = make_live_fixtures()
        fm[CHECK_REGION]["modelSummaries"].append(NEW_MODEL_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        out_file = tmp_path / "proposals.txt"
        assert app_tester.run(f"models check --region {CHECK_REGION} --propose --output {out_file}") == 1
        assert '"example-9": {' in out_file.read_text(encoding="utf-8")

    def test_output_without_propose_is_an_error(self, app_tester, monkeypatch, tmp_path, capsys):
        fm, profiles = make_live_fixtures()
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION} --output {tmp_path / 'x.txt'}") == 2
        assert "--output requires --propose" in capsys.readouterr().out

    def test_lifecycle_warning_printed_for_catalog_model(self, app_tester, monkeypatch, capsys):
        fm, profiles = make_live_fixtures()
        _replace_summary(fm, SONNET_4_LEGACY_SUMMARY)
        self._patch_fetch(monkeypatch, fm, profiles)
        assert app_tester.run(f"models check --region {CHECK_REGION}") == 0  # informational, in sync
        out = capsys.readouterr().out
        assert "anthropic.claude-sonnet-4-20250514-v1:0" in out
        assert "premium" in out.lower()
