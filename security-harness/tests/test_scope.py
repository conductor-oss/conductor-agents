"""Scope enforcement is the safety core — test it thoroughly."""
import pytest

from common import scope


def test_derive_scope_host_only():
    s = scope.derive_scope("http://localhost:3001/foo?x=1")
    assert s["in_scope_hosts"] == ["localhost"]
    assert s["allow_subdomains"] is False


def test_in_scope_same_host():
    s = scope.derive_scope("https://app.example.com")
    assert scope.in_scope("https://app.example.com/a/b", s) is True
    assert scope.in_scope("https://app.example.com:8443/x", s) is True  # port ignored


def test_out_of_scope_other_host():
    s = scope.derive_scope("https://app.example.com")
    assert scope.in_scope("https://evil.com/x", s) is False
    assert scope.in_scope("https://notapp.example.com/x", s) is False


def test_subdomains_gated():
    s = scope.derive_scope("https://example.com", allow_subdomains=True)
    assert scope.in_scope("https://api.example.com/x", s) is True
    assert scope.in_scope("https://example.com.evil.com/x", s) is False


def test_exclude_patterns():
    s = scope.normalize_scope(
        {"in_scope_hosts": ["app.example.com"], "exclude_patterns": ["/logout", "signout"]},
        "https://app.example.com")
    assert scope.in_scope("https://app.example.com/account", s) is True
    assert scope.in_scope("https://app.example.com/logout", s) is False
    assert scope.in_scope("https://app.example.com/auth/signout", s) is False


def test_enforce_raises_out_of_scope():
    s = scope.derive_scope("https://app.example.com")
    assert scope.enforce("https://app.example.com/x", s) == "https://app.example.com/x"
    with pytest.raises(scope.OutOfScopeError):
        scope.enforce("https://evil.com/x", s)


def test_normalize_scope_fills_defaults():
    s = scope.normalize_scope(None, "https://x.test")
    assert s["in_scope_hosts"] == ["x.test"]
    assert s["allow_subdomains"] is False
    assert s["exclude_patterns"] == []
