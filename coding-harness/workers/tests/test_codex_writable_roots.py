"""Codex keeps .codex/ and .agents/ read-only inside its workspace; a repository's tracked files there are
granted back as explicit writable roots, so agents can edit them (run c5fbbe5b: .codex/INSTALL.md)."""
import os

from common import codex, git


def test_tracked_protected_directories_become_writable_roots_and_nothing_else(tmp_path):
    repo = str(tmp_path)
    git.ensure_ready(repo)
    for rel in (".codex/INSTALL.md", ".agents/x.md", "README.md"):
        os.makedirs(os.path.dirname(os.path.join(repo, rel)) or repo, exist_ok=True)
        open(os.path.join(repo, rel), "w").write("x")
    git.git(repo, "add", ".codex", "README.md")
    git.git(repo, "commit", "-qm", "init")
    # .agents exists but tracks nothing: not granted. .git is never granted.
    assert codex.writable_roots(repo) == [os.path.realpath(os.path.join(repo, ".codex"))]
