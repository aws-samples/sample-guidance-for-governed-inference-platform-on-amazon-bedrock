# ABOUTME: Daily sweeper deleting raw AgentCore Memory events older than the extraction window
# ABOUTME: extracted-only mode only — approximates delete-after-extraction (Kinesis correlation impossible, R9 F14)

"""Raw-event sweeper for the extracted-only memory mode (ADR-0012 D4).

``MemoryRecordCreated`` stream events do not reference the raw event that
produced them, so delete-after-extraction cannot be event-driven (R9 F14).
Instead this function runs daily and deletes every raw event older than
PURGE_AGE_HOURS (default 24h — assumed extraction horizon, verified live per
R9 Q4). The Memory resource's 3-day EventExpiryDuration is the service-side
backstop if this sweeper stops running (fail-visible alarm, axiom A5).

Enumeration is ListActors -> ListSessions -> ListEvents(includePayloads
False) -> DeleteEvent (R9 F15). Individual delete failures are recorded and
re-raised at the end so the Errors alarm fires while the rest of the sweep
still completes.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3

MEMORY_ID = os.environ.get("MEMORY_ID", "")
PURGE_AGE_HOURS = int(os.environ.get("PURGE_AGE_HOURS", "24"))

client = boto3.client("bedrock-agentcore")


def _paginate(operation, result_key, **kwargs):
    """Yield items across all pages of a bedrock-agentcore list call."""
    token = None
    while True:
        params = dict(kwargs)
        if token:
            params["nextToken"] = token
        response = operation(**params)
        yield from response.get(result_key, [])
        token = response.get("nextToken")
        if not token:
            return


def sweep(now=None):
    """Delete raw events older than the purge window. Returns stats."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=PURGE_AGE_HOURS)
    stats = {"actors": 0, "sessions": 0, "events_seen": 0, "events_deleted": 0, "failures": 0}

    for actor in _paginate(client.list_actors, "actorSummaries", memoryId=MEMORY_ID):
        actor_id = actor.get("actorId", "")
        if not actor_id:
            continue
        stats["actors"] += 1
        for session in _paginate(client.list_sessions, "sessionSummaries", memoryId=MEMORY_ID, actorId=actor_id):
            session_id = session.get("sessionId", "")
            if not session_id:
                continue
            stats["sessions"] += 1
            for event in _paginate(
                client.list_events,
                "events",
                memoryId=MEMORY_ID,
                actorId=actor_id,
                sessionId=session_id,
                includePayloads=False,
            ):
                stats["events_seen"] += 1
                timestamp = event.get("eventTimestamp")
                if timestamp is None or timestamp >= cutoff:
                    continue
                try:
                    client.delete_event(
                        memoryId=MEMORY_ID,
                        actorId=actor_id,
                        sessionId=session_id,
                        eventId=event["eventId"],
                    )
                    stats["events_deleted"] += 1
                except Exception as e:  # noqa: BLE001 — keep sweeping, fail visibly at the end
                    stats["failures"] += 1
                    print(f"ERROR: DeleteEvent failed for event {event.get('eventId')}: {e}")

    return stats


def lambda_handler(event, context):
    stats = sweep()
    print(f"INFO: memory sweep complete cutoff_hours={PURGE_AGE_HOURS} stats={stats}")
    if stats["failures"]:
        # Fail-visible (A5): raise so the Errors alarm fires; events already
        # deleted stay deleted, the next daily run retries the remainder.
        raise RuntimeError(f"memory sweep had {stats['failures']} delete failure(s)")
    return {"statusCode": 200, "body": json.dumps(stats)}
