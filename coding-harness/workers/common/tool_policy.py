"""Import-light tool policy shared by the coding worker and its unit tests."""

# The coding surface an unattended worker gets. Scoped Bash rules approve only matching
# commands; anything else falls through to dontAsk and is denied.
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python *)", "Bash(python3 *)", "Bash(node *)", "Bash(npm *)",
    "Bash(npx *)", "Bash(cat *)", "Bash(ls *)", "Bash(pytest *)",
    "Bash(go *)", "Bash(cargo *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
    # Claude Code has no native move/delete tool. These remain bounded by the OS sandbox,
    # while the deny list below still wins for destructive/global variants.
    "Bash(git mv *)", "Bash(git rm *)", "Bash(mv *)", "Bash(rm *)",
    "Bash(mkdir *)", "Bash(cp *)", "Bash(touch *)",
]

DEFAULT_DISALLOWED_TOOLS = [
    "WebSearch", "WebFetch",
    "Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)",
    "Bash(rm -rf *)", "Bash(sudo *)",
]


def denied_without_changes(changed, denials) -> bool:
    """True when an unattended agent was blocked and produced no repository change."""
    return not changed and bool(denials)
