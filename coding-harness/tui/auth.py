"""Conductor key/secret authentication settings for the TUI.

Keep credential discovery separate from the HTTP client so registration and API
calls enforce the same all-or-nothing environment contract.  Values are never
rendered or included in errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class AuthConfigurationError(RuntimeError):
    """The Conductor key/secret environment pair is incomplete."""


@dataclass(frozen=True)
class ConductorCredentials:
    key: str
    secret: str


def credentials_from_env() -> ConductorCredentials | None:
    """Return the configured key/secret pair, or ``None`` for open mode.

    An incomplete pair is almost always a deployment mistake.  Reject it rather
    than letting the TUI make unauthenticated calls that fail less clearly later.
    """
    key = os.environ.get("CONDUCTOR_AUTH_KEY", "")
    secret = os.environ.get("CONDUCTOR_AUTH_SECRET", "")
    if bool(key) != bool(secret):
        missing = "CONDUCTOR_AUTH_SECRET" if key else "CONDUCTOR_AUTH_KEY"
        raise AuthConfigurationError(
            f"{missing} must be set when using Conductor key/secret authentication"
        )
    if key and secret:
        return ConductorCredentials(key=key, secret=secret)
    return None
