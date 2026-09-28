# ABOUTME: Daily model-lifecycle check — joins Bedrock modelLifecycle dates against tracked models
# ABOUTME: Publishes an SNS alert ladder (legacy / premium-pricing / EOL countdown) with SSM-backed dedup

"""Model lifecycle check Lambda (model-lifecycle stack).

Runs daily (EventBridge schedule). For every model this deployment references
(the tracked-models SSM parameter, seeded by `gip deploy model-lifecycle`):

1. ListFoundationModels(byProvider='Anthropic') in each configured region.
2. Join the tracked model's base model ID against live modelLifecycle data
   (status + legacyTime / publicExtendedAccessTime / endOfLifeTime).
3. Alert ladder, published to SNS on transition plus weekly reminders:
   - ACTIVE→LEGACY observed            → WARNING with all three dates
   - premium window within N days/now  → WARNING (premium is provider-set;
     the magnitude is NOT in the API — read the AWS Health notification)
   - EOL within 60/30/7 days           → escalating CRITICAL
   - tracked model absent from listing → CRITICAL (EOL happened / region pulled)

Alert state lives in a second SSM parameter so a daily schedule does not spam
daily. Fail-visible (A5): unhandled errors trip the stack's Errors>0 alarm.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3

TRACKED_MODELS_PARAM = os.environ.get("TRACKED_MODELS_PARAM", "")
ALERT_STATE_PARAM = os.environ.get("ALERT_STATE_PARAM", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
LEGACY_PREMIUM_WARNING_DAYS = int(os.environ.get("LEGACY_PREMIUM_WARNING_DAYS", "30"))
# Escalating EOL countdown thresholds in days, tightest window wins.
EOL_WARNING_DAYS = sorted(
    (int(d) for d in os.environ.get("EOL_WARNING_DAYS", "60,30,7").split(",") if d.strip()),
    reverse=True,
)
CHECK_REGIONS = [r.strip() for r in os.environ.get("CHECK_REGIONS", "").split(",") if r.strip()] or [
    os.environ.get("AWS_REGION", "us-east-1")
]
# Re-send an already-sent alert key after this many days (weekly reminder).
REMINDER_DAYS = int(os.environ.get("REMINDER_DAYS", "7"))

ssm_client = boto3.client("ssm")
sns_client = boto3.client("sns")


def _parse_ts(value):
    """Parse an ISO timestamp (tolerating 'Z') into an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_str(value):
    parsed = _parse_ts(value)
    return parsed.date().isoformat() if parsed else "unknown"


def base_model_id(tracked_id):
    """Strip the CRIS geography prefix: us.anthropic.claude-x → anthropic.claude-x.

    Lifecycle joins on the underlying base model, never on inference-profile
    status (all profiles report ACTIVE even for LEGACY base models — R14 §1.5).
    """
    if ".anthropic." in tracked_id and not tracked_id.startswith("anthropic."):
        return "anthropic." + tracked_id.split(".anthropic.", 1)[1]
    return tracked_id


def _lifecycle_dates(lifecycle):
    return (
        lifecycle.get("legacyTime"),
        lifecycle.get("publicExtendedAccessTime"),
        lifecycle.get("endOfLifeTime"),
    )


def evaluate_tracked_model(tracked_id, lifecycle, now):
    """Pure date-ladder evaluation. Returns a list of (alert_key, severity, message).

    ``lifecycle`` is the live modelLifecycle dict for the tracked model's base
    model, or None when the base model is absent from every checked region.
    """
    if lifecycle is None:
        return [
            (
                "absent",
                "CRITICAL",
                f"{tracked_id}: base model is ABSENT from ListFoundationModels in all checked "
                f"regions ({', '.join(CHECK_REGIONS)}). End of life reached or region access "
                "removed — inference requests will fail. Rotate now (RUNBOOKS.md 'Model rotation').",
            )
        ]

    if lifecycle.get("status") != "LEGACY":
        return []

    legacy_time, premium_time, eol_time = _lifecycle_dates(lifecycle)
    dates_line = (
        f"legacy={_date_str(legacy_time)}, "
        f"premium(publicExtendedAccess)={_date_str(premium_time)}, "
        f"endOfLife={_date_str(eol_time)}"
    )
    alerts = [
        (
            "legacy",
            "WARNING",
            f"{tracked_id}: model is LEGACY ({dates_line}). New customers cannot use it and "
            "access can lapse after 15 days of inactivity. Plan rotation (RUNBOOKS.md 'Model rotation').",
        )
    ]

    premium_dt = _parse_ts(premium_time)
    if premium_dt:
        if now >= premium_dt:
            alerts.append(
                (
                    "premium-active",
                    "WARNING",
                    f"{tracked_id}: provider-set premium pricing is ACTIVE since "
                    f"{_date_str(premium_time)}. The premium magnitude is not published in the "
                    "API — check the AWS Health Legacy notification for this model.",
                )
            )
        elif now >= premium_dt - timedelta(days=LEGACY_PREMIUM_WARNING_DAYS):
            alerts.append(
                (
                    f"premium-{LEGACY_PREMIUM_WARNING_DAYS}",
                    "WARNING",
                    f"{tracked_id}: provider-set premium pricing begins {_date_str(premium_time)} "
                    f"(within {LEGACY_PREMIUM_WARNING_DAYS} days). Rotate before then to avoid the premium.",
                )
            )

    eol_dt = _parse_ts(eol_time)
    if eol_dt:
        tightest = None
        for days in EOL_WARNING_DAYS:
            if now >= eol_dt - timedelta(days=days):
                tightest = days
        if tightest is not None:
            alerts.append(
                (
                    f"eol-{tightest}",
                    "CRITICAL",
                    f"{tracked_id}: inference FAILS after {_date_str(eol_time)} "
                    f"(within {tightest} days). Migration does not happen automatically — "
                    "rotate and re-package now (RUNBOOKS.md 'Model rotation').",
                )
            )
    return alerts


