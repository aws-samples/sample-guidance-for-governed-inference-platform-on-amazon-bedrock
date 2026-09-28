# ABOUTME: Tests for 'gip memory' commands (forget-user, status) with a mocked bedrock-agentcore data plane
# ABOUTME: Covers enumerate-and-delete erasure, ≤100-per-batch record deletion, --dry-run, and profile gating

"""Unit tests for the memory management commands (`gip memory ...`).

Erasure is enumerate-and-delete (ADR-0012 D4: no single purge API): raw events
via ListActors → ListSessions → ListEvents → DeleteEvent, extracted records
via ListMemoryRecords(namespacePath) → BatchDeleteMemoryRecords in chunks of
at most 100. All AWS calls are mocked — these tests assert the enumeration,
batching, and dry-run contracts.
"""

from unittest.mock import MagicMock, patch

from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli import create_application
from governed_inference_platform.cli.commands.memory_cmd import (
    BATCH_DELETE_MAX,
    MemoryDeletionError,
    MemoryForgetUserCommand,
    MemoryStatusCommand,
    actor_id_for_email,
    collect_user_events,
    collect_user_records,
    delete_user_records,
)
from governed_inference_platform.config import Profile

ALICE = "alice@example.com"
ALICE_ACTOR = actor_id_for_email(ALICE)


def _memory_profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "keyring",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip",
        "provider_type": "okta",
        "web_search_enabled": True,
        "memory_enabled": True,
        "memory_user_enabled": True,
        "memory_id": "mem-abc123",
    }
    data.update(overrides)
    return Profile.from_dict(data)


def _client_with(events_by_session=None, record_ids=None, actors=None):
    """A bedrock-agentcore client mock wired for one actor (ALICE)."""
    client = MagicMock()
    events_by_session = events_by_session or {}
    client.list_actors.return_value = {"actorSummaries": actors if actors is not None else [{"actorId": ALICE_ACTOR}]}
    client.list_sessions.return_value = {"sessionSummaries": [{"sessionId": s} for s in events_by_session]}
    client.list_events.side_effect = lambda **kw: {
        "events": [{"eventId": e} for e in events_by_session.get(kw["sessionId"], [])]
    }
    client.list_memory_records.return_value = {
        "memoryRecordSummaries": [{"memoryRecordId": r} for r in (record_ids or [])]
    }
    client.batch_delete_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": r} for r in (record_ids or [])],
        "failedRecords": [],
    }
    return client


# --- Pure enumeration/deletion helpers ---


def test_collect_user_events_only_enumerates_the_matching_actor():
    """Erasure must never touch another actor's sessions/events."""
    client = _client_with(
        events_by_session={"s1": ["e1", "e2"]},
        actors=[{"actorId": actor_id_for_email("bob@example.com")}, {"actorId": ALICE_ACTOR}],
    )

    events = collect_user_events(client, "mem-abc123", ALICE)

    assert events == [
        {"actorId": ALICE_ACTOR, "sessionId": "s1", "eventId": "e1"},
        {"actorId": ALICE_ACTOR, "sessionId": "s1", "eventId": "e2"},
    ]
    for call in client.list_sessions.call_args_list:
        assert call.kwargs["actorId"] == ALICE_ACTOR


def test_collect_user_events_no_matching_actor_returns_empty():
    client = _client_with(actors=[{"actorId": actor_id_for_email("bob@example.com")}])
    assert collect_user_events(client, "mem-abc123", ALICE) == []
    client.list_sessions.assert_not_called()


def test_collect_user_events_lists_without_payloads():
    """Enumeration for deletion never pulls conversation content."""
    client = _client_with(events_by_session={"s1": ["e1"]})
    collect_user_events(client, "mem-abc123", ALICE)
    assert client.list_events.call_args.kwargs["includePayloads"] is False


def test_collect_user_records_scopes_by_user_namespace_path():
    """Long-term records are enumerated under users/<email> only —
    the hierarchical namespacePath covers facts, preferences, and any
    future sub-namespaces of that user."""
    client = _client_with(record_ids=["r1", "r2"])

    records = collect_user_records(client, "mem-abc123", ALICE)

    assert records == ["r1", "r2"]
    namespace_paths = [call.kwargs["namespacePath"] for call in client.list_memory_records.call_args_list]
    assert namespace_paths == [f"users/{ALICE_ACTOR}"]


def test_collect_user_records_fails_when_response_shape_changes():
    client = _client_with()
    client.list_memory_records.return_value = {"records": []}

    try:
        collect_user_records(client, "mem-abc123", ALICE)
    except MemoryDeletionError as e:
        assert "memoryRecordSummaries" in str(e)
    else:
        raise AssertionError("expected MemoryDeletionError")


def test_collect_paginates_all_levels():
    client = MagicMock()
    client.list_actors.side_effect = [
        {"actorSummaries": [{"actorId": ALICE_ACTOR}], "nextToken": "t1"},
        {"actorSummaries": []},
    ]
    client.list_sessions.side_effect = [
        {"sessionSummaries": [{"sessionId": "s1"}], "nextToken": "t2"},
        {"sessionSummaries": [{"sessionId": "s2"}]},
    ]
    client.list_events.side_effect = [
        {"events": [{"eventId": "e1"}], "nextToken": "t3"},
        {"events": [{"eventId": "e2"}]},
        {"events": [{"eventId": "e3"}]},
    ]

    events = collect_user_events(client, "mem-abc123", ALICE)

    assert [e["eventId"] for e in events] == ["e1", "e2", "e3"]


