"""Which credential each agent backend runs with: a subscription login or an API key (pure).

Every backend CLI accepts both: Claude Code signs in to claude.ai (``claude login``) or takes
``ANTHROPIC_API_KEY``; Codex signs in with a ChatGPT account (``codex login``, ``~/.codex/auth.json``)
or takes ``OPENAI_API_KEY``; the Gemini CLI signs in with Google (``gemini`` then "Login with Google",
``~/.gemini/oauth_creds.json``) or takes ``GEMINI_API_KEY``. Each CLI prefers the key when its
variable is set, so a worker that inherits a stale key from the operator's shell never reaches the
login: the redis program's Architect sessions failed with ``401 Your API key has been invalidated``
while the host's Codex login was fine (run 5456297e).

One setting per backend decides: ``CLAUDE_AUTH`` / ``CODEX_AUTH`` / ``GEMINI_AUTH`` = ``auto`` (the
default: the login when one exists, else the key), ``login`` (only the login) or ``key`` (only the
key). ``child_environment`` applies the decision to the environment a backend's process gets: in
``login`` mode the key variable is blanked, so the CLI uses its stored login. Nothing here reads or
logs a key's value.
"""

from __future__ import annotations

import json
import os

BACKENDS = ("claude", "codex", "gemini")
KEY_VARS = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
MODE_VARS = {"claude": "CLAUDE_AUTH", "codex": "CODEX_AUTH", "gemini": "GEMINI_AUTH"}
MODES = ("auto", "login", "key")
LOGIN_HOWTO = {
    "claude": "run `claude login` (claude.ai account) on the worker host",
    "codex": "run `codex login` (ChatGPT account) on the worker host",
    "gemini": "run `gemini` once on the worker host and choose \"Login with Google\"",
}
KEY_HOWTO = {b: f"set {v} in .env" for b, v in KEY_VARS.items()}


def _home(home: str | None) -> str:
    return os.path.expanduser(home or "~")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def login_present(agent: str, *, home: str | None = None) -> tuple[bool, str]:
    """Whether the backend's CLI holds a subscription login on this host, and where it was seen.
    Presence only: nothing here proves the login still works."""
    h = _home(home)
    if agent == "claude":
        # macOS keeps the token in the keychain; ~/.claude.json records the signed-in account.
        # Linux keeps it in ~/.claude/.credentials.json.
        if _read_json(os.path.join(h, ".claude.json")).get("oauthAccount"):
            return True, "~/.claude.json (oauthAccount)"
        if os.path.isfile(os.path.join(h, ".claude", ".credentials.json")):
            return True, "~/.claude/.credentials.json"
        return False, ""
    if agent == "codex":
        auth = _read_json(os.path.join(h, ".codex", "auth.json"))
        if auth.get("tokens"):
            return True, "~/.codex/auth.json (ChatGPT login)"
        return False, ""
    if agent == "gemini":
        if os.path.isfile(os.path.join(h, ".gemini", "oauth_creds.json")):
            return True, "~/.gemini/oauth_creds.json"
        return False, ""
    return False, ""


def key_present(agent: str, env: dict | None = None, *, home: str | None = None) -> tuple[bool, str]:
    """Whether an API key is configured for the backend, and where (never its value)."""
    env = os.environ if env is None else env
    var = KEY_VARS.get(agent, "")
    if var and str(env.get(var) or "").strip():
        return True, var
    h = _home(home)
    if agent == "codex" and str(_read_json(os.path.join(h, ".codex", "auth.json")).get("OPENAI_API_KEY") or "").strip():
        return True, "~/.codex/auth.json (OPENAI_API_KEY)"
    if agent == "gemini":
        try:
            with open(os.path.join(h, ".gemini", ".env"), encoding="utf-8") as fh:
                if any(line.split("=", 1)[0].strip() == "GEMINI_API_KEY" and line.split("=", 1)[1].strip()
                       for line in fh if "=" in line):
                    return True, "~/.gemini/.env"
        except OSError:
            pass
    return False, ""


