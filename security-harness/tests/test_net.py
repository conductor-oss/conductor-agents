"""reachable_url rewrites localhost only inside a container."""
from common import net


def test_no_rewrite_on_host(monkeypatch):
    monkeypatch.delenv("SC_IN_DOCKER", raising=False)
    assert net.reachable_url("http://localhost:3001/x") == "http://localhost:3001/x"


def test_rewrite_localhost_in_docker(monkeypatch):
    monkeypatch.setenv("SC_IN_DOCKER", "1")
    assert net.reachable_url("http://localhost:3001/x?q=1") == \
        "http://host.docker.internal:3001/x?q=1"
    assert net.reachable_url("http://127.0.0.1:8080/a") == \
        "http://host.docker.internal:8080/a"


def test_no_rewrite_external_in_docker(monkeypatch):
    monkeypatch.setenv("SC_IN_DOCKER", "1")
    url = "https://app.example.com/path?x=1"
    assert net.reachable_url(url) == url
