# ABOUTME: Tests for the metering_processor Lambda (server-side usage metering, Phase 1)
# ABOUTME: Covers identity parsing, defensive cache-token extraction, transactional dedup+accrual atomicity, cost calc, daily reset

"""Tests for the metering_processor Lambda.

The processor parses Bedrock model invocation log events delivered via a
CloudWatch Logs subscription filter, prices tokens with shared/pricing.py,
and — per user, in chunked TransactWriteItems — atomically commits the
conditional DEDUP# markers together with the server_* accrual update onto
UserQuotaMetrics month items. Markers and accrual succeed or fail together,
so retries and DLQ redrives are idempotent (review F4 / R5).
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "metering_processor"
    / "index.py"
)


def _load_processor(env: dict) -> object:
    """Load the metering_processor Lambda module fresh with the given environment."""
    for key, value in env.items():
        os.environ[key] = value

    module_name = f"metering_processor_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def base_env():
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "QUOTA_TABLE_REGION": "us-east-1",
        "METERING_MODE": "shadow",
        "DEDUP_TTL_HOURS": "24",
    }


@pytest.fixture
def mod(base_env):
    module = _load_processor(base_env)
    module.quota_table = MagicMock()
    module.quota_table.get_item.return_value = {}
    module.ddb_client = MagicMock()
    module.ddb_client.transact_write_items.return_value = {}
    return module


def _record(
    request_id: str = "req-1",
    arn: str = "arn:aws:sts::123456789012:assumed-role/ClaudeCodeRole/alice@example.com",
    model_id: str = "us.anthropic.claude-opus-4-8",
    input_tokens: int = 100,
    output_tokens: int = 50,
    input_extra: dict | None = None,
    output_extra: dict | None = None,
) -> dict:
    """A metadata-only invocation log record (per the design's cited log format)."""
    return {
        "schemaType": "ModelInvocationLog",
        "schemaVersion": "1.0",
        "timestamp": "2026-07-08T00:00:00Z",
        "accountId": "123456789012",
        "identity": {"arn": arn},
        "region": "us-east-1",
        "requestId": request_id,
        "operation": "InvokeModel",
        "modelId": model_id,
        "input": {"inputTokenCount": input_tokens, **(input_extra or {})},
        "output": {"outputTokenCount": output_tokens, **(output_extra or {})},
    }


def _make_event(records: list[dict]) -> dict:
    """Wrap records into a CloudWatch Logs subscription payload."""
    payload = {
        "messageType": "DATA_MESSAGE",
        "logGroup": "/aws/bedrock/gip-metering",
        "logEvents": [{"id": str(i), "timestamp": 0, "message": json.dumps(r)} for i, r in enumerate(records)],
    }
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")
    return {"awslogs": {"data": data}}


def _canceled(codes: list[str]) -> ClientError:
    """A TransactionCanceledException whose CancellationReasons carry the given codes."""
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in codes],
        },
        "TransactWriteItems",
    )


def _transact_calls(mod) -> list[list[dict]]:
    return [c.kwargs["TransactItems"] for c in mod.ddb_client.transact_write_items.call_args_list]


def _puts(items: list[dict]) -> list[dict]:
    return [i["Put"] for i in items if "Put" in i]


def _update(items: list[dict]) -> dict:
    updates = [i["Update"] for i in items if "Update" in i]
    assert len(updates) == 1, "each transaction must carry exactly one accrual Update"
    return updates[0]


# ---------------------------------------------------------------------------
# Identity extraction (mirrors sidecar_monitor / quota_check parse)
# ---------------------------------------------------------------------------


