"""Unit tests for ``common.exec.run``'s PATH augmentation: on-disk homebrew/
uv/cargo bin dirs get appended for subprocesses even when the worker process's
own PATH omits them (e.g. started by a restart-loop wrapper, not a login shell)."""

from __future__ import annotations

import os

from common.exec import _augmented_env, run


def test_augmented_env_appends_missing_existing_dir(tmp_path, monkeypatch):
    extra = tmp_path / "bin"
    extra.mkdir()
    monkeypatch.setattr("common.exec._EXTRA_PATH_DIRS", (str(extra),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _augmented_env()
    assert env["PATH"] == f"/usr/bin:/bin{os.pathsep}{extra}"


def test_augmented_env_skips_nonexistent_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr("common.exec._EXTRA_PATH_DIRS", (str(missing),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _augmented_env()
    assert env["PATH"] == "/usr/bin:/bin"


def test_augmented_env_skips_dir_already_on_path(tmp_path, monkeypatch):
    extra = tmp_path / "bin"
    extra.mkdir()
    monkeypatch.setattr("common.exec._EXTRA_PATH_DIRS", (str(extra),))
    monkeypatch.setenv("PATH", f"/usr/bin:{extra}")
    env = _augmented_env()
    assert env["PATH"] == f"/usr/bin:{extra}"


def test_run_uses_augmented_path_for_subprocess(tmp_path, monkeypatch):
    extra = tmp_path / "bin"
    extra.mkdir()
    tool = extra / "only-on-extra-path"
    tool.write_text("#!/bin/sh\necho found\n")
    tool.chmod(0o755)

    monkeypatch.setattr("common.exec._EXTRA_PATH_DIRS", (str(extra),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = run(["bash", "-c", "only-on-extra-path"], check=False)
    assert result.code == 0
    assert result.stdout.strip() == "found"
