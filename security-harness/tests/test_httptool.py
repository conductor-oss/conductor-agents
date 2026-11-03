"""http_request worker + multi-identity resolution."""
import types

from common import auth
from httptool import tasks as ht


def _task(**inp):
    return types.SimpleNamespace(input_data=inp)


def test_resolve_identities_includes_anon_and_each():
    ids = auth.resolve_identities({"identities": [
        {"label": "userA", "auth_token": "ta"},
        {"label": "admin", "auth_token": "tb", "auth_header": "X-Api-Key", "auth_scheme": ""},
    ]})
    assert ids["anon"] == {}
    assert ids["userA"] == {"header": "Authorization", "value": "Bearer ta"}
    assert ids["admin"] == {"header": "X-Api-Key", "value": "tb"}


def test_http_request_refuses_out_of_scope():
    scope = {"in_scope_hosts": ["app.test"]}
    out = ht.http_request(_task(method="GET", url="https://evil.com/x", scope=scope))
    assert out["error"] == "refused: out of scope"
    assert out["response"] == {}


def test_http_request_no_url():
    assert ht.http_request(_task()).get("error") == "no url provided"


def test_http_request_redacts_auth_in_evidence(monkeypatch):
    # stub requests.request so no network call; assert auth header is sent but redacted in evidence
    captured = {}

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = '{"ok":true}'
        content = b'{"ok":true}'
        url = "https://app.test/api/me"
        class elapsed:  # noqa: N801
            @staticmethod
            def total_seconds():
                return 0.01

    def fake_request(method, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _Resp()

    monkeypatch.setattr(ht.requests, "request", fake_request)
    ids = {"userA": {"header": "Authorization", "value": "Bearer SECRET"}}
    out = ht.http_request(_task(method="GET", url="https://app.test/api/me",
                                scope={"in_scope_hosts": ["app.test"]},
                                identities=ids, identity="userA"))
    # auth actually sent on the wire...
    assert captured["headers"]["Authorization"] == "Bearer SECRET"
    # ...but redacted in the recorded evidence
    assert out["request"]["headers"]["Authorization"] == "<redacted>"
    assert out["response"]["status"] == 200
    assert "SECRET" not in str(out)


class _R:
    """Minimal fake requests.Response for redirect tests."""
    def __init__(self, status, headers, url, body=""):
        self.status_code = status
        self.headers = headers
        self.text = body
        self.content = body.encode()
        self.url = url
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in headers
        self.elapsed = types.SimpleNamespace(total_seconds=lambda: 0.01)


def test_http_request_refuses_out_of_scope_redirect(monkeypatch):
    # An in-scope target 302s to an out-of-scope host. The redirect must NOT be followed and the
    # credential must never reach the out-of-scope host.
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
        if "app.test" in url:
            return _R(302, {"Location": "https://evil.com/steal"}, url)
        return _R(200, {"Content-Type": "text/plain"}, url, "pwned")

    monkeypatch.setattr(ht.requests, "request", fake_request)
    ids = {"userA": {"header": "X-Authorization", "value": "SECRETJWT"}}
    out = ht.http_request(_task(method="GET", url="https://app.test/go",
                                scope={"in_scope_hosts": ["app.test"]},
                                identities=ids, identity="userA"))

    assert all("evil.com" not in c["url"] for c in calls), "must not request the out-of-scope host"
    assert "SECRETJWT" not in str(out), "credential must not leak into the result"
    assert "out-of-scope" in (out.get("error") or "")


def test_http_request_follows_in_scope_redirect(monkeypatch):
    # A same-host in-scope redirect IS followed; credentials are preserved on the same host.
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append(url)
        if url.endswith("/start"):
            return _R(302, {"Location": "https://app.test/landing"}, url)
        return _R(200, {"Content-Type": "text/plain"}, url, "final page")

    monkeypatch.setattr(ht.requests, "request", fake_request)
    out = ht.http_request(_task(method="GET", url="https://app.test/start",
                                scope={"in_scope_hosts": ["app.test"]}))
    assert out["response"]["status"] == 200
    assert out["response"]["final_url"].endswith("/landing")
    assert any(u.endswith("/landing") for u in seen)


def test_http_request_strips_credentials_on_cross_host_redirect(monkeypatch):
    # Two in-scope hosts (subdomains allowed): following across hosts is permitted, but the
    # credential is stripped on the host change so it never rides to the second host.
    hdrs_by_host = {}

    def fake_request(method, url, **kwargs):
        host = "a.test" if "a.test" in url else "b.test"
        hdrs_by_host[host] = dict(kwargs.get("headers") or {})
        if host == "a.test":
            return _R(302, {"Location": "https://b.test/next"}, url)
        return _R(200, {"Content-Type": "text/plain"}, url, "ok")

    monkeypatch.setattr(ht.requests, "request", fake_request)
    ids = {"userA": {"header": "X-Authorization", "value": "SECRETJWT"}}
    out = ht.http_request(_task(method="GET", url="https://a.test/start",
                                scope={"in_scope_hosts": ["test"], "allow_subdomains": True},
                                identities=ids, identity="userA"))
    assert "X-Authorization" in hdrs_by_host["a.test"]           # sent to the first host
    assert "X-Authorization" not in hdrs_by_host.get("b.test", {})  # stripped on the hop
    assert out["response"]["status"] == 200
