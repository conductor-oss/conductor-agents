"""Deterministic, safe-for-work CALLSIGNS for swarm sub-agents.

The blackboard identifies each agent by a UUID, which makes the message board read like a wall of
hex. Mapping every agent id to a stable red-team callsign (RAVEN-07, WRAITH-42, ...) makes the chatter
read like a real operation without changing any stored data -- the mapping is a pure, deterministic
display concern. Same agent id -> same callsign, always; no I/O, no state.
"""
from __future__ import annotations

import hashlib

# Curated, SFW red-team callsigns. Deliberately evocative but clean -- no profanity, slurs, or
# real org/person names. Kept to a power-of-nothing-in-particular; add freely.
CALLSIGNS = (
    "RAVEN", "VIPER", "GHOST", "REAPER", "PHANTOM", "COBRA", "WRAITH", "FALCON", "HYDRA", "ONYX",
    "ROGUE", "NOMAD", "CIPHER", "SPECTER", "TALON", "VORTEX", "BASILISK", "MAVERICK", "WARDEN", "HAVOC",
    "OBSIDIAN", "NIGHTHAWK", "IRONCLAD", "TEMPEST", "DRACO", "KRAKEN", "BLACKOUT", "SABLE", "VANDAL", "ZEALOT",
    "OVERWATCH", "SENTINEL", "MARAUDER", "OUTLAW", "RIPTIDE", "THUNDER", "GRYPHON", "MANTIS", "SCORPION", "JACKAL",
    "DIAMONDBACK", "AVALANCHE", "WOLFHOUND", "STINGRAY", "PREDATOR", "VENOM", "ECLIPSE", "RENEGADE", "JUGGERNAUT", "PYTHON",
    "COMET", "GLADIATOR", "FURY", "BANSHEE", "CRUSADER", "WIDOWMAKER", "HORNET", "BARRACUDA", "GRIMLOCK", "VULTURE",
    "ZEPHYR", "ARCLIGHT", "DEADBOLT", "FIREBRAND", "GARGOYLE", "HELLCAT", "IRONSIDE", "LONGBOW", "MOONSHOT", "NIGHTSHADE",
    "OSPREY", "PALADIN", "QUICKSILVER", "RAMPART", "SIDEWINDER", "TITAN", "UNDERTOW", "WARHAWK", "XIPHOS", "YELLOWJACKET",
)


def callsign(agent_id) -> str:
    """Stable 'CALLSIGN-NN' handle for an agent id (UUID, workflow id, or any string)."""
    s = str(agent_id or "").strip()
    if not s:
        return "GHOST-00"
    h = int(hashlib.sha1(s.encode("utf-8")).hexdigest(), 16)
    word = CALLSIGNS[h % len(CALLSIGNS)]
    tag = (h // len(CALLSIGNS)) % 100
    return f"{word}-{tag:02d}"


def label(target) -> str:
    """Render a message 'to'/'from' target as a callsign, leaving role/broadcast targets readable.

    Agent ids become callsigns; 'role:scout', 'all', 'team', '*' and the like pass through unchanged."""
    t = str(target or "").strip()
    if not t or t in ("all", "team", "*", "broadcast"):
        return t or "?"
    if ":" in t and not _looks_like_id(t):
        return t  # role:scout, kind:finding, etc.
    return callsign(t) if _looks_like_id(t) else t


def _looks_like_id(t: str) -> bool:
    """True for a UUID / long hex / workflow-id-shaped token (what an agent id looks like)."""
    core = t.replace("-", "")
    return len(core) >= 16 and all(c in "0123456789abcdefABCDEF" for c in core)