def fetch_live_lifecycles(regions):
    """ListFoundationModels per region → ({base_model_id: modelLifecycle}, failed_regions)."""
    live = {}
    failed = []
    for region in regions:
        try:
            client = boto3.client("bedrock", region_name=region)
            resp = client.list_foundation_models(byProvider="Anthropic")
            for summary in resp.get("modelSummaries", []):
                model_id = summary.get("modelId", "")
                lifecycle = summary.get("modelLifecycle") or {}
                existing = live.get(model_id)
                # Prefer a LEGACY record over ACTIVE if regions disagree.
                if existing is None or lifecycle.get("status") == "LEGACY":
                    live[model_id] = lifecycle
        except Exception as e:  # noqa: BLE001 — per-region resilience, alarm covers total failure
            print(f"WARNING: ListFoundationModels failed in {region}: {e}")
            failed.append(region)
    if failed and len(failed) == len(regions):
        raise RuntimeError(f"ListFoundationModels failed in ALL checked regions: {', '.join(failed)}")
    return live, failed


def match_lifecycle(base_id, live):
    """Find the live lifecycle for a base model ID (tolerating :suffix variants)."""
    if base_id in live:
        return live[base_id]
    for live_id, lifecycle in live.items():
        if live_id.startswith(base_id + ":") or base_id.startswith(live_id + ":"):
            return lifecycle
    return None


def _read_param_json(name, default):
    try:
        value = ssm_client.get_parameter(Name=name)["Parameter"]["Value"]
        return json.loads(value)
    except (ssm_client.exceptions.ParameterNotFound, json.JSONDecodeError):
        return default


def _should_send(state_for_model, alert_key, now):
    last_sent = _parse_ts(state_for_model.get(alert_key))
    if last_sent is None:
        return True
    return now - last_sent >= timedelta(days=REMINDER_DAYS)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)

    tracked = _read_param_json(TRACKED_MODELS_PARAM, [])
    if not isinstance(tracked, list) or not tracked:
        print(
            "WARNING: tracked-models parameter is empty or unseeded — run "
            "'gip deploy model-lifecycle' to seed it from the profile. Nothing to check."
        )
        return {"tracked": 0, "alerts_sent": 0, "clean": True, "note": "tracked-models parameter empty"}

    live, failed_regions = fetch_live_lifecycles(CHECK_REGIONS)
    state = _read_param_json(ALERT_STATE_PARAM, {})
    if not isinstance(state, dict):
        state = {}

    alerts_sent = 0
    evaluated = []
    for tracked_id in tracked:
        lifecycle = match_lifecycle(base_model_id(tracked_id), live)
        if lifecycle is None and failed_regions:
            # A region we could not query might still list the model — do not
            # raise a false CRITICAL "absent"; the failure is already logged.
            print(f"WARNING: {tracked_id} not found, but {len(failed_regions)} region(s) failed — skipping 'absent'")
            continue
        for alert_key, severity, message in evaluate_tracked_model(tracked_id, lifecycle, now):
            evaluated.append((tracked_id, alert_key))
            model_state = state.setdefault(tracked_id, {})
            if not _should_send(model_state, alert_key, now):
                continue
            subject = f"[gip] Model lifecycle {severity}: {tracked_id}"[:100]
            sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
            model_state[alert_key] = now.isoformat()
            alerts_sent += 1

    ssm_client.put_parameter(Name=ALERT_STATE_PARAM, Value=json.dumps(state), Type="String", Overwrite=True)

    result = {
        "tracked": len(tracked),
        "alerts_sent": alerts_sent,
        "active_alert_keys": len(evaluated),
        "clean": not evaluated,
        "failed_regions": failed_regions,
    }
    print(json.dumps(result))
    return result
