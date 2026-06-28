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
