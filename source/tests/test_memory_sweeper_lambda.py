# ABOUTME: Tests for the memory_sweeper Lambda (extracted-only T+24h raw-event purge)
# ABOUTME: Covers cutoff selection, pagination, and fail-visible error propagation (A5)

"""Tests for the memory_sweeper Lambda.

The sweeper approximates delete-after-extraction (Kinesis correlation is
impossible — R9 F14): every raw event older than PURGE_AGE_HOURS is deleted;
younger events are left for extraction to complete. Delete failures are
counted, the sweep continues, and the invocation raises at the end so the
Errors alarm fires (fail-visible, A5).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "memory_sweeper"
    / "index.py"
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _load_sweeper(env: dict | None = None) -> object:
    defaults = {"MEMORY_ID": "mem-test123", "PURGE_AGE_HOURS": "24"}
    defaults.update(env or {})
    for key, value in defaults.items():
        os.environ[key] = value
    module_name = f"memory_sweeper_index_{id(defaults)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.client = MagicMock()
    return module


def _wire(module, events, actors=None, sessions=None):
    module.client.list_actors.return_value = {"actorSummaries": actors or [{"actorId": "alice@example.com"}]}
    module.client.list_sessions.return_value = {"sessionSummaries": sessions or [{"sessionId": "s1"}]}
    module.client.list_events.return_value = {"events": events}


def test_deletes_only_events_older_than_cutoff():
    module = _load_sweeper()
    old = {"eventId": "e-old", "eventTimestamp": NOW - timedelta(hours=25)}
    fresh = {"eventId": "e-fresh", "eventTimestamp": NOW - timedelta(hours=2)}
    _wire(module, [old, fresh])

    stats = module.sweep(now=NOW)

    assert stats["events_deleted"] == 1
    delete_kwargs = module.client.delete_event.call_args.kwargs
    assert delete_kwargs == {
        "memoryId": "mem-test123",
        "actorId": "alice@example.com",
        "sessionId": "s1",
        "eventId": "e-old",
    }


def test_lists_events_without_payloads():
    """Enumeration never pulls conversation content (metadata-only sweep)."""
    module = _load_sweeper()
    _wire(module, [])
    module.sweep(now=NOW)
    assert module.client.list_events.call_args.kwargs["includePayloads"] is False


def test_paginates_all_levels():
    module = _load_sweeper()
    module.client.list_actors.side_effect = [
        {"actorSummaries": [{"actorId": "a1"}], "nextToken": "t1"},
        {"actorSummaries": [{"actorId": "a2"}]},
    ]
    module.client.list_sessions.return_value = {"sessionSummaries": [{"sessionId": "s1"}]}
    module.client.list_events.side_effect = [
        {"events": [{"eventId": "e1", "eventTimestamp": NOW - timedelta(days=2)}], "nextToken": "t2"},
        {"events": [{"eventId": "e2", "eventTimestamp": NOW - timedelta(days=2)}]},
        {"events": []},
    ]

    stats = module.sweep(now=NOW)

    assert stats["actors"] == 2
    assert stats["events_deleted"] == 2


def test_delete_failure_is_counted_and_raised_fail_visible():
    """One bad event doesn't stop the sweep, but the handler raises at the
    end so the Errors alarm fires (A5)."""
    module = _load_sweeper()
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    events = [
        {"eventId": "e1", "eventTimestamp": ancient},
        {"eventId": "e2", "eventTimestamp": ancient},
    ]
    _wire(module, events)
    module.client.delete_event.side_effect = [RuntimeError("boom"), {}]

    with pytest.raises(RuntimeError, match="1 delete failure"):
        module.lambda_handler({}, None)
    assert module.client.delete_event.call_count == 2
