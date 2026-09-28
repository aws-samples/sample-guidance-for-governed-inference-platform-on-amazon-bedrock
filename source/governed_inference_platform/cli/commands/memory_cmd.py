# ABOUTME: 'gip memory' commands — per-user erasure (forget-user) and stack/config status
# ABOUTME: Erasure is enumerate-and-delete (no single purge API): events via ListActors/ListSessions/ListEvents/DeleteEvent, records via ListMemoryRecords/BatchDeleteMemoryRecords

"""Memory management commands (opt-in AgentCore Memory stack, ADR-0016).

``gip memory forget-user <email>`` implements the per-user erasure axiom A3
requires of every content-bearing feature: it enumerates and deletes both the
user's raw short-term events (ListActors -> ListSessions -> ListEvents ->
DeleteEvent) and their extracted long-term records
(ListMemoryRecords(namespacePath=users/<hashed-actor-id>) -> BatchDeleteMemoryRecords,
at most 100 records per batch). ``--dry-run`` reports what would be deleted
without deleting anything.

``gip memory status`` summarizes the profile configuration and the deployed
stack (mode, gate, memory ID).
"""

import hashlib
import re

from cleo.commands.command import Command
from cleo.helpers import argument, option
from rich import box
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from governed_inference_platform.config import WEBSEARCH_SUPPORTED_REGIONS, Config

# BatchDeleteMemoryRecords accepts at most 100 records per call.
BATCH_DELETE_MAX = 100

# Long-term records live under these namespaces for a user (memory-stack.yaml
# strategies); the namespacePath prefix below covers both plus any future
# sub-namespaces under the user's path.
USER_NAMESPACE_PREFIX = "users/{actor_id}"
MEMORY_NAMESPACE_PATH_RE = re.compile(r"^[a-zA-Z0-9/*][a-zA-Z0-9-_/*]*(?::[a-zA-Z0-9-_/*]+)*[a-zA-Z0-9-_/*]*$")


class MemoryDeletionError(RuntimeError):
    """Raised when AgentCore Memory reports a partial deletion failure."""


def actor_id_for_email(email: str) -> str:
    normalized = email.strip().lower()
    return f"email:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def actor_ids_for_email(email: str) -> list[str]:
    """Current actorId plus the pre-hash raw-email candidate for erasure."""
    normalized = email.strip().lower()
    current = actor_id_for_email(normalized)
    return [current, normalized] if normalized != current else [current]


def namespace_paths_for_email(email: str) -> list[str]:
    paths = []
    for actor_id in actor_ids_for_email(email):
        path = USER_NAMESPACE_PREFIX.format(actor_id=actor_id)
        if MEMORY_NAMESPACE_PATH_RE.match(path):
            paths.append(path)
    return paths


def _memory_region(profile) -> str:
    """The memory stack lives in the websearch gateway region."""
    return getattr(profile, "websearch_region", None) or WEBSEARCH_SUPPORTED_REGIONS[0]


def _resolve_memory_id(profile, console: Console) -> str | None:
    """Memory ID from the profile (saved at deploy), else the stack outputs."""
    memory_id = getattr(profile, "memory_id", "") or ""
    if memory_id:
        return memory_id
    from governed_inference_platform.cli.utils.aws import get_stack_outputs

    stack_name = profile.stack_names.get("memory", f"{profile.identity_pool_name}-memory")
    outputs = get_stack_outputs(stack_name, _memory_region(profile)) or {}
    memory_id = outputs.get("MemoryId")
    if not memory_id:
        console.print(
            f"[red]Could not resolve the memory ID from the profile or the '{stack_name}' stack. "
            "Run 'gip deploy memory' first.[/red]"
        )
        return None
    return memory_id


def _paginate(operation, result_key, **kwargs):
    """Yield items across all pages of a bedrock-agentcore list call."""
    token = None
    while True:
        params = dict(kwargs)
        if token:
            params["nextToken"] = token
        response = operation(**params)
        if result_key not in response:
            raise MemoryDeletionError(
                f"{getattr(operation, '__name__', 'operation')} response missing {result_key}; keys={sorted(response)}"
            )
        yield from response.get(result_key, [])
        token = response.get("nextToken")
        if not token:
            return


