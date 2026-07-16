"""Unit tests for the env-driven tuning knobs in ``common/coding_agent.py``.

Focus: the ``_file_tree`` file-listing prime and its two caps
(``CODING_AGENT_FILE_TREE_MAX_FILES`` / ``CODING_AGENT_FILE_TREE_MAX_CHARS``),
which are read once at import into module-level constants. Because they are read
at import, the env-var cases ``importlib.reload`` the module after setting the var
so the new value takes effect.

Pure-logic: no live Conductor, no network, no LLM. ``_file_tree`` shells out to
``git ls-files`` when the dir is a git repo; ``tmp_path`` here is a plain dir, so
it exercises the bounded ``os.walk`` fallback.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import common.coding_agent as ca


def test_file_tree_empty_dir_returns_sentinel(tmp_path: Path) -> None:
    """An empty (non-git) directory yields the explicit empty-dir sentinel."""
    assert ca._file_tree(str(tmp_path)) == "(the working directory is currently empty)"


def test_file_tree_lists_files(tmp_path: Path) -> None:
    """A handful of files below the cap are listed in full, no truncation note."""
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x")
    out = ca._file_tree(str(tmp_path))
    assert "f0.txt" in out and "f1.txt" in out and "f2.txt" in out
    assert "more file(s)" not in out


def test_file_tree_truncates_when_over_max_files(tmp_path: Path, monkeypatch) -> None:
    """With a small monkeypatched MAX_FILES (module reloaded so the import-time
    constant picks it up), a dir with more files is truncated: exactly ``max_files``
    entries are shown and the '… and N more file(s)' note is appended.

    (``tmp_path`` is not a git repo, so this exercises the bounded ``os.walk``
    fallback, which itself caps collection at ``max_files * 2`` — hence N is not
    the full surplus, only what the capped walk saw. We assert the note is present
    with a positive N rather than a hardcoded count.)"""
    for i in range(20):
        (tmp_path / f"file_{i:02d}.txt").write_text("x")
    monkeypatch.setenv("CODING_AGENT_FILE_TREE_MAX_FILES", "5")
    reloaded = importlib.reload(ca)
    try:
        assert reloaded._FILE_TREE_MAX_FILES == 5
        out = reloaded._file_tree(str(tmp_path))
        shown = [ln for ln in out.splitlines() if ln.endswith(".txt")]
        assert len(shown) == 5
        m = re.search(r"… and (\d+) more file\(s\) \(list truncated", out)
        assert m is not None, out
        assert int(m.group(1)) > 0
    finally:
        monkeypatch.delenv("CODING_AGENT_FILE_TREE_MAX_FILES", raising=False)
        importlib.reload(ca)


def test_invalid_env_falls_back_to_default(monkeypatch) -> None:
    """Non-numeric and non-positive env values fall back to the built-in defaults."""
    monkeypatch.setenv("CODING_AGENT_FILE_TREE_MAX_FILES", "not-a-number")
    monkeypatch.setenv("CODING_AGENT_FILE_TREE_MAX_CHARS", "0")
    monkeypatch.setenv("CODING_AGENT_STDERR_LINES", "-3")
    monkeypatch.setenv("CODING_AGENT_STDERR_TAIL", "")
    reloaded = importlib.reload(ca)
    try:
        assert reloaded._FILE_TREE_MAX_FILES == 400
        assert reloaded._FILE_TREE_MAX_CHARS == 8000
        assert reloaded._STDERR_LINES == 200
        assert reloaded._STDERR_TAIL == 4000
    finally:
        for name in ("CODING_AGENT_FILE_TREE_MAX_FILES", "CODING_AGENT_FILE_TREE_MAX_CHARS",
                     "CODING_AGENT_STDERR_LINES", "CODING_AGENT_STDERR_TAIL"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(ca)


def test_env_int_helper() -> None:
    """The ``_env_int`` helper: valid positive → parsed; junk/<=0/unset → default."""
    import os

    os.environ["_CA_TEST_KNOB"] = "42"
    try:
        assert ca._env_int("_CA_TEST_KNOB", 7) == 42
        os.environ["_CA_TEST_KNOB"] = "0"
        assert ca._env_int("_CA_TEST_KNOB", 7) == 7
        os.environ["_CA_TEST_KNOB"] = "-1"
        assert ca._env_int("_CA_TEST_KNOB", 7) == 7
        os.environ["_CA_TEST_KNOB"] = "abc"
        assert ca._env_int("_CA_TEST_KNOB", 7) == 7
    finally:
        del os.environ["_CA_TEST_KNOB"]
    assert ca._env_int("_CA_TEST_UNSET_KNOB", 9) == 9
