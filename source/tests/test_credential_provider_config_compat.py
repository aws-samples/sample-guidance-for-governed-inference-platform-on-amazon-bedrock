"""Tests for credential-provider config schema compatibility."""

from credential_provider.__main__ import _profile_mapping


def test_profile_mapping_reads_nested_schema():
    assert _profile_mapping({"profiles": {"gip": {"aws_region": "us-east-1"}}}) == {"gip": {"aws_region": "us-east-1"}}


def test_profile_mapping_accepts_legacy_flat_schema():
    assert _profile_mapping({"gip": {"aws_region": "us-east-1"}}) == {"gip": {"aws_region": "us-east-1"}}