def collect_user_events(client, memory_id: str, email: str) -> list[dict]:
    """Enumerate the actor's raw events: [{'sessionId', 'eventId'}, ...].

    The actorId is a deterministic hash of the user's email because AgentCore
    Memory actorId rejects raw email punctuation such as '@' and '.'.
    """
    actor_ids = set(actor_ids_for_email(email))
    events = []
    for actor in _paginate(client.list_actors, "actorSummaries", memoryId=memory_id):
        actor_id = actor.get("actorId")
        if actor_id not in actor_ids:
            continue
        for session in _paginate(client.list_sessions, "sessionSummaries", memoryId=memory_id, actorId=actor_id):
            session_id = session.get("sessionId", "")
            if not session_id:
                continue
            for event in _paginate(
                client.list_events,
                "events",
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=session_id,
                includePayloads=False,
            ):
                events.append({"actorId": actor_id, "sessionId": session_id, "eventId": event["eventId"]})
    return events


def collect_user_records(client, memory_id: str, email: str) -> list[str]:
    """Enumerate the user's extracted long-term record IDs (namespacePath prefix)."""
    record_ids = []
    seen = set()
    for namespace_path in namespace_paths_for_email(email):
        for record in _paginate(
            client.list_memory_records,
            "memoryRecordSummaries",
            memoryId=memory_id,
            namespacePath=namespace_path,
        ):
            record_id = record["memoryRecordId"]
            if record_id not in seen:
                seen.add(record_id)
                record_ids.append(record_id)
    return record_ids


def delete_user_events(client, memory_id: str, email: str, events: list[dict]) -> int:
    deleted = 0
    for event in events:
        client.delete_event(
            memoryId=memory_id,
            actorId=event["actorId"],
            sessionId=event["sessionId"],
            eventId=event["eventId"],
        )
        deleted += 1
    return deleted


def delete_user_records(client, memory_id: str, record_ids: list[str]) -> int:
    deleted = 0
    for start in range(0, len(record_ids), BATCH_DELETE_MAX):
        chunk = record_ids[start : start + BATCH_DELETE_MAX]
        response = client.batch_delete_memory_records(
            memoryId=memory_id,
            records=[{"memoryRecordId": record_id} for record_id in chunk],
        )
        if not isinstance(response, dict):
            raise MemoryDeletionError("BatchDeleteMemoryRecords returned an unexpected response shape")
        failed = response.get("failedRecords") or []
        if failed:
            failed_details = [
                {
                    "memoryRecordId": r.get("memoryRecordId", "?"),
                    "errorCode": r.get("errorCode", "?"),
                    "errorMessage": r.get("errorMessage", ""),
                }
                for r in failed
                if isinstance(r, dict)
            ]
            raise MemoryDeletionError(f"BatchDeleteMemoryRecords failed for {len(failed)} record(s): {failed_details}")
        successful = response.get("successfulRecords")
        if not isinstance(successful, list):
            raise MemoryDeletionError("BatchDeleteMemoryRecords response did not include successfulRecords")
        if len(successful) != len(chunk):
            raise MemoryDeletionError(
                f"BatchDeleteMemoryRecords deleted {len(successful)} of {len(chunk)} requested record(s)"
            )
        deleted += len(successful)
    return deleted