class TestIdentityExtraction:
    def test_direct_sts_email_session(self, mod):
        arn = "arn:aws:sts::123456789012:assumed-role/ClaudeCodeRole/alice@example.com"
        assert mod.extract_identity(arn) == "alice@example.com"

    def test_email_case_preserved(self, mod):
        """quota_check does not lowercase — the metering key must match its item key."""
        arn = "arn:aws:sts::123456789012:assumed-role/ClaudeCodeRole/Alice@Example.COM"
        assert mod.extract_identity(arn) == "Alice@Example.COM"

    def test_idc_reserved_sso_non_email_username(self, mod):
        arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDeveloper_abc123/akshaya.claude"
        assert mod.extract_identity(arn) == "akshaya.claude"

    def test_non_sso_role_without_email_is_unattributed(self, mod):
        arn = "arn:aws:sts::123456789012:assumed-role/CustomRole/session123"
        assert mod.extract_identity(arn) == "UNATTRIBUTED#CustomRole"

    def test_cognito_session_is_unattributed(self, mod):
        """Cognito federation sets its own session name (design Open Q3)."""
        arn = "arn:aws:sts::123456789012:assumed-role/CognitoBedrockRole/CognitoIdentityCredentials"
        assert mod.extract_identity(arn) == "UNATTRIBUTED#CognitoBedrockRole"

    def test_non_assumed_role_arn_returns_none(self, mod):
        assert mod.extract_identity("arn:aws:iam::123456789012:user/bob") is None

    def test_empty_arn_returns_none(self, mod):
        assert mod.extract_identity("") is None
        assert mod.extract_identity(None) is None


# ---------------------------------------------------------------------------
# Token extraction, including defensively captured cache fields
# ---------------------------------------------------------------------------


