from credential_provider.__main__ import MultiProviderAuth


def test_legacy_quota_check_blocks_oidc_token_without_email_by_default():
    auth = MultiProviderAuth.__new__(MultiProviderAuth)
    auth.config = {"quota_fail_mode": "closed"}
    auth._debug_print = lambda *_args, **_kwargs: None

    result = auth._check_quota({"sub": "user-123"}, id_token="header.payload.signature")  # nosec B106

    assert result["allowed"] is False
    assert result["reason"] == "missing_identity"


def test_legacy_quota_check_missing_email_honors_explicit_fail_open():
    auth = MultiProviderAuth.__new__(MultiProviderAuth)
    auth.config = {"quota_fail_mode": "open"}
    auth._debug_print = lambda *_args, **_kwargs: None

    result = auth._check_quota({"sub": "user-123"}, id_token="header.payload.signature")  # nosec B106

    assert result["allowed"] is True
    assert result["reason"] == "no_email"
