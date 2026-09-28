# Open items and known gaps

Last reviewed: 2026-08-15.

This is the durable list of work that is **known incomplete** in this repository. It exists so that
a reader can tell the difference between "not built" and "built but undocumented", without needing
access to the internal delivery ledger used during development (which is not published here).

Items are removed from this file when they are done, not marked done in place.

## Open

| ID | Item | State | What is actually true today | What closing it requires |
|----|------|-------|------------------------------|--------------------------|
| **OI-2** | Complete external validation gaps | **In progress** | [LIVE_VALIDATION.md](LIVE_VALIDATION.md) is authoritative. A second full run on 2026-08-15 passed deploy, package, distribution, Linux/Windows/macOS clients, macOS cleanup, and the applicable LV matrix. Claude Desktop LV-13 is superseded because the repository now mirrors the exact Apps Gateway/Desktop upstream contract instead of maintaining a local implementation. Okta was skipped because no tenant was available. LV-1 requires a payer-account-only API. Headless macOS still cannot grant native Keychain consent. LV-17 remains time-blocked only by one EC2 Mac host's mandatory 24-hour floor; periodic cloud and local release backstops are active for 2026-08-16 12:50 UTC. | Provide an Okta tenant; run LV-1 with payer-account access; grant Keychain consent in a macOS GUI session; and verify the scheduled Mac-host release plus terminal LV-17 sweep. |

## How to read the validation status

[LIVE_VALIDATION.md](LIVE_VALIDATION.md) carries a status table with one row per validation
(LV-1 … LV-17). Each row states the risk if the underlying assumption turns out to be wrong.

Read `unverified` as "we believe this and have written down why, but we have not observed it against
live AWS" - not as "this is broken". `Resolved-negative` means live evidence disproved the assumed
service behavior and the product now rejects the unsafe configuration. Provider-qualified passes do
not establish behavior for untested IdPs.

## Related decision records

Deliberate deferrals are recorded as ADRs rather than left implicit:

- [ADR-0022](adr/0022-mantle-metering-deferred-tripwire.md) — metering deferral with a tripwire
- [ADR-0028](adr/0028-discard-quota-lambda-sizing.md) — a discarded experiment and why
- [ADR-0031](adr/0031-defer-live-aws-validation.md) — deferring live AWS validation
- [ADR-0032](adr/0032-defer-gateway-fail-open-spend-enforcement.md) — gateway spend-enforcement posture

See the [ADR index](adr/README.md) for the full set.
