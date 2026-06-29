"""oob worker package: the out-of-band collaborator's query side (oob_check).
The listener itself runs as a standalone process via conductor/oob-setup.sh.
Importing this module registers the @worker_task(s)."""

from . import tasks  # noqa: F401
