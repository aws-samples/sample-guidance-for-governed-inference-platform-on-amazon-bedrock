# ADR-0023 — Deterministic skill eval runner: local hard-assertion harness, no judge gating, no live model calls by default

Status: Accepted · Wave 3, lane E-S3 · 2026-07-13 · Research: an internal research memo

## Context

Model releases outpace skill review: a skill verified on one model family can
silently regress on the next (R15). The registry lane (E-S1, ADR-0017) already
freezes the metadata this needs — `model_compat[]` rows with
`verified | drift | unverified | incompatible` status and a
`default_for_families` resolution index — but nothing produced the verdicts.
R15 evaluated three eval substrates: Bedrock Evaluations (scores
prompt/response pairs, never executes anything), AgentCore Evaluations
(evaluates OTEL trace spans, predominantly judge-model scores), and a
roll-your-own deterministic runner. Only the third can execute fixtures and
assert on artifacts, which is the owner's core requirement.

## Decision

1. **A local deterministic runner, `gip skills eval <dir>`**
   (`source/governed_inference_platform/cli/commands/skill_eval.py`), driven by an
   `eval.yaml` next to `SKILL.md` (schema_version 1, strictly validated —
   unknown keys rejected, model ids checked against the `models.py` catalog,
   `--validate-only` for CI).
2. **Hard assertions only gate v1:** exit code, expected/forbidden output
   files, `contains` anchors, JSON-schema checks (feature-gated on the
   optional `jsonschema` package — declared assertions fail closed when it is
   missing), and forbidden-path integrity (sha256 before/after; create,
   modify, or delete all fail). `judge` criteria are schema-validated and
   recorded but never gate.
3. **No live model calls by default.** Cases execute an optional local
   `command` in a fresh isolated workspace seeded from fixtures; cases that
   invoke a real harness/model must declare `live: true` and are skipped
   unless `--live` is passed.
4. **Results in the R15 §4.1 record shape**, one per (skill-version x
   model-id x run), persisted to `<skill>/evals/results/<run_id>.json`:
   verdict, pass_rate, per-trial case results, `drift_vs_baseline`,
   `case_flips`, `model_family` (CRIS variants collapse to one family stem),
   `eval_manifest_sha`. Drift = baseline pass_rate - candidate pass_rate;
   verdict `drift` on any pass->fail case flip or drift > 0.1 — the manual
   fork trigger (`fork_of` / `default_for_families`, already enforced at
   publish time by E-S1's one-owner-per-family invariant).

## Alternatives considered

- **Bedrock Evaluations as substrate — REJECTED** (R15 §1): scores supplied
  prompt/response JSONL; cannot execute a skill or diff artifacts.
- **AgentCore Evaluations as substrate — REJECTED for v1, kept as sidecar
  path:** trace-span domain, judge-heavy; useful later for soft-criteria
  scoring and drift dashboards, not as the runner.
- **Adopting Alibaba skill-up — REJECTED for now:** closest off-the-shelf
  runner but `v1alpha1` and container-oriented; its schema shapes informed
  `eval.yaml`. Re-evaluate when stable.
- **CodeBuild live-harness matrix (`claude -p` headless) — DEFERRED:** the
  load-bearing assumption (transcript shape, cost envelope) is unverified
  (R15 §6); the local runner's `command` + `live: true` contract composes
  with it without schema changes.

## Consequences

- Skill authors get a CI-runnable drift gate with zero AWS dependencies; the
  same `eval.yaml` later drives the live matrix.
- Deferred and explicitly out of v1: judge-model scoring as a gate,
  auto-forking on drift (governance regression if unreviewed), transcript
  assertions (`tool_calls`, `skill_triggered`), variance normalization,
  registry write-back of eval records, and MCP dynamic skill delivery.
- Registry metadata is untouched: `model_compat` / `default_for_families`
  keep their frozen `io.gip.skill/v1` semantics; eval records stay outside
  the registry (`evals.suite_ref` / `latest_score_ref` pointers only).

## Evidence

`skill_eval.py:validate_eval_manifest/run_case_trial/run_eval/model_family`;
tests in `tests/cli/commands/test_skill_eval.py` (schema rejections,
assertion semantics including forbidden-path create/modify/delete, drift/flip
scoring, live-case skipping, command surface); docs
`assets/docs/SKILLS_REGISTRY.md` §"Skill evals".