class TestTokenExtraction:
    def test_basic_input_output_counts(self, mod):
        parsed = mod.parse_record(_record(input_tokens=123, output_tokens=45))
        assert parsed["input_tokens"] == 123
        assert parsed["output_tokens"] == 45
        assert parsed["model_id"] == "us.anthropic.claude-opus-4-8"
        assert parsed["request_id"] == "req-1"

    def test_missing_cache_fields_default_to_zero(self, mod):
        """Metadata-only logs may omit cache counts entirely (design Open Q1)."""
        parsed = mod.parse_record(_record())
        assert parsed["cache_read_tokens"] == 0
        assert parsed["cache_write_tokens"] == 0
        assert parsed["cache_raw"] == {}

    def test_camel_case_cache_fields_captured(self, mod):
        parsed = mod.parse_record(_record(input_extra={"cacheReadInputTokenCount": 5, "cacheWriteInputTokenCount": 7}))
        assert parsed["cache_read_tokens"] == 5
        assert parsed["cache_write_tokens"] == 7
        assert parsed["cache_raw"] == {"cacheReadInputTokenCount": 5, "cacheWriteInputTokenCount": 7}

    def test_snake_case_cache_fields_in_nested_usage_block(self, mod):
        """Key names are uncertain — any cache.*token.*count match must be captured."""
        parsed = mod.parse_record(
            _record(
                output_extra={
                    "usage": {
                        "cache_read_input_token_count": 11,
                        "cache_creation_input_token_count": 13,
                    }
                }
            )
        )
        assert parsed["cache_read_tokens"] == 11
        assert parsed["cache_write_tokens"] == 13  # "creation" classified as write

    def test_unrecognized_top_level_cache_field_ignored(self, mod):
        record = _record(input_extra={"cacheReadInputTokenCount": 5})
        record["cacheReadInputTokenCount"] = "caller-controlled"
        parsed = mod.parse_record(record)
        assert parsed["cache_read_tokens"] == 5
        assert parsed["cache_raw"] == {"cacheReadInputTokenCount": 5}

    def test_converse_token_usage_spelling_without_count_suffix_captured(self, mod):
        """Converse TokenUsage uses cacheReadInputTokens/cacheWriteInputTokens."""
        parsed = mod.parse_record(_record(input_extra={"cacheReadInputTokens": 5, "cacheWriteInputTokens": 9}))
        assert parsed["cache_read_tokens"] == 5
        assert parsed["cache_write_tokens"] == 9
        assert parsed["cache_raw"] == {"cacheReadInputTokens": 5, "cacheWriteInputTokens": 9}

    def test_anthropic_native_usage_spelling_captured(self, mod):
        parsed = mod.parse_record(
            _record(
                output_extra={
                    "usage": {
                        "cache_read_input_tokens": 21,
                        "cache_creation_input_tokens": 34,
                    }
                }
            )
        )
        assert parsed["cache_read_tokens"] == 21
        assert parsed["cache_write_tokens"] == 34

    def test_alternate_spellings_of_same_quantity_not_double_counted(self, mod):
        parsed = mod.parse_record(
            _record(
                input_extra={"cacheReadInputTokenCount": 100, "cacheWriteInputTokenCount": 200},
                output_extra={
                    "usage": {
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 200,
                    }
                },
            )
        )
        assert parsed["cache_read_tokens"] == 100
        assert parsed["cache_write_tokens"] == 200
        assert len(parsed["cache_raw"]) == 4

    def test_unclassifiable_cache_key_recorded_raw_but_not_counted(self, mod):
        parsed = mod.parse_record(_record(input_extra={"cacheInputTokenCount": 7}))
        assert parsed["cache_read_tokens"] == 0
        assert parsed["cache_write_tokens"] == 0
        assert parsed["cache_raw"] == {"cacheInputTokenCount": 7}

    @pytest.mark.parametrize(
        "record",
        [
            _record(input_extra={"cacheReadInputTokenCount": "not-a-number"}),
            _record(output_extra={"usage": {"cache_creation_input_tokens": "not-a-number"}}),
        ],
    )
    def test_malformed_recognized_cache_counts_rejected(self, mod, capsys, record):
        result = mod.lambda_handler(_make_event([record]), None)

        stats = json.loads(result["body"])
        assert stats["invalid_reasons"] == {"invalid_token_count": 1}
        mod.ddb_client.transact_write_items.assert_not_called()
        output = capsys.readouterr().out
        assert '"event": "invalid_metering_record"' in output
        assert '"InvalidRecords": 1' in output

    def test_caller_request_metadata_cache_token_policy_does_not_invalidate_record(self, mod):
        record = _record(input_tokens=100, output_tokens=50)
        record["requestMetadata"] = {"cacheTokenPolicy": "caller-selected-policy"}

        result = mod.lambda_handler(_make_event([record]), None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 1
        assert stats["invalid_records"] == 0
        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        assert values[":delta"] == {"N": "150"}
        assert values[":cread"] == {"N": "0"}
        assert values[":cwrite"] == {"N": "0"}

    def test_missing_token_counts_rejected(self, mod):
        record = _record()
        record["input"] = {}
        record["output"] = {}
        with pytest.raises(mod.RecordValidationError, match="invalid_token_count"):
            mod.parse_record(record)


# ---------------------------------------------------------------------------
# Dedupe: conditional DEDUP# markers committed atomically with accrual
# ---------------------------------------------------------------------------


class TestDedup:
    def test_marker_put_in_same_transaction_as_accrual(self, mod):
        """Each event's DEDUP# marker rides in the same transaction as the accrual."""
        mod.lambda_handler(_make_event([_record(request_id="req-42")]), None)

        calls = _transact_calls(mod)
        assert len(calls) == 1
        puts = _puts(calls[0])
        assert len(puts) == 1
        assert puts[0]["TableName"] == "TestQuotaTable"
        assert puts[0]["Item"]["pk"] == {"S": "DEDUP#req-42"}
        assert puts[0]["Item"]["sk"] == {"S": "DEDUP"}
        assert puts[0]["ConditionExpression"] == "attribute_not_exists(pk)"
        # TTL ~24h out
        now = int(datetime.now(timezone.utc).timestamp())
        assert now + 23 * 3600 < int(puts[0]["Item"]["ttl"]["N"]) <= now + 25 * 3600
        # And the accrual Update is in the SAME transaction (atomicity).
        assert _update(calls[0])["Key"]["pk"] == {"S": "USER#alice@example.com"}

    def test_intra_batch_duplicate_accrued_once(self, mod):
        """The same requestId twice in one batch must be dropped in memory.

        (TransactWriteItems rejects two actions targeting the same item, so
        repeats never reach the transaction.)
        """
        event = _make_event([_record(request_id="req-same"), _record(request_id="req-same")])

        result = mod.lambda_handler(event, None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 1
        assert stats["duplicates"] == 1
        calls = _transact_calls(mod)
        assert len(calls) == 1
        assert len(_puts(calls[0])) == 1
        values = _update(calls[0])["ExpressionAttributeValues"]
        assert values[":delta"] == {"N": "150"}  # one event: 100 input + 50 output

    def test_redelivered_batch_is_noop(self, mod):
        """A fully redelivered batch (all markers exist) accrues nothing."""
        mod.ddb_client.transact_write_items.side_effect = _canceled(["ConditionalCheckFailed", "None"])

        result = mod.lambda_handler(_make_event([_record(request_id="req-42")]), None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 0
        assert stats["duplicates"] == 1
        assert mod.ddb_client.transact_write_items.call_count == 1  # nothing left to retry

    def test_partial_duplicates_dropped_and_remainder_retried(self, mod):
        """Duplicate requestIds are dropped from the chunk; the rest re-accrues."""
        mod.ddb_client.transact_write_items.side_effect = [
            _canceled(["ConditionalCheckFailed", "None", "None"]),  # r1 dup, r2 fresh
            {},
        ]
        event = _make_event(
            [
                _record(request_id="r1", input_tokens=100, output_tokens=10),
                _record(request_id="r2", input_tokens=200, output_tokens=20),
            ]
        )

        result = mod.lambda_handler(event, None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 1
        assert stats["duplicates"] == 1
        retry_items = _transact_calls(mod)[1]
        puts = _puts(retry_items)
        assert len(puts) == 1
        assert puts[0]["Item"]["pk"] == {"S": "DEDUP#r2"}
        # The retried accrual carries ONLY the fresh event's tokens.
        assert _update(retry_items)["ExpressionAttributeValues"][":delta"] == {"N": "220"}

    def test_cancellation_without_duplicates_propagates(self, mod):
        """Capacity/conflict cancellations committed nothing — the batch must retry."""
        mod.ddb_client.transact_write_items.side_effect = _canceled(["None", "TransactionConflict"])

        with pytest.raises(ClientError):
            mod.lambda_handler(_make_event([_record()]), None)

    def test_other_client_errors_propagate(self, mod):
        """Non-transactional DDB errors must bubble up so the batch retries into the DLQ."""
        mod.ddb_client.transact_write_items.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "TransactWriteItems"
        )
        with pytest.raises(ClientError):
            mod.lambda_handler(_make_event([_record()]), None)

    def test_record_without_request_id_skipped(self, mod):
        record = _record()
        del record["requestId"]
        result = mod.lambda_handler(_make_event([record]), None)
        stats = json.loads(result["body"])
        assert stats["accrued"] == 0
        assert stats["skipped"] == 1
        assert stats["invalid_reasons"] == {"missing_request_id": 1}
        mod.ddb_client.transact_write_items.assert_not_called()


# ---------------------------------------------------------------------------
# Atomicity: markers and accrual succeed or fail together (review F4 / R5)
# ---------------------------------------------------------------------------


class TestAtomicity:
    def test_partial_failure_then_retry_no_undercount_no_double_count(self, mod):
        """The F4 failure sequence: batch with users A and B; A's transaction
        commits, B's throttles; the redelivered batch must re-accrue B exactly
        once and not re-accrue A.

        Pre-fix behavior: markers were committed per event before accrual, so
        the retry read the whole batch as duplicates and B's tokens were
        permanently lost (DLQ redrive a no-op within the 24h marker TTL).
        """
        alice = _record(request_id="ra", input_tokens=100, output_tokens=10)
        bob = _record(
            request_id="rb",
            arn="arn:aws:sts::123456789012:assumed-role/ClaudeCodeRole/bob@example.com",
            input_tokens=200,
            output_tokens=20,
        )
        event = _make_event([alice, bob])

        # First delivery: alice's transaction commits, bob's throttles.
        mod.ddb_client.transact_write_items.side_effect = [
            {},
            ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "TransactWriteItems"),
        ]
        with pytest.raises(ClientError):
            mod.lambda_handler(event, None)

        # Redelivery: alice's marker now exists (committed WITH her accrual),
        # bob's does not (his transaction committed nothing).
        mod.ddb_client.transact_write_items.side_effect = [
            _canceled(["ConditionalCheckFailed", "None"]),  # alice: duplicate, dropped
            {},  # bob: accrues
        ]
        result = mod.lambda_handler(event, None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 1  # bob, exactly once
        assert stats["duplicates"] == 1  # alice, not double-counted

        calls = _transact_calls(mod)
        assert len(calls) == 4
        committed = [calls[0], calls[3]]  # the two successful transactions
        committed_keys = [_update(items)["Key"]["pk"]["S"] for items in committed]
        assert committed_keys == ["USER#alice@example.com", "USER#bob@example.com"]
        assert _update(calls[3])["ExpressionAttributeValues"][":delta"] == {"N": "220"}

    def test_failed_transaction_leaves_no_markers_for_dlq_redrive(self, mod):
        """A wholly failed batch writes nothing — a DLQ redrive re-accrues in full."""
        event = _make_event([_record(request_id="r1"), _record(request_id="r2")])

        mod.ddb_client.transact_write_items.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "TransactWriteItems"
        )
        with pytest.raises(ClientError):
            mod.lambda_handler(event, None)

        # Redrive: no markers exist, so the same batch accrues in full.
        mod.ddb_client.transact_write_items.side_effect = None
        mod.ddb_client.transact_write_items.return_value = {}
        result = mod.lambda_handler(event, None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 2
        assert stats["duplicates"] == 0
        redrive_items = _transact_calls(mod)[-1]
        assert _update(redrive_items)["ExpressionAttributeValues"][":delta"] == {"N": "300"}

    def test_chunking_respects_transact_item_limit(self, mod):
        """>99 events for one user split into chunks of <=99 markers + 1 update."""
        events = [_record(request_id=f"r{i}", input_tokens=1, output_tokens=0) for i in range(150)]

        result = mod.lambda_handler(_make_event(events), None)

        stats = json.loads(result["body"])
        assert stats["accrued"] == 150
        calls = _transact_calls(mod)
        assert [len(items) for items in calls] == [100, 52]  # 99+1, 51+1
        deltas = [int(_update(items)["ExpressionAttributeValues"][":delta"]["N"]) for items in calls]
        assert deltas == [99, 51]

    def test_daily_reset_applied_once_across_chunks(self, mod):
        """The SET-based daily reset fires in the first committed chunk only."""
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        mod.quota_table.get_item.return_value = {"Item": {"server_daily_date": yesterday}}
        events = [_record(request_id=f"r{i}", input_tokens=1, output_tokens=0) for i in range(150)]

        mod.lambda_handler(_make_event(events), None)

        first, second = _transact_calls(mod)
        assert "SET server_daily_tokens = :delta" in _update(first)["UpdateExpression"]
        assert "SET server_daily_tokens" not in _update(second)["UpdateExpression"]
        assert "server_daily_tokens :delta" in _update(second)["UpdateExpression"]  # ADD clause


# ---------------------------------------------------------------------------
# Cost calculation (parity with shared/pricing.py)
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_opus_cost_matches_pricing_rates(self, mod):
        """1M input + 1M output on opus = $5 + $25 = $30 (shared/pricing.py rates)."""
        event = _make_event(
            [_record(model_id="global.anthropic.claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000)]
        )
        mod.lambda_handler(event, None)

        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        assert Decimal(values[":cost"]["N"]) == Decimal("30.0")
        assert values[":inp"] == {"N": "1000000"}
        assert values[":out"] == {"N": "1000000"}

    def test_cache_tokens_priced_when_present(self, mod):
        """Cache read/write counts, when the log carries them, contribute to cost."""
        event = _make_event(
            [
                _record(
                    model_id="us.anthropic.claude-sonnet-4-6-v1",
                    input_tokens=0,
                    output_tokens=0,
                    input_extra={"cacheReadInputTokenCount": 1_000_000, "cacheWriteInputTokenCount": 1_000_000},
                )
            ]
        )
        mod.lambda_handler(event, None)

        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        # sonnet: cache_read 0.30 + cache_write 3.75
        assert Decimal(values[":cost"]["N"]) == Decimal("4.05")
        assert values[":cread"] == {"N": "1000000"}
        assert values[":cwrite"] == {"N": "1000000"}

    def test_cost_parity_with_shared_pricing(self, mod):
        """The accrued cost must equal shared.pricing.calculate_cost exactly."""
        from decimal import Decimal as D

        event = _make_event(
            [_record(model_id="us.anthropic.claude-fable-5", input_tokens=250_000, output_tokens=80_000)]
        )
        mod.lambda_handler(event, None)

        expected = mod.calculate_cost(250_000, 80_000, 0, 0, model_family="fable", rates=mod.get_rates())
        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        assert D(values[":cost"]["N"]) == D(str(round(expected, 6)))

    def test_application_inference_profile_arn_is_not_silently_priced_as_sonnet(self, mod, capsys):
        event = _make_event(
            [
                _record(
                    model_id="arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/team-sonnet",
                    input_tokens=1_000_000,
                    output_tokens=1_000_000,
                )
            ]
        )

        mod.lambda_handler(event, None)

        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        assert Decimal(values[":cost"]["N"]) == Decimal("0.0")
        assert "server_estimated_cost set to 0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Accrual: aggregation, daily reset guard, item shape
# ---------------------------------------------------------------------------


class TestAccrual:
    def test_batch_aggregated_per_user_single_transaction(self, mod):
        """Multiple events for one user produce exactly one transaction (design §7.3)."""
        event = _make_event(
            [
                _record(request_id="r1", input_tokens=100, output_tokens=10),
                _record(request_id="r2", input_tokens=200, output_tokens=20),
            ]
        )
        mod.lambda_handler(event, None)

        calls = _transact_calls(mod)
        assert len(calls) == 1
        assert len(_puts(calls[0])) == 2  # one marker per event, same transaction
        update = _update(calls[0])
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        assert update["Key"] == {"pk": {"S": "USER#alice@example.com"}, "sk": {"S": f"MONTH#{current_month}"}}
        values = update["ExpressionAttributeValues"]
        assert values[":delta"] == {"N": "330"}
        assert values[":inp"] == {"N": "300"}
        assert values[":out"] == {"N": "30"}

    def test_multiple_users_get_separate_transactions(self, mod):
        event = _make_event(
            [
                _record(request_id="r1"),
                _record(
                    request_id="r2",
                    arn="arn:aws:sts::123456789012:assumed-role/ClaudeCodeRole/bob@example.com",
                ),
            ]
        )
        mod.lambda_handler(event, None)
        keys = {_update(items)["Key"]["pk"]["S"] for items in _transact_calls(mod)}
        assert keys == {"USER#alice@example.com", "USER#bob@example.com"}

    def test_new_day_resets_daily_counters_with_set(self, mod):
        """server_daily_date != today -> daily counters are SET (reset), not ADDed."""
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        mod.quota_table.get_item.return_value = {"Item": {"server_daily_date": yesterday}}

        mod.lambda_handler(_make_event([_record()]), None)

        expr = _update(_transact_calls(mod)[0])["UpdateExpression"]
        assert "SET server_daily_tokens = :delta" in expr
        assert "server_daily_date = :date" in expr

    def test_same_day_adds_to_daily_counters(self, mod):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mod.quota_table.get_item.return_value = {"Item": {"server_daily_date": today}}

        mod.lambda_handler(_make_event([_record()]), None)

        expr = _update(_transact_calls(mod)[0])["UpdateExpression"]
        assert "SET server_daily_tokens" not in expr
        assert "server_daily_tokens :delta" in expr  # ADD clause

    def test_source_region_recorded_as_string_set(self, mod):
        mod.lambda_handler(_make_event([_record()]), None)
        values = _update(_transact_calls(mod)[0])["ExpressionAttributeValues"]
        assert values[":region"] == {"SS": [mod.SOURCE_REGION]}

    def test_unattributed_events_accrue_under_bucket_key(self, mod):
        event = _make_event([_record(arn="arn:aws:sts::123456789012:assumed-role/CustomRole/session123")])
        mod.lambda_handler(event, None)
        assert _update(_transact_calls(mod)[0])["Key"]["pk"] == {"S": "USER#UNATTRIBUTED#CustomRole"}

    def test_unparseable_message_skipped(self, mod):
        payload = {"logEvents": [{"id": "0", "timestamp": 0, "message": "not json"}]}
        data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
        result = mod.lambda_handler({"awslogs": {"data": data}}, None)
        stats = json.loads(result["body"])
        assert stats["skipped"] == 1
        assert stats["invalid_reasons"] == {"malformed_json": 1}
        mod.ddb_client.transact_write_items.assert_not_called()

    def test_non_object_record_is_fail_visible(self, mod):
        payload = {"logEvents": [{"id": "0", "timestamp": 0, "message": "[]"}]}
        data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
        result = mod.lambda_handler({"awslogs": {"data": data}}, None)

        stats = json.loads(result["body"])
        assert stats["invalid_reasons"] == {"malformed_record": 1}
        mod.ddb_client.transact_write_items.assert_not_called()

    @pytest.mark.parametrize("field", ["identity", "input", "output"])
    def test_non_object_record_blocks_are_fail_visible(self, mod, field):
        record = _record()
        record[field] = []

        result = mod.lambda_handler(_make_event([record]), None)

        stats = json.loads(result["body"])
        assert stats["invalid_reasons"] == {"malformed_record": 1}
        mod.ddb_client.transact_write_items.assert_not_called()

    def test_non_string_request_id_is_fail_visible_without_marker(self, mod):
        result = mod.lambda_handler(_make_event([_record(request_id=["not", "an", "id"])]), None)

        stats = json.loads(result["body"])
        assert stats["invalid_reasons"] == {"missing_request_id": 1}
        mod.ddb_client.transact_write_items.assert_not_called()

    @pytest.mark.parametrize(
        ("record", "reason"),
        [
            (_record(input_tokens=0, output_tokens=0), "zero_tokens"),
            (_record(input_tokens=-1, output_tokens=1), "invalid_token_count"),
            (_record(input_tokens="bad", output_tokens=1), "invalid_token_count"),
        ],
    )
    def test_invalid_tokens_emit_structured_metric_without_dedup_marker(self, mod, capsys, record, reason):
        result = mod.lambda_handler(_make_event([record]), None)

        stats = json.loads(result["body"])
        assert stats["invalid_records"] == 1
        assert stats["invalid_reasons"] == {reason: 1}
        mod.ddb_client.transact_write_items.assert_not_called()
        output = capsys.readouterr().out
        assert '"event": "invalid_metering_record"' in output
        assert '"InvalidRecords": 1' in output
        assert '"Namespace": "GIP/Metering"' in output

    def test_event_without_awslogs_is_noop(self, mod):
        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        mod.ddb_client.transact_write_items.assert_not_called()