def requested_mode(agent: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    mode = str(env.get(MODE_VARS.get(agent, ""), "") or "auto").strip().lower()
    return mode if mode in MODES else "auto"


def decide(agent: str, env: dict | None = None, *, home: str | None = None) -> dict:
    """The credential the backend runs with: ``{"backend", "requested", "mode": login|key|none,
    "login": bool, "key": bool, "source", "detail"}``. ``auto`` prefers the login."""
    login, login_src = login_present(agent, home=home)
    key, key_src = key_present(agent, env, home=home)
    req = requested_mode(agent, env)
    if req == "login":
        mode = "login" if login else "none"
    elif req == "key":
        mode = "key" if key else "none"
    else:
        mode = "login" if login else ("key" if key else "none")
    source = login_src if mode == "login" else (key_src if mode == "key" else "")
    if mode == "none":
        want = {"login": LOGIN_HOWTO.get(agent, ""), "key": KEY_HOWTO.get(agent, "")}
        detail = (f"{MODE_VARS.get(agent)}={req} but no {req} is configured: {want.get(req, '')}" if req != "auto"
                  else f"no login and no key: {LOGIN_HOWTO.get(agent, '')}, or {KEY_HOWTO.get(agent, '')}")
    elif mode == "login" and key:
        detail = f"the login is used; {KEY_VARS[agent]} is withheld from the backend ({MODE_VARS[agent]}=key to use it)"
    elif mode == "key" and login:
        detail = f"the key is used ({MODE_VARS[agent]}=login to use the {login_src})"
    else:
        detail = ""
    return {"backend": agent, "requested": req, "mode": mode, "login": login, "key": key, "source": source,
            "detail": detail}


def child_environment(agent: str, env: dict) -> dict:
    """The environment the backend's process gets. In ``login`` mode the key variable is blanked (the
    CLIs treat a blank as unset and use their stored login); otherwise the environment is unchanged."""
    out = dict(env)
    if decide(agent, env)["mode"] == "login" and KEY_VARS.get(agent):
        out[KEY_VARS[agent]] = ""
    return out


def overrides(agent: str, env: dict | None = None) -> dict:
    """What to merge onto a backend's environment (the Claude SDK merges rather than replaces)."""
    env = os.environ if env is None else env
    return {KEY_VARS[agent]: ""} if decide(agent, env)["mode"] == "login" and KEY_VARS.get(agent) else {}


def extra(agent: str, overrides_in: dict | None = None, env: dict | None = None) -> dict:
    """The overrides a backend's process environment is built with: the caller's own plus the key
    blanked in login mode (for ``check_execution.inherited_environment(...)``)."""
    return {**(overrides_in or {}), **overrides(agent, env)}


def ensure_codex_key(env: dict | None = None, *, home: str | None = None, runner=None) -> dict:
    """Codex takes an API key only through its own login store (``codex login --with-api-key``), not
    from the environment: with ``OPENAI_API_KEY`` set and no ``~/.codex/auth.json`` every request went
    out with no credential at all ("Missing bearer or basic authentication in header", run 9b8d3b39).
    In key mode, register the environment's key with the CLI once (idempotent: skipped when the store
    already holds it). The key value is passed on stdin and never logged. Returns what was done."""
    env = os.environ if env is None else env
    d = decide("codex", env, home=home)
    if d["mode"] != "key":
        return {"registered": False, "reason": f"codex runs in {d['mode']} mode"}
    key = str(env.get(KEY_VARS["codex"]) or "").strip()
    if not key:
        return {"registered": False, "reason": "the key is in ~/.codex/auth.json already"}
    stored = str(_read_json(os.path.join(_home(home), ".codex", "auth.json")).get("OPENAI_API_KEY") or "")
    if stored == key:
        return {"registered": False, "reason": "codex already holds this key"}
    if runner is None:
        def runner(argv, stdin_text):  # noqa: E306
            # Through the shared runtime (stdin is closed there): the key reaches the CLI on a pipe
            # from an environment variable of this one child, never as an argument or a log line.
            from common import check_execution
            env = check_execution.inherited_environment({"HARNESS_CODEX_KEY": stdin_text.strip(),
                                                         "HARNESS_CODEX_BIN": argv[0]})
            res = check_execution.execute(
                ["sh", "-c", 'printf "%s\\n" "$HARNESS_CODEX_KEY" | "$HARNESS_CODEX_BIN" login --with-api-key'],
                cwd=_home(home), env=env)
            return res.exit_code, (res.stdout or "") + (res.stderr or "")
    try:
        code, out = runner([os.environ.get("CODEX_BIN", "codex"), "login", "--with-api-key"], key + "\n")
    except OSError as e:
        return {"registered": False, "reason": f"codex login --with-api-key could not run: {e}"}
    if code != 0:
        return {"registered": False, "reason": f"codex login --with-api-key exited {code}: {out.strip()[-200:]}"}
    return {"registered": True, "reason": "OPENAI_API_KEY registered with the codex CLI's login store"}


def report(env: dict | None = None, *, home: str | None = None) -> list[dict]:
    return [decide(b, env, home=home) for b in BACKENDS]


def report_lines(env: dict | None = None, *, home: str | None = None) -> list[str]:
    """One line per backend for a log or a terminal, never a key's value."""
    out = []
    for r in report(env, home=home):
        how = {"login": f"login ({r['source']})", "key": f"API key ({r['source']})", "none": "NOT CONFIGURED"}[r["mode"]]
        out.append(f"{r['backend']:7} {how}" + (f" — {r['detail']}" if r["detail"] else ""))
    return out
