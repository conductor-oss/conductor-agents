from __future__ import annotations

import pytest

from common.conductor_config import configuration_from_env


def test_configuration_uses_server_key_and_secret(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_SERVER_URL", "https://enterprise.example/api")
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")

    config = configuration_from_env()

    assert config.host == "https://enterprise.example/api"
    assert config.authentication_settings.key_id == "test-key"
    assert config.authentication_settings.key_secret == "test-secret"


@pytest.mark.parametrize("present,missing", [
    ("CONDUCTOR_AUTH_KEY", "CONDUCTOR_AUTH_SECRET"),
    ("CONDUCTOR_AUTH_SECRET", "CONDUCTOR_AUTH_KEY"),
])
def test_configuration_rejects_partial_credentials(monkeypatch, present, missing):
    monkeypatch.delenv("CONDUCTOR_AUTH_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_AUTH_SECRET", raising=False)
    monkeypatch.setenv(present, "do-not-render-this")

    with pytest.raises(RuntimeError, match=missing) as caught:
        configuration_from_env()

    assert "do-not-render-this" not in str(caught.value)
