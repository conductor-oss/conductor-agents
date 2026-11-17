"""Each backend runs with a subscription login or an API key, as CLAUDE_AUTH / CODEX_AUTH / GEMINI_AUTH
decide; a login is preferred, and in login mode the key is withheld from the backend's process."""
import json
import os

import common  # noqa: F401 -- sys.path shim
from common import backend_auth as ba


def _home(tmp_path, *, claude=False, codex_login=False, codex_key=False, gemini_login=False, gemini_env_key=False):
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    if claude:
        (h / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "x@y"}}))
    if codex_login or codex_key:
        (h / ".codex").mkdir(exist_ok=True)
        (h / ".codex" / "auth.json").write_text(json.dumps(
            {**({"tokens": {"access_token": "t"}} if codex_login else {}), **({"OPENAI_API_KEY": "sk"} if codex_key else {})}))
    if gemini_login:
        (h / ".gemini").mkdir(exist_ok=True)
        (h / ".gemini" / "oauth_creds.json").write_text("{}")
    if gemini_env_key:
        (h / ".gemini").mkdir(exist_ok=True)
        (h / ".gemini" / ".env").write_text("GEMINI_API_KEY=abc\n")
    return str(h)


def test_auto_prefers_the_login_and_withholds_the_key(tmp_path):
    home = _home(tmp_path, claude=True, codex_login=True, gemini_login=True)
    env = {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o", "GEMINI_API_KEY": "g"}
    for b in ba.BACKENDS:
        d = ba.decide(b, env, home=home)
        assert d["mode"] == "login" and d["login"] and d["key"] and "withheld" in d["detail"], b
    assert ba.decide("claude", env, home=home)["source"] == "~/.claude.json (oauthAccount)"
    assert ba.decide("codex", env, home=home)["source"] == "~/.codex/auth.json (ChatGPT login)"


def test_key_is_used_when_no_login_exists_or_when_asked(tmp_path):
    home = _home(tmp_path, codex_key=True, gemini_env_key=True)
    env = {"ANTHROPIC_API_KEY": "sk-a"}
    assert ba.decide("claude", env, home=home) | {} == {**ba.decide("claude", env, home=home)}
    assert ba.decide("claude", env, home=home)["mode"] == "key" and ba.decide("claude", env, home=home)["source"] == "ANTHROPIC_API_KEY"
    assert ba.decide("codex", env, home=home)["mode"] == "key" and "auth.json" in ba.decide("codex", env, home=home)["source"]
    assert ba.decide("gemini", env, home=home)["mode"] == "key" and ba.decide("gemini", env, home=home)["source"] == "~/.gemini/.env"
    # Asked for the key while a login exists: the key, and the detail says how to switch.
    home2 = _home(tmp_path / "b", claude=True)
    d = ba.decide("claude", {"ANTHROPIC_API_KEY": "sk", "CLAUDE_AUTH": "key"}, home=home2)
    assert d["mode"] == "key" and "CLAUDE_AUTH=login" in d["detail"]


def test_nothing_configured_or_a_mode_without_its_credential_is_named(tmp_path):
    home = _home(tmp_path)
    d = ba.decide("codex", {}, home=home)
    assert d["mode"] == "none" and "codex login" in d["detail"] and "OPENAI_API_KEY" in d["detail"]
    d = ba.decide("claude", {"ANTHROPIC_API_KEY": "sk", "CLAUDE_AUTH": "login"}, home=home)
    assert d["mode"] == "none" and "CLAUDE_AUTH=login but no login" in d["detail"]
    assert ba.requested_mode("gemini", {"GEMINI_AUTH": "whatever"}) == "auto"


def test_child_environment_blanks_the_key_only_in_login_mode(tmp_path, monkeypatch):
    home = _home(tmp_path, claude=True, codex_login=True)
    monkeypatch.setenv("HOME", home)
    env = {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o", "GEMINI_API_KEY": "g", "PATH": "/bin"}
    out = ba.child_environment("codex", env)
    assert out["OPENAI_API_KEY"] == "" and out["ANTHROPIC_API_KEY"] == "sk-a" and out["PATH"] == "/bin"
    assert ba.child_environment("gemini", env)["GEMINI_API_KEY"] == "g"  # no Google login: the key stays
    assert ba.overrides("claude", env) == {"ANTHROPIC_API_KEY": ""}
    assert ba.overrides("claude", {**env, "CLAUDE_AUTH": "key"}) == {}
    assert env["OPENAI_API_KEY"] == "sk-o"  # the caller's environment is untouched


def test_report_never_shows_a_key_value(tmp_path):
    home = _home(tmp_path, claude=True)
    lines = ba.report_lines({"ANTHROPIC_API_KEY": "sk-secret-value", "OPENAI_API_KEY": "sk-other"}, home=home)
    text = "\n".join(lines)
    assert "sk-secret-value" not in text and "sk-other" not in text
    assert lines[0].startswith("claude  login") and "withheld" in lines[0]
    assert lines[1].startswith("codex   API key (OPENAI_API_KEY)") and lines[2].startswith("gemini  NOT CONFIGURED")


def test_drivers_pass_the_decided_environment():
    src = os.path.join(os.path.dirname(__file__), "..", "common")
    codex = open(os.path.join(src, "codex.py")).read()
    gemini = open(os.path.join(src, "gemini.py")).read()
    agent = open(os.path.join(src, "coding_agent.py")).read()
    assert codex.count('backend_auth.extra("codex", extra_env)') == 2  # the SDK client and the CLI fallback
    assert 'backend_auth.extra("gemini")' in gemini
    assert ba.extra("codex", {"GOCACHE": "/c"}, {"OPENAI_API_KEY": "sk", "CODEX_AUTH": "key"}) == {"GOCACHE": "/c"}
    assert 'backend_auth.overrides("claude")' in agent


def test_codex_key_is_registered_with_the_cli_store_once(tmp_path, monkeypatch):
    """Codex ignores OPENAI_API_KEY in the environment and sent requests with no credential at all
    (run 9b8d3b39): in key mode the key is handed to `codex login --with-api-key` on stdin, once."""
    home = _home(tmp_path)
    calls = []

    def runner(argv, stdin_text):
        calls.append((argv[-2:], stdin_text.strip()))
        (tmp_path / "home" / ".codex").mkdir(exist_ok=True)
        (tmp_path / "home" / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": stdin_text.strip()}))
        return 0, ""

    env = {"OPENAI_API_KEY": "sk-test"}
    out = ba.ensure_codex_key(env, home=home, runner=runner)
    assert out["registered"] is True and calls == [(["login", "--with-api-key"], "sk-test")]
    assert ba.ensure_codex_key(env, home=home, runner=runner)["registered"] is False  # idempotent
    assert len(calls) == 1
    # Login mode never touches the store; a failing login is reported, never raised.
    home2 = _home(tmp_path / "b", codex_login=True)
    assert ba.ensure_codex_key({"OPENAI_API_KEY": "sk"}, home=home2, runner=runner)["registered"] is False
    assert "exited 3" in ba.ensure_codex_key({"OPENAI_API_KEY": "sk-other"}, home=home, runner=lambda a, s: (3, "boom"))["reason"]
    assert "sk-test" not in json.dumps(ba.report(env, home=home))