class MemoryForgetUserCommand(Command):
    name = "memory forget-user"
    description = "Erase a user's memory data (raw events + extracted records) — per-user erasure (A3)"

    arguments = [argument("email", description="Email of the user to erase (the memory actorId)")]

    options = [
        option("profile", description="Configuration profile to use", flag=False),
        option("dry-run", description="Report what would be deleted without deleting anything", flag=True),
        option("force", description="Skip the confirmation prompt", flag=True),
    ]

    def handle(self) -> int:
        console = Console()
        config = Config.load()
        profile = config.get_profile(self.option("profile") or config.active_profile)
        if not profile:
            console.print("[red]No profile found. Run 'poetry run gip init' first.[/red]")
            return 1
        if not getattr(profile, "memory_enabled", False):
            console.print("[yellow]Memory is not enabled for this profile — nothing to erase.[/yellow]")
            return 1

        email = self.argument("email").strip()
        if not email or "@" not in email:
            console.print(f"[red]'{email}' does not look like an email address.[/red]")
            return 1

        memory_id = _resolve_memory_id(profile, console)
        if not memory_id:
            return 1

        dry_run = self.option("dry-run")
        region = _memory_region(profile)
        actor_id = actor_id_for_email(email)

        import boto3

        client = boto3.client("bedrock-agentcore", region_name=region)

        console.print(f"[dim]Memory {memory_id} in {region} — enumerating data for {email} ({actor_id})...[/dim]")
        try:
            events = collect_user_events(client, memory_id, email)
            record_ids = collect_user_records(client, memory_id, email)
        except MemoryDeletionError as e:
            console.print(f"[red]Memory erasure failed: {e}[/red]")
            return 1

        table = Table(box=box.SIMPLE, title="Erasure plan" if dry_run else "Erasure summary")
        table.add_column("Data")
        table.add_column("Where")
        table.add_column("Count", justify="right")
        table.add_row("Raw events (short-term)", f"actor {actor_id}", str(len(events)))
        table.add_row("Extracted records (long-term)", f"users/{actor_id}/*", str(len(record_ids)))

        if not events and not record_ids:
            console.print(f"[green]No memory data found for {email} — nothing to delete.[/green]")
            return 0

        if dry_run:
            console.print(table)
            console.print("[yellow]Dry run — nothing was deleted.[/yellow]")
            return 0

        if not self.option("force"):
            console.print(table)
            if not Confirm.ask(f"\n[bold red]Permanently delete all memory data for {email}?[/bold red]"):
                console.print("[yellow]Cancelled — nothing was deleted.[/yellow]")
                return 0

        try:
            deleted_events = delete_user_events(client, memory_id, email, events)
            deleted_records = delete_user_records(client, memory_id, record_ids)
        except MemoryDeletionError as e:
            console.print(f"[red]Memory erasure failed: {e}[/red]")
            return 1

        console.print(
            f"[green]✓ Erased {deleted_events} raw event(s) and {deleted_records} extracted record(s) "
            f"for {email}.[/green]"
        )
        console.print(
            "[dim]Note: an extraction job already in flight may materialize a record after this pass — "
            "re-run this command once, ~1 hour later, for a departing user "
            "(see assets/docs/MEMORY.md).[/dim]"
        )
        return 0


class MemoryStatusCommand(Command):
    name = "memory status"
    description = "Show the memory feature configuration and deployed stack state"

    options = [
        option("profile", description="Configuration profile to use", flag=False),
    ]

    def handle(self) -> int:
        console = Console()
        config = Config.load()
        profile = config.get_profile(self.option("profile") or config.active_profile)
        if not profile:
            console.print("[red]No profile found. Run 'poetry run gip init' first.[/red]")
            return 1

        enabled = getattr(profile, "memory_enabled", False)
        table = Table(box=box.SIMPLE, title="AgentCore Memory")
        table.add_column("Setting")
        table.add_column("Value")
        table.add_row("Enabled", "yes" if enabled else "no")
        table.add_row("User memory", "yes" if getattr(profile, "memory_user_enabled", False) else "no")
        table.add_row("Org memory", "yes" if getattr(profile, "memory_org_enabled", False) else "no")
        table.add_row("Mode", getattr(profile, "memory_mode", "extracted-only"))
        if getattr(profile, "memory_mode", "extracted-only") == "full":
            table.add_row("Raw event retention", f"{getattr(profile, 'memory_raw_event_retention_days', 30)} days")
        else:
            table.add_row("Raw event retention", "3 days (service minimum) + T+24h sweeper")
        table.add_row("Deploy gate", getattr(profile, "memory_deploy_gate", "log-only"))
        org_groups = getattr(profile, "memory_org_write_groups", []) or []
        table.add_row("Org write groups", ", ".join(org_groups) if org_groups else "(empty — admins only)")
        table.add_row("Region", _memory_region(profile))

        if not enabled:
            console.print(table)
            console.print("[dim]Enable memory via 'gip init' (requires web search).[/dim]")
            return 0

        from governed_inference_platform.cli.utils.aws import get_stack_outputs

        stack_name = profile.stack_names.get("memory", f"{profile.identity_pool_name}-memory")
        outputs = get_stack_outputs(stack_name, _memory_region(profile)) or {}
        if outputs:
            table.add_row("Stack", f"{stack_name} (deployed)")
            table.add_row("Memory ID", outputs.get("MemoryId", getattr(profile, "memory_id", "") or "?"))
            table.add_row("Deployed mode", outputs.get("MemoryModeValue", "?"))
            table.add_row("Deployed gate", outputs.get("DeployGateValue", "?"))
        else:
            table.add_row("Stack", f"{stack_name} (not deployed — run 'gip deploy memory')")
        console.print(table)
        return 0
