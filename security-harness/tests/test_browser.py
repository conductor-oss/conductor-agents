"""Browser-driven action capability gate (spec 15.1) in playwright_action.

Only the early-return refusal/scope paths are exercised so no real browser launches.
"""
from browser import tasks as bt


class _Task:
    def __init__(self, data):
        self.input_data = data


def _call(**over):
    base = {"base_url": "http://localhost:3000", "url": "http://localhost:3000/x",
            "scope": {"in_scope_hosts": ["localhost"]}}
    base.update(over)
    return bt.playwright_action(_Task(base))


def test_click_refused_at_capability_1():
    r = _call(action={"type": "click", "selector": "#submit"}, capability_max=1)
    assert "refused: capability" in r["note"]


def test_fill_refused_at_capability_1():
    r = _call(action={"type": "fill", "selector": "#q", "value": "x"}, capability_max=1)
    assert "refused: capability" in r["note"]


def test_out_of_scope_url_refused_before_capability():
    r = _call(url="http://evil.example.org/x",
              action={"type": "click", "selector": "#b"}, capability_max=2)
    assert "out of scope" in r["note"]


def test_missing_capability_defaults_read_only():
    # No capability_max supplied (Conductor sends "" for absent) -> treated as level 1,
    # so a click is refused.
    r = _call(action={"type": "click", "selector": "#b"}, capability_max="")
    assert "refused: capability" in r["note"]


# ── _apply_scoped_auth: credentials must reach in-scope hosts only ──────────────
class _FakeReq:
    def __init__(self, url, headers):
        self.url = url
        self.headers = headers


class _FakeRoute:
    def __init__(self, req):
        self.request = req
        self.continued = None

    def continue_(self, headers=None):
        self.continued = headers


class _FakeRouter:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


_AUTH = {"header": "Authorization", "value": "Bearer SECRET"}
_SCOPE = {"in_scope_hosts": ["app.test"]}


def _drive(url, req_headers):
    router = _FakeRouter()
    bt._apply_scoped_auth(router, _AUTH, _SCOPE)
    route = _FakeRoute(_FakeReq(url, dict(req_headers)))
    router.handler(route)
    return route.continued


def test_scoped_auth_added_for_in_scope_request():
    sent = _drive("https://app.test/api/me", {"user-agent": "x"})
    assert sent["Authorization"] == "Bearer SECRET"


def test_scoped_auth_not_sent_to_out_of_scope_subresource():
    sent = _drive("https://cdn.thirdparty.com/lib.js", {"user-agent": "x"})
    assert "Authorization" not in sent


def test_scoped_auth_strips_stray_credential_off_target():
    # Even if a credential somehow rides on the request, it's stripped for an out-of-scope host.
    sent = _drive("https://evil.example.org/x", {"Authorization": "Bearer SECRET"})
    assert "Authorization" not in sent


def test_scoped_auth_noop_without_credential():
    router = _FakeRouter()
    bt._apply_scoped_auth(router, {}, _SCOPE)
    assert router.handler is None  # no interception registered when there's nothing to attach
