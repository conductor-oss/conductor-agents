from __future__ import annotations

import pytest

from common.conductor_config import configuration_from_env


def test_configuration_uses_key_and_secret_environment(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_SERVER_URL", "https://conductor.example/api")
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")
    config = configuration_from_env()
    assert config.host == "https://conductor.example/api"
    assert config.authentication_settings.key_id == "test-key"
    assert config.authentication_settings.key_secret == "test-secret"


def test_configuration_allows_unauthenticated_server(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_AUTH_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_AUTH_SECRET", raising=False)
    assert configuration_from_env().authentication_settings is None


@pytest.mark.parametrize("present,missing", [
    ("CONDUCTOR_AUTH_KEY", "CONDUCTOR_AUTH_SECRET"),
    ("CONDUCTOR_AUTH_SECRET", "CONDUCTOR_AUTH_KEY"),
])
def test_configuration_rejects_partial_credentials(monkeypatch, present, missing):
    monkeypatch.setenv(present, "configured")
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(RuntimeError, match=missing):
        configuration_from_env()