def test_delete_user_records_chunks_at_batch_delete_max():
    """BatchDeleteMemoryRecords accepts at most 100 records per call."""
    client = MagicMock()
    record_ids = [f"r{i}" for i in range(2 * BATCH_DELETE_MAX + 50)]
    client.batch_delete_memory_records.side_effect = lambda **kw: {
        "successfulRecords": kw["records"],
        "failedRecords": [],
    }

    deleted = delete_user_records(client, "mem-abc123", record_ids)

    assert deleted == 250
    sizes = [len(c.kwargs["records"]) for c in client.batch_delete_memory_records.call_args_list]
    assert sizes == [100, 100, 50]
    first_record = client.batch_delete_memory_records.call_args_list[0].kwargs["records"][0]
    assert first_record == {"memoryRecordId": "r0"}


def test_delete_user_records_fails_on_partial_failure():
    client = MagicMock()
    client.batch_delete_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "r1"}],
        "failedRecords": [{"memoryRecordId": "r2", "errorCode": "InternalError"}],
    }

    try:
        delete_user_records(client, "mem-abc123", ["r1", "r2"])
    except MemoryDeletionError as e:
        assert "r2" in str(e)
    else:
        raise AssertionError("expected MemoryDeletionError")


# --- Command wiring ---


def test_memory_commands_registered():
    app = create_application()
    names = set(app._commands)
    assert "memory forget-user" in names
    assert "memory status" in names


def _run_forget(profile, args, client, capsys):
    with (
        patch("governed_inference_platform.cli.commands.memory_cmd.Config") as MockConfig,
        patch("boto3.client", return_value=client),
    ):
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        tester = CommandTester(MemoryForgetUserCommand())
        code = tester.execute(args)
        return code, capsys.readouterr().out


def test_forget_user_dry_run_reports_and_deletes_nothing(capsys):
    client = _client_with(events_by_session={"s1": ["e1", "e2"]}, record_ids=["r1"])

    code, output = _run_forget(_memory_profile(), f"{ALICE} --dry-run", client, capsys)

    assert code == 0
    assert "Dry run" in output
    client.delete_event.assert_not_called()
    client.batch_delete_memory_records.assert_not_called()


def test_forget_user_force_deletes_events_and_records(capsys):
    client = _client_with(events_by_session={"s1": ["e1"], "s2": ["e2"]}, record_ids=["r1", "r2"])

    code, output = _run_forget(_memory_profile(), f"{ALICE} --force", client, capsys)

    assert code == 0
    assert client.delete_event.call_count == 2
    delete_call = client.delete_event.call_args_list[0].kwargs
    assert delete_call["memoryId"] == "mem-abc123"
    assert delete_call["actorId"] == ALICE_ACTOR
    records = client.batch_delete_memory_records.call_args.kwargs["records"]
    assert records == [{"memoryRecordId": "r1"}, {"memoryRecordId": "r2"}]
    assert "2 raw event(s)" in output
    assert "2 extracted record(s)" in output
    # Extraction race note: departing users need a second pass ~1h later.
    assert "re-run" in output.lower()


def test_forget_user_returns_nonzero_when_record_delete_fails(capsys):
    client = _client_with(record_ids=["r1"])
    client.batch_delete_memory_records.return_value = {"failedRecords": [{"memoryRecordId": "r1"}]}

    code, output = _run_forget(_memory_profile(), f"{ALICE} --force", client, capsys)

    assert code == 1
    assert "Memory erasure failed" in output


def test_forget_user_nothing_found_exits_zero_without_deleting(capsys):
    client = _client_with()

    code, output = _run_forget(_memory_profile(), f"{ALICE} --force", client, capsys)

    assert code == 0
    assert "nothing to delete" in output.lower()
    client.delete_event.assert_not_called()
    client.batch_delete_memory_records.assert_not_called()


def test_forget_user_requires_memory_enabled(capsys):
    client = _client_with()
    code, output = _run_forget(_memory_profile(memory_enabled=False), f"{ALICE} --force", client, capsys)
    assert code == 1
    assert "not enabled" in output
    client.list_actors.assert_not_called()


def test_forget_user_rejects_non_email(capsys):
    client = _client_with()
    code, output = _run_forget(_memory_profile(), "not-an-email --force", client, capsys)
    assert code == 1
    assert "email" in output.lower()
    client.list_actors.assert_not_called()


def _run_status(profile, capsys):
    with patch("governed_inference_platform.cli.commands.memory_cmd.Config") as MockConfig:
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        tester = CommandTester(MemoryStatusCommand())
        code = tester.execute("")
        return code, capsys.readouterr().out


def test_status_disabled_profile_shows_how_to_enable(capsys):
    code, output = _run_status(_memory_profile(memory_enabled=False), capsys)
    assert code == 0
    assert "no" in output
    assert "gip init" in output


def test_status_enabled_profile_reads_stack_outputs(capsys):
    outputs = {"MemoryId": "mem-abc123", "MemoryModeValue": "extracted-only", "DeployGateValue": "log-only"}
    with patch("governed_inference_platform.cli.utils.aws.get_stack_outputs", return_value=outputs):
        code, output = _run_status(_memory_profile(), capsys)
    assert code == 0
    assert "mem-abc123" in output
    assert "deployed" in output
    assert "log-only" in output
