"""Automatic halt conditions (spec 15.2)."""
from common import halt
from common import scope as scope_mod


def _scope():
    return scope_mod.derive_scope("https://app.example.com")


def test_no_halt_on_clean_get():
    obs = {"method": "GET", "url": "https://app.example.com/x",
           "final_url": "https://app.example.com/x", "sensitive": {"found": False}}
    assert halt.evaluate(obs, {}, _scope())["halt"] is False


def test_halt_on_forbidden_operation():
    m = {"forbidden_operations": ["DELETE /api/users/*"]}
    obs = {"method": "DELETE", "url": "https://app.example.com/api/users/1",
           "final_url": "https://app.example.com/api/users/1"}
    v = halt.evaluate(obs, m, _scope())
    assert v["halt"] is True
    assert "forbidden" in v["reason"]


def test_halt_on_scope_crossing_redirect():
    obs = {"method": "GET", "url": "https://app.example.com/redir",
           "final_url": "https://evil.example.org/landed", "sensitive": {"found": False}}
    v = halt.evaluate(obs, {}, _scope())
    assert v["halt"] is True
    assert "scope boundary" in v["reason"]


def test_halt_on_unexpected_bulk_sensitive_data():
    obs = {"method": "GET", "url": "https://app.example.com/export",
           "final_url": "https://app.example.com/export",
           "sensitive": {"found": True, "secrets": {"aws_access_key": 3}, "pii": {"email": 4}}}
    v = halt.evaluate(obs, {}, _scope())
    assert v["halt"] is True
    assert "secrets/PII" in v["reason"]


def test_no_halt_when_sensitive_host_is_expected():
    m = {"expected_data_hosts": ["app.example.com"]}
    obs = {"method": "GET", "url": "https://app.example.com/export",
           "final_url": "https://app.example.com/export",
           "sensitive": {"found": True, "secrets": {"jwt": 10}, "pii": {}}}
    assert halt.evaluate(obs, m, _scope())["halt"] is False


def test_in_scope_host_is_an_expected_data_source():
    """An authorized in-scope host returns the user's own data by design (e.g. an admin reading its
    org's users) — bulk PII there must NOT halt; only out-of-scope bulk access does."""
    obs = {"method": "GET", "url": "https://app.example.com/api/organization/users",
           "final_url": "https://app.example.com/api/organization/users",
           "sensitive": {"found": True, "secrets": {}, "pii": {"email": 44}}}
    assert halt.evaluate(obs, {"in_scope_hosts": ["app.example.com"]}, _scope())["halt"] is False
    # same bulk PII surfacing from an out-of-scope host (exfil/SSRF) still halts
    off = {**obs, "url": "https://evil.test/x", "final_url": "https://evil.test/x"}
    assert halt.evaluate(off, {"in_scope_hosts": ["app.example.com"]}, None)["halt"] is True


def test_halt_on_request_budget():
    m = {"rate": {"max_requests": 100}}
    obs = {"method": "GET", "url": "https://app.example.com/x",
           "final_url": "https://app.example.com/x", "sensitive": {"found": False}}
    assert halt.evaluate(obs, m, _scope(), {"requests": 101})["halt"] is True


def test_halt_on_data_volume_budget():
    m = {"data_volume": {"max_bytes": 1000}}
    obs = {"method": "GET", "url": "https://app.example.com/x",
           "final_url": "https://app.example.com/x", "sensitive": {"found": False}}
    assert halt.evaluate(obs, m, _scope(), {"bytes": 2000})["halt"] is True
