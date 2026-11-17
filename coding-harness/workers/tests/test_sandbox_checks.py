"""Closure plan WP9 (H17): repository checks run OS-sandboxed — writes confined, no internet."""
import os
import subprocess
import sys

import pytest

import common  # noqa: F401
from common import check_execution as ce


def test_profile_never_opens_the_internet_through_a_local_side_filter():
    prof = ce.macos_profile(["/w"])
    assert '(local ip "localhost:*")' not in prof.split("(allow network-bind")[0]
    assert '(allow network-outbound (remote ip "localhost:*"))' in prof
    assert "(deny file-write*)" in prof and '(subpath "/w")' in prof


def test_linux_argv_binds_only_writable_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(ce.sys, "platform", "linux")
    monkeypatch.setattr(ce.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    argv = ce.sandboxed_argv(["pytest", "-q"], cwd=str(tmp_path))
    assert argv[0] == "/usr/bin/bwrap" and "--unshare-net" in argv and argv[-2:] == ["pytest", "-q"]
    assert ["--bind", str(tmp_path), str(tmp_path)] == argv[argv.index(str(tmp_path)) - 1: argv.index(str(tmp_path)) + 2]
    monkeypatch.setattr(ce.shutil, "which", lambda name: None)
    with pytest.raises(ce.SandboxUnavailable):
        ce.sandboxed_argv(["pytest"], cwd=str(tmp_path))


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real macOS sandbox-exec")
def test_real_sandbox_confines_writes_and_network(tmp_path):
    def run(code):
        argv = ce.sandboxed_argv([sys.executable, "-c", code], cwd=str(tmp_path))
        return subprocess.run(argv, cwd=str(tmp_path), capture_output=True, text=True).returncode
    escape = os.path.join(os.path.expanduser("~"), f".sbx-escape-{os.getpid()}")
    assert run("open('inside.txt','w').write('x')") == 0
    assert run(f"open({escape!r},'w').write('x')") != 0 and not os.path.exists(escape)
    assert run("import socket;socket.create_connection(('1.1.1.1',80))") != 0
    assert run("import socket,threading\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\n"
               "t=threading.Thread(target=s.accept);t.start();socket.create_connection(s.getsockname());t.join()") == 0
