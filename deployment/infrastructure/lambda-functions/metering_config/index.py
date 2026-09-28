# ABOUTME: CloudFormation custom resource that manages the Bedrock model invocation logging config
# ABOUTME: Metadata-only (all five data-delivery booleans off); adopt-or-fail semantics for pre-existing configs

"""Custom resource for the per-region Bedrock invocation logging singleton.

The logging configuration is a per-account, per-region singleton, so blindly
calling PutModelInvocationLoggingConfiguration would clobber a customer's
existing setup (e.g. SIEM delivery). Semantics (design §7.6):

- No existing config -> create ours, metadata-only (text/image/embedding/
  video/audio data delivery all OFF — the quota system must never become a
  prompt-content store).
- Existing config we don't own + AdoptExistingConfig=false -> FAIL closed.
- Existing metadata-only config + AdoptExistingConfig=true -> touch nothing;
  return the existing CloudWatch log group so the stack only attaches a
  subscription filter (fail if it has no CloudWatch destination or enables
  any content-delivery flag).
- Delete -> remove the config only if this stack created it.
"""

import json
import os
import urllib.request

import boto3
from botocore.exceptions import ClientError, ParamValidationError

# All data-delivery booleans the LoggingConfig API exposes. Every one is set
# to False; video/audio are newer and dropped on ParamValidationError so the
# function keeps working on older botocore runtimes.
DATA_DELIVERY_FLAGS = [
    "textDataDeliveryEnabled",
    "imageDataDeliveryEnabled",
    "embeddingDataDeliveryEnabled",
    "videoDataDeliveryEnabled",
    "audioDataDeliveryEnabled",
]
_OPTIONAL_FLAGS = ["videoDataDeliveryEnabled", "audioDataDeliveryEnabled"]


def enabled_data_delivery_flags(config):
    """Return content-delivery flags explicitly enabled by Bedrock."""
    return [flag for flag in DATA_DELIVERY_FLAGS if config.get(flag) is True]


def send_response(event, context, status, data=None, reason=None, physical_id=None):
    """Send the CloudFormation custom-resource response (no cfnresponse bundled)."""
    log_stream = getattr(context, "log_stream_name", "") or "metering-config"
    body = json.dumps(
        {
            "Status": status,
            "Reason": (reason or f"See CloudWatch log stream {log_stream}")[:3800],
            "PhysicalResourceId": physical_id or log_stream,
            "StackId": event.get("StackId"),
            "RequestId": event.get("RequestId"),
            "LogicalResourceId": event.get("LogicalResourceId"),
            "Data": data or {},
        }
    ).encode("utf-8")
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT", headers={"Content-Type": ""})
    with urllib.request.urlopen(request, timeout=15):  # noqa: S310 # nosec B310 — CFN pre-signed S3 URL
        pass


def put_metadata_only_config(bedrock, log_group_name, role_arn):
    """Create/replace the config with CW delivery and ALL data delivery off."""
    config = {
        "cloudWatchConfig": {"logGroupName": log_group_name, "roleArn": role_arn},
    }
    for flag in DATA_DELIVERY_FLAGS:
        config[flag] = False
    try:
        bedrock.put_model_invocation_logging_configuration(loggingConfig=config)
    except ParamValidationError:
        # Older botocore without the newer flags — retry with the core set.
        for flag in _OPTIONAL_FLAGS:
            config.pop(flag, None)
        bedrock.put_model_invocation_logging_configuration(loggingConfig=config)


def lambda_handler(event, context):
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    log_group_name = props.get("LogGroupName", "")
    role_arn = props.get("RoleArn", "")
    adopt = str(props.get("AdoptExistingConfig", "false")).lower() == "true"
    region = os.environ.get("AWS_REGION", "unknown")
    prior_physical_id = event.get("PhysicalResourceId", "")
    print(f"INFO: {request_type} invocation logging config in {region} (adopt={adopt})")

    try:
        bedrock = boto3.client("bedrock")

        if request_type == "Delete":
            if prior_physical_id.endswith("-created"):
                try:
                    bedrock.delete_model_invocation_logging_configuration()
                    print("INFO: deleted invocation logging config created by this stack")
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                        raise
            else:
                print("INFO: adopted config — leaving the customer's logging config untouched")
            send_response(event, context, "SUCCESS", physical_id=prior_physical_id)
            return

        existing = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
        existing_log_group = ((existing or {}).get("cloudWatchConfig") or {}).get("logGroupName", "")
        # Ownership: the existing config points at our managed log group, or a
        # prior Create of this resource reported "-created".
        ours = bool(existing) and (
            existing_log_group == log_group_name
            or (request_type == "Update" and prior_physical_id.endswith("-created"))
        )

        if existing and not ours:
            if not adopt:
                send_response(
                    event,
                    context,
                    "FAILED",
                    reason=(
                        f"A Bedrock model invocation logging configuration already exists in {region} "
                        f"(log group: {existing_log_group or 'S3-only'}) and AdoptExistingConfig is false. "
                        "Refusing to overwrite it. Set AdoptExistingConfig=true to attach metering to the "
                        "existing log group, or remove the existing configuration first."
                    ),
                    physical_id=prior_physical_id or None,
                )
                return
            if not existing_log_group:
                send_response(
                    event,
                    context,
                    "FAILED",
                    reason=(
                        "AdoptExistingConfig=true but the existing invocation logging configuration has no "
                        "CloudWatch Logs destination (S3-only). Metering requires a CloudWatch log group "
                        "to subscribe to."
                    ),
                    physical_id=prior_physical_id or None,
                )
                return
            enabled_flags = enabled_data_delivery_flags(existing)
            if enabled_flags:
                send_response(
                    event,
                    context,
                    "FAILED",
                    reason=(
                        "AdoptExistingConfig=true but the existing invocation logging configuration enables "
                        f"content delivery ({', '.join(enabled_flags)}). Metering telemetry must remain "
                        "metadata-only. Disable every data-delivery flag before adoption."
                    ),
                    physical_id=prior_physical_id or None,
                )
                return
            print(f"INFO: adopting existing config; log group {existing_log_group}")
            send_response(
                event,
                context,
                "SUCCESS",
                data={"LogGroupName": existing_log_group, "CreatedConfig": "false"},
                physical_id=f"bedrock-invocation-logging-{region}-adopted",
            )
            return

        put_metadata_only_config(bedrock, log_group_name, role_arn)
        print(f"INFO: invocation logging config set (metadata-only) -> {log_group_name}")
        send_response(
            event,
            context,
            "SUCCESS",
            data={"LogGroupName": log_group_name, "CreatedConfig": "true"},
            physical_id=f"bedrock-invocation-logging-{region}-created",
        )
    except Exception as e:  # noqa: BLE001 — custom resource must always respond
        print(f"ERROR: {e}")
        send_response(event, context, "FAILED", reason=str(e), physical_id=prior_physical_id or None)
