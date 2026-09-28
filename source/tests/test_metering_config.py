# ABOUTME: Regression tests for metadata-only Bedrock invocation-logging adoption
# ABOUTME: Verifies every content-delivery flag blocks adoption while metadata-only configs remain adoptable

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "metering_config"
    / "index.py"
)


@pytest.fixture
def mod(monkeypatch):
    module_name = "metering_config_index_test"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.send_response = MagicMock()
    module.boto3.client = MagicMock()
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return module


def _event():
    return {
        "RequestType": "Create",
        "ResourceProperties": {
            "LogGroupName": "/aws/bedrock/gip-metering",
            "RoleArn": "arn:aws:iam::123456789012:role/MeteringRole",
            "AdoptExistingConfig": "true",
        },
    }


@pytest.mark.parametrize(
    "enabled_flag",
    [
        "textDataDeliveryEnabled",
        "imageDataDeliveryEnabled",
        "embeddingDataDeliveryEnabled",
        "videoDataDeliveryEnabled",
        "audioDataDeliveryEnabled",
    ],
)
def test_adoption_rejects_every_content_delivery_flag(mod, enabled_flag):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = {
        "loggingConfig": {
            "cloudWatchConfig": {"logGroupName": "/customer/bedrock"},
            enabled_flag: True,
        }
    }

    mod.lambda_handler(_event(), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "FAILED"
    assert enabled_flag in response.kwargs["reason"]
    bedrock.put_model_invocation_logging_configuration.assert_not_called()


def test_adoption_preserves_metadata_only_existing_config(mod):
    bedrock = mod.boto3.client.return_value
    bedrock.get_model_invocation_logging_configuration.return_value = {
        "loggingConfig": {
            "cloudWatchConfig": {"logGroupName": "/customer/bedrock"},
            **dict.fromkeys(mod.DATA_DELIVERY_FLAGS, False),
        }
    }

    mod.lambda_handler(_event(), MagicMock())

    response = mod.send_response.call_args
    assert response.args[2] == "SUCCESS"
    assert response.kwargs["data"] == {"LogGroupName": "/customer/bedrock", "CreatedConfig": "false"}
    bedrock.put_model_invocation_logging_configuration.assert_not_called()
