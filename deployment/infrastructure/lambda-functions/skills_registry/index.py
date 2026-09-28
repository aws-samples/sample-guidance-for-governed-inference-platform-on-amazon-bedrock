# ABOUTME: CloudFormation custom resource handler for Custom::AgentRegistry
# ABOUTME: Creates or adopts an Agent Registry (no CFN type exists — verified 2026-07-08)

"""Custom::AgentRegistry provisioner.

Create: adopt an existing registry with the requested name, or create one.
Update: update the description; a Name change provisions a new registry and
        returns a new PhysicalResourceId so CloudFormation retires the old one
        (subject to RetainOnDelete).
Delete: no-op when RetainOnDelete is 'true' (default — approval state lives
        only in the registry); otherwise deletes the registry.

All registry API calls go through registry_client.py (namespace-migration
isolation — see that module's header).
"""

import json
import time
import urllib.request

from registry_client import RegistryClient

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send_response(event, context, status, data=None, physical_resource_id=None, reason=""):
    """Send the custom-resource response to the CloudFormation callback URL."""
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch log stream: {context.log_stream_name}",
            "PhysicalResourceId": physical_resource_id or context.log_stream_name,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data or {},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 # nosec B310 — CFN pre-signed S3 URL
        print(f"INFO: CloudFormation response sent, HTTP {resp.status}")


def _create_or_adopt(client, name, description, authorizer_type):
    """Return (registry_id, adopted)."""
    existing = client.find_registry_by_name(name)
    if existing:
        registry_id = existing.get("registryId")
        if not registry_id:
            raise RuntimeError(f"Existing registry '{name}' did not include registryId")
        _wait_registry_ready(client, registry_id)
        print(f"INFO: adopting existing registry '{name}' ({registry_id})")
        return registry_id, True
    registry_id = client.create_registry(name, description, authorizer_type)
    _wait_registry_ready(client, registry_id)
    print(f"INFO: created registry '{name}' ({registry_id})")
    return registry_id, False


def _wait_registry_ready(client, registry_id, attempts=150, delay_seconds=2):
    """Wait for Agent Registry's async Create/Update path to become usable."""
    # F-009: Registry creation can exceed the previous one-minute polling window.
    failure = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
    for _ in range(attempts):
        registry = client.get_registry(registry_id)
        status = registry.get("status")
        if status == "READY":
            return registry
        if status in failure:
            raise RuntimeError(f"Registry {registry_id} entered {status}: {registry.get('statusReason', '')}")
        time.sleep(delay_seconds)
    raise TimeoutError(f"Registry {registry_id} did not become READY")


def handler(event, context):
    """Custom resource entry point."""
    props = event.get("ResourceProperties", {})
    name = props.get("Name", "gip-skills")
    description = props.get("Description", "")
    authorizer_type = props.get("AuthorizerType", "AWS_IAM")
    retain = str(props.get("RetainOnDelete", "true")).lower() == "true"
    request_type = event.get("RequestType")

    try:
        client = RegistryClient()

        if request_type == "Create":
            registry_id, adopted = _create_or_adopt(client, name, description, authorizer_type)
            send_response(
                event,
                context,
                SUCCESS,
                data={"RegistryId": registry_id, "Adopted": str(adopted).lower()},
                physical_resource_id=registry_id,
            )
            return

        if request_type == "Update":
            old_name = event.get("OldResourceProperties", {}).get("Name")
            if old_name and old_name != name:
                # Name change: provision the new registry; new physical id
                # makes CloudFormation send a Delete for the old one.
                registry_id, adopted = _create_or_adopt(client, name, description, authorizer_type)
            else:
                registry_id = event["PhysicalResourceId"]
                adopted = False
                client.update_registry(registry_id, description)
                _wait_registry_ready(client, registry_id)
            send_response(
                event,
                context,
                SUCCESS,
                data={"RegistryId": registry_id, "Adopted": str(adopted).lower()},
                physical_resource_id=registry_id,
            )
            return

        if request_type == "Delete":
            registry_id = event["PhysicalResourceId"]
            if retain:
                print(f"INFO: RetainOnDelete=true — keeping registry {registry_id}")
            else:
                client.delete_registry(registry_id)
                print(f"INFO: deleted registry {registry_id}")
            send_response(event, context, SUCCESS, physical_resource_id=registry_id)
            return

        send_response(event, context, FAILED, reason=f"Unknown RequestType: {request_type}")
    except Exception as e:  # noqa: BLE001 — custom resources must always answer CFN
        print(f"ERROR: registry provisioning failed: {e}")
        send_response(
            event,
            context,
            FAILED,
            physical_resource_id=event.get("PhysicalResourceId"),
            reason=str(e),
        )
