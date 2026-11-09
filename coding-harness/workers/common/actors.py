"""Actor identity for steering (roadmap H16, closure plan WP7, §7 open question #7).

"Human outranks" is only safe when the human is *identified*. The team trusts a configured registry of
actors — the security swarm's precedent of trusting only a configured identity — mapping a bearer token
to ``{name, email, authority}``. The console authenticates every steering write with it and stamps the
author as ``human:<email>``; workers derive a directive's authority from its author, so a directive that
merely *claims* ``authority: human`` without a registered human author ranks as an agent's.

Registry: ``$SWARM_ACTORS_FILE`` (default ``~/.conductor-code/actors.json``)::

    {"tokens": {"<secret token>": {"name": "Viren", "email": "viren@orkes.io", "authority": "human"}}}
"""

from __future__ import annotations

import json
import os

DEFAULT_PATH = os.path.join("~", ".conductor-code", "actors.json")


def _path() -> str:
    return os.path.expanduser(os.environ.get("SWARM_ACTORS_FILE") or DEFAULT_PATH)


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"tokens": {}}
    tokens = data.get("tokens") if isinstance(data, dict) else None
    return {"tokens": tokens if isinstance(tokens, dict) else {}}


def register(email: str, name: str = "", authority: str = "human") -> str:
    """Add an actor to the registry and return its new bearer token. The file is created if missing and
    kept private to the user (0600): a token is a credential."""
    import secrets
    email = str(email or "").strip()
    if "@" not in email:
        raise ValueError("an actor needs an email address")
    path = _path()
    data = load()
    token = secrets.token_urlsafe(24)
    data["tokens"][token] = {"name": name.strip() or email, "email": email, "authority": authority}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)
    return token


def resolve(token: str | None) -> dict | None:
    """The registered actor for a bearer token, or None."""
    if not token:
        return None
    actor = load()["tokens"].get(str(token).strip())
    if not isinstance(actor, dict) or not actor.get("email"):
        return None
    return {"name": str(actor.get("name") or actor["email"]), "email": str(actor["email"]),
            "authority": str(actor.get("authority") or "human"), "author": f"human:{actor['email']}"}


def human_emails() -> set[str]:
    return {str(a.get("email")) for a in load()["tokens"].values()
            if isinstance(a, dict) and a.get("email") and str(a.get("authority") or "human") == "human"}


def authority_of(author: str, *, humans: set[str] | None = None) -> str:
    """Derive authority from WHO wrote a directive, never from what it claims: a registered human
    (``human:<email>``) → ``human``; the monitor → ``monitor``; anyone else → ``agent``."""
    a = str(author or "")
    if a.startswith("human:"):
        return "human" if a.split(":", 1)[1] in (humans if humans is not None else human_emails()) else "agent"
    if a == "monitor":
        return "monitor"
    return "agent"
