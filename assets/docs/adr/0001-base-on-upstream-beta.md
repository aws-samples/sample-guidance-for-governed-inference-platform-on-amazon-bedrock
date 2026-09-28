# ADR-0001: Base on upstream `beta`, not `main`

Status: Accepted · Date: 2026-07-07

## Context

This fork hardens the upstream guidance into a governed enterprise inference
platform (`REVIEW.md:3-5`). Upstream develops on `beta`; `main` receives only
release merges from `beta` — its first-parent history is release PRs
(`731dcf8` "Merge pull request #742 from aws-solutions-library-samples/beta",
`914c7a9` same for #634). The repo's own rules state it: "Target: `beta` (not
`main`). `main` is release-only" (`CLAUDE.md` "Branch Strategy") and
"`main` is release-only" (`assets/docs/RUNBOOKS.md`, upgrade runbook step 1).

## Decision

Branch from `beta` at `d7b435f` (`REVIEW.md:5`).

## Alternatives considered

- **Base on `main`.** Rejected. At fork time three cost-quota fixes existed
  only on `beta` — verified with `git merge-base --is-ancestor` against
  `origin/main` / `origin/beta`:
  - `e5e0cca` — fix(quota): cost-based enforcement reads correct DDB
    attributes (#748)
  - `35093c6` — fix(quota): price cache-write (cacheCreation) tokens in cost
    estimate (#756)
  - `d7b435f` — fix(quota): aggregate token usage with `sum_over_time`, not
    `increase` (#754)

  Our own cost-mode work (ADR-0002, `9d38075`) layers directly on the
  attribute reads corrected by #748; basing on `main` would have meant
  re-implementing all three and colliding with them on every future sync.
- **Base on a release tag.** Rejected for the same duplication reason, plus
  frozen docs that the truth pass (`036ac09`) would then fix twice.

## Consequences & optimizations

- **Cost: we track a moving branch.** `beta` went v2.5.1 → v2.5.2-beta.8 in
  four days (`CHANGELOG.md:38-57`) and takes periodic `main`-sync merges
  (`ebc25b6`). Mitigation: rebase before PRs (`CLAUDE.md` "Branch Strategy")
  and keep fork changes in reviewable, upstreamable series (`REVIEW.md:6-9`).
- Benefit realized immediately: `9d38075` extends #748's corrected
  `get_policy`/`get_user_usage` cost fields instead of duplicating them.

## Evidence

- `REVIEW.md:5` — "Branch based on `beta` at `d7b435f`".
- `git log --first-parent origin/main` — release-merge-only history.
- Commits: `e5e0cca`, `35093c6`, `d7b435f` (on `beta`, absent from `main`).
