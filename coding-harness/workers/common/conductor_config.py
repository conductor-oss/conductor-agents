"""Build Conductor Python SDK configuration from worker environment.

The upstream SDK currently discovers these variables itself. Keeping the wiring
explicit here prevents a future SDK default change and, importantly, rejects a
partial key/secret pair instead of silently polling without authentication.
"""

from __future__ import annotations

import os

from conductor.client.configuration.configuration import Configuration
from conductor.client.configuration.settings.authentication_settings import AuthenticationSettings


def configuration_from_env() -> Configuration:
    key = os.environ.get("CONDUCTOR_AUTH_KEY", "")
    secret = os.environ.get("CONDUCTOR_AUTH_SECRET", "")
    if bool(key) != bool(secret):
        missing = "CONDUCTOR_AUTH_SECRET" if key else "CONDUCTOR_AUTH_KEY"
        raise RuntimeError(f"{missing} must be set when using Conductor key/secret authentication")
    if key and secret:
        return Configuration(authentication_settings=AuthenticationSettings(
            key_id=key,
            key_secret=secret,
        ))
    return Configuration()
