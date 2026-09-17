from __future__ import annotations

import time
from unittest.mock import patch

from app import config


def _fresh_settings() -> config.Settings:
    # conftest.py already sets EVENTS_TABLE etc. as env vars for every test,
    # so a bare Settings() picks up valid required fields.
    return config.Settings()


@patch("app.config._load_secure_params")
def test_apply_secure_params_defaults_to_test_when_mode_unset(mock_load):
    mock_load.return_value = {
        "stripe_test_secret_key": "sk_test_abc",
        "stripe_live_secret_key": "sk_live_abc",
    }
    s = _fresh_settings()
    config._apply_secure_params(s)
    assert s.stripe_mode == "test"
    assert s.stripe_secret_key == "sk_test_abc"


@patch("app.config._load_secure_params")
def test_apply_secure_params_never_treats_anything_but_exactly_live_as_live(mock_load):
    for garbage in ("Live", " live", "LIVE", "production", "", "1", None):
        mock_load.return_value = {"stripe_mode": garbage} if garbage is not None else {}
        s = _fresh_settings()
        config._apply_secure_params(s)
        assert s.stripe_mode == "test", f"{garbage!r} must not be treated as live"


@patch("app.config._load_secure_params")
def test_apply_secure_params_switches_to_the_live_pair(mock_load):
    mock_load.return_value = {
        "stripe_mode": "live",
        "stripe_test_secret_key": "sk_test_abc",
        "stripe_test_webhook_secret": "whsec_test_abc",
        "stripe_live_secret_key": "sk_live_xyz",
        "stripe_live_webhook_secret": "whsec_live_xyz",
        "stripe_live_publishable_key": "pk_live_xyz",
    }
    s = _fresh_settings()
    config._apply_secure_params(s)
    assert s.stripe_mode == "live"
    assert s.stripe_secret_key == "sk_live_xyz"
    assert s.stripe_webhook_secret == "whsec_live_xyz"
    assert s.stripe_publishable_key == "pk_live_xyz"


@patch("app.config._load_secure_params")
def test_apply_secure_params_missing_key_for_the_active_mode_keeps_the_old_value(mock_load):
    # live mode selected, but only the test keys are actually set in SSM --
    # e.g. a live Stripe account that hasn't been created yet.
    mock_load.return_value = {"stripe_mode": "live"}
    s = _fresh_settings()
    s.stripe_secret_key = "sk_keep_me"
    config._apply_secure_params(s)
    assert s.stripe_mode == "live"
    assert s.stripe_secret_key == "sk_keep_me"  # not blanked out


@patch("app.config._load_secure_params")
def test_apply_secure_params_updates_session_secret_when_present(mock_load):
    mock_load.return_value = {"session_secret": "new-secret"}
    s = _fresh_settings()
    config._apply_secure_params(s)
    assert s.session_secret == "new-secret"


@patch("app.config._load_secure_params")
def test_refresh_is_a_noop_within_the_ttl(mock_load):
    mock_load.return_value = {"stripe_mode": "live"}
    fresh = _fresh_settings()
    with patch.object(config, "settings", fresh), \
            patch.object(config, "_secure_params_loaded_at", time.time()):
        config.refresh_secure_params_if_stale()
    mock_load.assert_not_called()
    assert fresh.stripe_mode == "test"  # untouched -- still its constructed default


@patch("app.config._load_secure_params")
def test_refresh_refetches_once_stale(mock_load):
    mock_load.return_value = {"stripe_mode": "live", "stripe_live_secret_key": "sk_live_z"}
    fresh = _fresh_settings()
    with patch.object(config, "settings", fresh), \
            patch.object(config, "_secure_params_loaded_at", 0.0):
        config.refresh_secure_params_if_stale()
    mock_load.assert_called_once()
    assert fresh.stripe_mode == "live"
    assert fresh.stripe_secret_key == "sk_live_z"


def test_load_secure_params_returns_nothing_without_a_prefix(monkeypatch):
    monkeypatch.delenv("SECURE_PARAM_PREFIX", raising=False)
    assert config._load_secure_params() == {}
