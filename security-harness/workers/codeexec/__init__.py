"""code_exec worker package: runs agent-authored Python in a hardened, ephemeral
Docker sandbox so the deep-pentest agent can operate the target product end to end.
Importing this module registers the @worker_task(s)."""

from . import tasks  # noqa: F401
