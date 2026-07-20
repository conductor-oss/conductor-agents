"""GitHub operations via the `gh` CLI.

Remote transport (clone/fetch/pull/push) lives in ``common/git.py`` and rides on
gh's git-credential helper; this module covers the GitHub-specific PR surface —
create, checkout, status, comment, merge — all through `gh`, which is already
authenticated on the worker host (`gh auth login` / `GH_TOKEN`). Every helper
shells through ``common/exec.run`` (stdin closed, captured, timeout) and returns
plain dicts for the worker layer to wrap.

Auth model: ``ensure_git_auth()`` runs `gh auth setup-git` once per process so
plain `git` over HTTPS uses gh's credentials — no token juggling in URLs. It is a
no-op (logged) when gh isn't authenticated, so local-only flows still work.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

from .exec import run

log = logging.getLogger("gitops.github")

_AUTH_LOCK = threading.Lock()
_AUTH_DONE = False

# Login of the authenticated gh user, resolved once per process (like _AUTH_DONE).
_VIEWER_LOCK = threading.Lock()
_VIEWER_LOGIN: str | None = None
_VIEWER_CACHED = False

# Invisible marker appended to every harness-posted PR comment. Lets pr_comments
# skip the harness's own comments so the review-feedback loop is safely re-runnable
# (the bot posts under the same account as humans, so author can't distinguish it).
HARNESS_MARKER = "<!-- conductor-harness -->"

# Default location (under the checkout) for the rendered review markdown, so the
# review output is never lost regardless of which posting path is taken.
DEFAULT_REVIEW_OUTPUT_PATH = ".conductor/review-output.md"

# gh/GitHub 422 substrings that mean "you can't submit a formal review of your own
# PR" — matched case-insensitively to trigger the comments-API fallback.
SELF_REVIEW_422_HINTS = ("own pull request", "review cannot be submitted")


def ensure_git_auth() -> bool:
    """Configure git to use gh as its credential helper (once per process).
    Returns True if gh auth is available. Safe/idempotent; never raises."""
    global _AUTH_DONE
    with _AUTH_LOCK:
        if _AUTH_DONE:
            return True
        status = run(["gh", "auth", "status"], check=False)
        if status.code != 0:
            log.warning("gh not authenticated (%s); remote GitHub ops will fail",
                        (status.stderr or status.stdout).strip()[:200])
            return False
        setup = run(["gh", "auth", "setup-git"], check=False)
        if setup.code != 0:
            log.warning("gh auth setup-git failed: %s",
                        (setup.stderr or setup.stdout).strip()[:200])
            return False
        _AUTH_DONE = True
        return True


def _gh(repo: str, *args: str, check: bool = True):
    """Run a `gh` command scoped to a repo working dir (gh infers the repo/remote
    from the checkout's origin)."""
    return run(["gh", *args], cwd=repo, check=check)


def repo_slug(repo_or_url: str) -> str:
    """Normalize a repo URL or slug to `owner/name` (for `gh --repo`).
    Accepts https://github.com/o/n[.git], git@github.com:o/n.git, or o/n."""
    s = repo_or_url.strip()
    if s.startswith("git@"):
        s = s.split(":", 1)[-1]
    elif "://" in s:
        s = s.split("://", 1)[-1].split("/", 1)[-1]
    if s.endswith(".git"):
        s = s[:-4]
    return s.strip("/")


def clone_url(repo_or_url: str) -> str:
    """Return something `git clone` accepts. A bare `owner/name` slug (which the gh-based
    tasks accept, but `git clone` does not) is expanded to an https GitHub URL; an existing
    URL, scp-style `git@…`, or local path is passed through unchanged."""
    s = (repo_or_url or "").strip()
    if not s:
        return s
    # already a clone target: URL scheme, scp-style, or a filesystem path
    if "://" in s or s.startswith("git@") or s.startswith(("/", ".", "~")):
        return s
    return f"https://github.com/{repo_slug(s)}.git"


def issue_fetch(repo_or_url: str, number: int) -> dict:
    """Fetch a GitHub issue's title/body/state/labels via `gh issue view`.
    Repo-scoped by slug so it works before any clone exists."""
    ensure_git_auth()
    slug = repo_slug(repo_or_url)
    r = run(["gh", "issue", "view", str(number), "--repo", slug,
             "--json", "number,title,body,state,url,labels"], check=True)
    d = json.loads(r.stdout or "{}")
    return {
        "number": d.get("number", number),
        "title": d.get("title", ""),
        "body": d.get("body", ""),
        "state": d.get("state"),
        "url": d.get("url"),
        "labels": [lb.get("name") for lb in (d.get("labels") or [])],
    }


_PR_META_FIELDS = ("number,title,headRefName,baseRefName,url,"
                   "headRepositoryOwner,headRepository,comments,reviews")


def _keep(body: str) -> bool:
    """A comment is actionable feedback only if it has text and is NOT one the
    harness itself posted (identified by HARNESS_MARKER)."""
    return bool((body or "").strip()) and HARNESS_MARKER not in (body or "")


def pr_comments(repo_or_url: str, number: int) -> dict:
    """Gather + consolidate a PR's review feedback from all three GitHub surfaces —
    conversation comments, formal reviews, and inline file/line review threads —
    skipping the harness's own comments. Returns PR metadata (for cloning/checkout)
    plus a single consolidated ``feedback`` markdown blob for the coding agent."""
    ensure_git_auth()
    slug = repo_slug(repo_or_url)
    # Repo-scoped via --repo (from a neutral cwd) so this works before any clone exists.
    mr = run(["gh", "pr", "view", str(number), "--repo", slug,
              "--json", _PR_META_FIELDS], check=True)
    meta = json.loads(mr.stdout or "{}")

    conv = [((c.get("author") or {}).get("login", "?"), c.get("body", ""))
            for c in (meta.get("comments") or []) if _keep(c.get("body", ""))]
    reviews = [((r.get("author") or {}).get("login", "?"), r.get("state", ""), r.get("body", ""))
               for r in (meta.get("reviews") or []) if _keep(r.get("body", ""))]
    inline_raw = run(["gh", "api", f"repos/{slug}/pulls/{number}/comments"],
                     check=False).stdout
    try:
        inline = [((c.get("user") or {}).get("login", "?"), c.get("path", ""),
                   c.get("line") or c.get("original_line"), c.get("body", ""))
                  for c in json.loads(inline_raw or "[]") if _keep(c.get("body", ""))]
    except ValueError:
        inline = []

    sections: list[str] = []
    if conv:
        sections.append("## Conversation comments\n" +
                        "\n".join(f"- @{a}: {b.strip()}" for a, b in conv))
    if reviews:
        sections.append("## Reviews\n" +
                        "\n".join(f"- @{a} ({s or 'COMMENT'}): {b.strip()}"
                                  for a, s, b in reviews))
    if inline:
        sections.append("## Inline comments\n" +
                        "\n".join(f"- `{p}`" + (f":{ln}" if ln else "") +
                                  f" — @{a}: {b.strip()}" for a, p, ln, b in inline))
    feedback = "\n\n".join(sections)
    count = len(conv) + len(reviews) + len(inline)

    owner = (meta.get("headRepositoryOwner") or {}).get("login", "")
    name = (meta.get("headRepository") or {}).get("name", "")
    head_repo_url = f"https://github.com/{owner}/{name}.git" if owner and name else ""

    return {
        "number": meta.get("number", number),
        "title": meta.get("title", ""),
        "head": meta.get("headRefName", ""),
        "base": meta.get("baseRefName", ""),
        "url": meta.get("url", ""),
        "headRepoUrl": head_repo_url,
        "feedback": feedback,
        "commentCount": count,
        "hasFeedback": count > 0,
    }


def pr_create(repo: str, *, title: str, body: str = "", base: str | None = None,
              head_branch: str | None = None, draft: bool = False,
              fill: bool = False) -> dict:
    """Open a PR from the current (or ``head_branch``) branch. Returns the PR
    number + URL. ``fill`` derives title/body from commits when set."""
    ensure_git_auth()
    args = ["pr", "create"]
    # No explicit title → derive title/body from the commits (gh --fill).
    if fill or not title:
        args.append("--fill")
    else:
        args += ["--title", title, "--body", body or title]
    if base:
        args += ["--base", base]
    if head_branch:
        args += ["--head", head_branch]
    if draft:
        args.append("--draft")
    r = _gh(repo, *args)
    url = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
    number = None
    if "/pull/" in url:
        try:
            number = int(url.rsplit("/pull/", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            number = None
    return {"created": True, "number": number, "url": url, "draft": draft}


def pr_checkout(repo: str, number: int, *, branch: str | None = None,
                force: bool = False) -> dict:
    """Check out an existing PR by number into ``repo`` so the harness can iterate
    on it. Returns the local branch + head."""
    ensure_git_auth()
    args = ["pr", "checkout", str(number)]
    if branch:
        args += ["--branch", branch]
    if force:
        args.append("--force")
    _gh(repo, *args)
    from . import git as _git
    return {"number": number, "branch": _git._current_branch(repo), "head": _git.head(repo)}


_STATUS_FIELDS = "number,state,mergeable,reviewDecision,title,url,headRefName,baseRefName"


def pr_status(repo: str, number: int | None = None) -> dict:
    """Read a PR's review/merge state + CI checks. ``number`` optional — gh infers
    it from the current branch when omitted."""
    ensure_git_auth()
    view_args = ["pr", "view"]
    if number is not None:
        view_args.append(str(number))
    view_args += ["--json", _STATUS_FIELDS]
    vr = _gh(repo, *view_args)
    try:
        view = json.loads(vr.stdout or "{}")
    except ValueError:
        view = {}

    check_args = ["pr", "checks"]
    if number is not None:
        check_args.append(str(number))
    check_args += ["--json", "name,state,bucket,link"]
    # `gh pr checks` exits non-zero when checks are failing/pending — read regardless.
    cr = _gh(repo, *check_args, check=False)
    try:
        checks = json.loads(cr.stdout or "[]")
    except ValueError:
        checks = []
    buckets: dict[str, int] = {}
    for c in checks:
        b = (c.get("bucket") or c.get("state") or "unknown").lower()
        buckets[b] = buckets.get(b, 0) + 1
    return {
        "number": view.get("number", number),
        "state": view.get("state"),
        "mergeable": view.get("mergeable"),
        "reviewDecision": view.get("reviewDecision"),
        "title": view.get("title"),
        "url": view.get("url"),
        "headRefName": view.get("headRefName"),
        "baseRefName": view.get("baseRefName"),
        "checks": [{"name": c.get("name"), "bucket": c.get("bucket") or c.get("state")}
                   for c in checks],
        "passing": buckets.get("pass", 0),
        "failing": buckets.get("fail", 0),
        "pending": buckets.get("pending", 0),
    }


def pr_comment(repo: str, number: int, body: str) -> dict:
    """Post a comment on a PR. Always appends HARNESS_MARKER (invisible in rendered
    markdown) so pr_comments can recognize and skip harness-authored comments."""
    ensure_git_auth()
    tagged = f"{body}\n\n{HARNESS_MARKER}" if HARNESS_MARKER not in body else body
    r = _gh(repo, "pr", "comment", str(number), "--body", tagged)
    url = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
    return {"commented": True, "number": number, "url": url}


def pr_merge(repo: str, number: int, *, method: str = "squash",
             delete_branch: bool = True, auto: bool = False) -> dict:
    """Merge a PR. ``method`` = squash|rebase|merge. ``auto`` enables
    merge-when-ready (waits for required checks). Idempotent-ish: gh errors if the
    PR is already merged — surfaced as a failure by the caller."""
    ensure_git_auth()
    flag = {"squash": "--squash", "rebase": "--rebase", "merge": "--merge"}.get(method, "--squash")
    args = ["pr", "merge", str(number), flag]
    if delete_branch:
        args.append("--delete-branch")
    if auto:
        args.append("--auto")
    _gh(repo, *args)
    return {"merged": True, "number": number, "method": method, "auto": auto}


_DIFF_CAP_DEFAULT = 200_000  # chars — keep a huge PR from blowing up the review prompt


def _diff_cap() -> int:
    """Read the diff-truncation cap from ``PR_DIFF_CAP`` (chars). Non-numeric or
    non-positive values fall back to the 200_000 default."""
    raw = os.environ.get("PR_DIFF_CAP")
    if not raw:
        return _DIFF_CAP_DEFAULT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _DIFF_CAP_DEFAULT
    return val if val > 0 else _DIFF_CAP_DEFAULT


_DIFF_CAP = _diff_cap()


def _local_pr_diff(repo_path: str, base: str | None) -> tuple[str, list[str]]:
    """Compute a PR's diff from a local checkout — the fallback when GitHub's compare
    API can't serve it (`gh pr diff` returns HTTP 406 on PRs over 300 files). HEAD is
    the PR tip (pr_checkout ran first); we diff against the merge-base with ``base``
    (three-dot, matching what GitHub shows), fetching the base ref if needed."""
    from . import git as _git
    ref = f"origin/{base}" if base else "origin/HEAD"
    if base:
        _git.git(repo_path, "fetch", "--quiet", "origin", base, check=False)
    if _git.git(repo_path, "rev-parse", "--verify", ref, check=False).code != 0:
        # base remote-tracking ref missing; FETCH_HEAD (from the fetch above) is the base tip
        ref = "FETCH_HEAD" if _git.git(
            repo_path, "rev-parse", "--verify", "FETCH_HEAD", check=False).code == 0 else (base or ref)

    def _diff(*spec: str) -> tuple[str, list[str]]:
        d = _git.git(repo_path, "diff", *spec, check=False).stdout or ""
        n = _git.git(repo_path, "diff", "--name-only", *spec, check=False).stdout or ""
        return d, [f for f in n.splitlines() if f.strip()]

    diff, files = _diff(f"{ref}...HEAD")          # three-dot: changes since the merge base
    if not diff.strip():                          # no merge base found → plain two-endpoint diff
        diff, files = _diff(ref, "HEAD")
    return diff, files


def pr_diff(repo_or_url: str, number: int, repo_path: str | None = None) -> dict:
    """Return a PR's unified diff (capped) + the list of changed files, to feed the
    read-only reviewer. Prefers `gh` (needs no clone); on large PRs `gh pr diff` 406s
    (>300 files), so when a local checkout is supplied we fall back to a git diff."""
    ensure_git_auth()
    slug = repo_slug(repo_or_url)

    # File list + base branch via the files API (`gh pr view --json files`) — it
    # paginates and handles large PRs, unlike `gh pr diff --name-only`.
    base, files = None, []
    vr = run(["gh", "pr", "view", str(number), "--repo", slug,
              "--json", "files,baseRefName"], check=False)
    if vr.code == 0:
        try:
            meta = json.loads(vr.stdout or "{}")
            base = meta.get("baseRefName")
            files = [f.get("path") for f in (meta.get("files") or []) if f.get("path")]
        except ValueError:
            pass

    dr = run(["gh", "pr", "diff", str(number), "--repo", slug], check=False)
    if dr.code == 0 and (dr.stdout or "").strip():
        diff, source = dr.stdout, "gh"
    elif repo_path:
        diff, local_files = _local_pr_diff(repo_path, base)
        files = local_files or files          # local list is authoritative for a local diff
        source = "local"
    else:
        detail = (dr.stderr or dr.stdout or "").strip()[:200]
        raise RuntimeError(
            f"pr_diff: gh pr diff {number} --repo {slug} exited {dr.code}: {detail}; "
            "no local checkout (repoPath) to fall back to")

    cap = _diff_cap()
    truncated = len(diff) > cap
    if truncated:
        diff = diff[:cap] + "\n…[diff truncated]"
    return {"diff": diff, "changedFiles": files, "truncated": truncated, "diffSource": source}


def viewer_login() -> str | None:
    """Login of the authenticated gh user (`gh api user --jq .login`).

    Cached per process (like ``_AUTH_DONE``): the second call issues no extra gh
    call. Returns None when gh is unauthenticated / the call fails. Never raises."""
    global _VIEWER_LOGIN, _VIEWER_CACHED
    with _VIEWER_LOCK:
        if _VIEWER_CACHED:
            return _VIEWER_LOGIN
        login: str | None = None
        try:
            r = run(["gh", "api", "user", "--jq", ".login"], check=False)
            if r.code == 0:
                login = (r.stdout or "").strip() or None
        except Exception as e:  # noqa: BLE001 — degrade to the reviews-API path
            log.warning("viewer_login failed: %s", str(e)[:200])
            login = None
        _VIEWER_LOGIN = login
        _VIEWER_CACHED = True
        return _VIEWER_LOGIN


def pr_author_login(repo_or_url: str, number: int) -> str | None:
    """PR author's login via `gh pr view <n> --repo <slug> --json author`.
    Returns None on any error. Never raises."""
    try:
        slug = repo_slug(repo_or_url)
        r = run(["gh", "pr", "view", str(number), "--repo", slug,
                 "--json", "author"], check=False)
        if r.code != 0:
            return None
        d = json.loads(r.stdout or "{}") or {}
        return ((d.get("author") or {}).get("login") or "").strip() or None
    except Exception as e:  # noqa: BLE001 — degrade to the reviews-API path
        log.warning("pr_author_login failed: %s", str(e)[:200])
        return None


def _verdict_badge(verdict: str) -> str:
    """Emoji + label for the review verdict used in the markdown H2 title."""
    return "🔧 request changes" if (verdict or "").upper() == "REQUEST_CHANGES" else "✅ comment"


def _anchor(comment: dict) -> str:
    """`path:line` when the comment anchors to an integer line, else `path`."""
    path = comment.get("path") or ""
    line = comment.get("line")
    try:
        line_int = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_int = None
    return f"{path}:{line_int}" if (path and line_int is not None) else path


def render_review_markdown(summary: str, verdict: str,
                           comments: list | None) -> str:
    """Render the whole review as one markdown blob (reused for the fallback
    conversation comment body and the local file): an H2 title with the verdict
    badge, the summary, an optional '### Inline findings' bullet list, and a
    trailing HARNESS_MARKER so the same blob is safe to post as a comment."""
    body = (summary or "").strip() or "No summary provided."
    blocks = [f"## Automated review — {_verdict_badge(verdict)}", body]
    items = comments or []
    if items:
        bullets = "\n".join(f"- `{_anchor(c)}` — {c.get('body', '')}" for c in items)
        blocks.append("### Inline findings\n\n" + bullets)
    blocks.append(HARNESS_MARKER)
    return "\n\n".join(blocks)


def _post_inline_comment(slug: str, number: int, payload: dict) -> None:
    """POST one inline review comment to `repos/{slug}/pulls/{n}/comments`.
    Raises (RunError) if gh exits non-zero, so the caller can fold the finding."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        run(["gh", "api", f"repos/{slug}/pulls/{number}/comments",
             "--method", "POST", "--input", path], check=True)
    finally:
        os.unlink(path)


def post_review_comments(repo_or_url: str, number: int, *, summary: str,
                         verdict: str, comments: list | None = None) -> dict:
    """Post a review through the COMMENTS API (no formal review submission) — the
    self-review fallback. Each anchorable finding becomes an inline review comment
    (commit_id = head sha, side=RIGHT); findings that can't be posted inline are
    folded into one conversation summary comment via ``pr_comment``."""
    ensure_git_auth()
    slug = repo_slug(repo_or_url)
    ev = (verdict or "COMMENT").upper()
    if ev not in ("COMMENT", "REQUEST_CHANGES"):
        ev = "COMMENT"  # never APPROVE from the bot
    items = comments or []

    # Head sha anchors inline comments; without it, fold every finding.
    head_sha = ""
    vr = run(["gh", "pr", "view", str(number), "--repo", slug,
              "--json", "headRefOid"], check=False)
    if vr.code == 0:
        try:
            head_sha = (json.loads(vr.stdout or "{}") or {}).get("headRefOid") or ""
        except ValueError:
            head_sha = ""

    inline_count = 0
    folded: list = []
    for c in items:
        path = c.get("path")
        line = c.get("line")
        try:
            line_int = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_int = None
        if path and line_int is not None and head_sha:
            payload = {"commit_id": head_sha, "path": path, "line": line_int,
                       "side": "RIGHT",
                       "body": (c.get("body", "") + "\n\n" + HARNESS_MARKER)}
            try:
                _post_inline_comment(slug, number, payload)
                inline_count += 1
                continue
            except Exception as e:  # noqa: BLE001 — line not in diff etc.; fold it
                log.warning("inline comment on %s rejected (%s); folding into summary",
                            _anchor(c), str(e)[:200])
        folded.append(c)

    conv = pr_comment(slug, number, render_review_markdown(summary, ev, folded))
    return {"reviewed": True, "mode": "comments", "selfReview": True, "event": ev,
            "inlineCount": inline_count, "inline": inline_count > 0,
            "url": conv.get("url", ""), "localOutputPath": ""}


def submit_review(repo_or_url: str, number: int, *, summary: str,
                  event: str = "COMMENT", comments: list | None = None,
                  reviewer: str | None = None,
                  local_output_path: str | None = None,
                  repo_path: str | None = None) -> dict:
    """Post a PR review (inline comments + summary + verdict). ``event`` is clamped
    to COMMENT / REQUEST_CHANGES (never APPROVE).

    Self-review safe: GitHub 422s a formal review of your own PR. When the reviewer
    (``reviewer`` override, else ``viewer_login()``) equals the PR author, or the
    reviews API returns a self-review 422 anyway, we fall back to the COMMENTS API
    (see ``post_review_comments``). Otherwise the formal reviews-API path is used,
    with the existing inline-drop summary-only retry for line-not-in-diff 422s.

    ``local_output_path`` (resolved relative to ``repo_path`` when given) receives
    the rendered markdown before any posting, so the review is never lost.
    """
    ensure_git_auth()
    slug = repo_slug(repo_or_url)
    ev = (event or "COMMENT").upper()
    if ev not in ("COMMENT", "REQUEST_CHANGES"):
        ev = "COMMENT"  # never APPROVE from the bot
    items = comments or []

    md = render_review_markdown(summary, ev, items)

    # Optional local file — written before any posting, errors logged not raised.
    written_path = ""
    if local_output_path:
        try:
            target = local_output_path
            if repo_path and not os.path.isabs(target):
                target = os.path.join(repo_path, target)
            target = os.path.abspath(target)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(md)
            written_path = target
        except Exception as e:  # noqa: BLE001 — don't lose the review over a bad path
            log.warning("failed to write review output to %s: %s",
                        local_output_path, str(e)[:200])
            written_path = ""

    def _comments_fallback() -> dict:
        result = post_review_comments(repo_or_url, number, summary=summary,
                                      verdict=ev, comments=items)
        result["selfReview"] = True
        result["localOutputPath"] = written_path
        return result

    # Detect self-review up front — both logins must be known and equal.
    reviewer_login = reviewer if reviewer is not None else viewer_login()
    author = pr_author_login(repo_or_url, number)
    self_review = bool(reviewer_login and author and
                       reviewer_login.strip().lower() == author.strip().lower())
    if self_review:
        return _comments_fallback()

    def _inline(c: dict) -> dict | None:
        """Build one inline-comment payload, or None if it can't anchor. A non-integer
        or None ``line`` is skipped rather than raising, so one bad comment doesn't
        sink the whole review."""
        if not (c.get("path") and c.get("line")):
            return None
        try:
            line = int(c["line"])
        except (TypeError, ValueError):
            return None
        return {"path": c["path"], "line": line, "side": "RIGHT",
                "body": c.get("body", "")}

    inline = [p for p in (_inline(c) for c in items) if p is not None]

    def _post(payload: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            r = run(["gh", "api", f"repos/{slug}/pulls/{number}/reviews",
                     "--method", "POST", "--input", path], check=True)
            try:
                return (json.loads(r.stdout or "{}") or {}).get("html_url", "")
            except ValueError:
                return ""
        finally:
            os.unlink(path)

    def _is_self_review(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(hint in text for hint in SELF_REVIEW_422_HINTS)

    body = summary or "Automated review."
    try:
        url = _post({"body": body, "event": ev, "comments": inline})
        return {"reviewed": True, "mode": "review", "selfReview": False, "event": ev,
                "inlineCount": len(inline), "inline": True, "url": url,
                "localOutputPath": written_path}
    except Exception as e:  # noqa: BLE001 — self-review 422, or inline anchoring failed
        if _is_self_review(e):
            log.warning("reviews API rejected self-review (%s); posting via comments",
                        str(e)[:200])
            return _comments_fallback()
        log.warning("inline review rejected (%s); posting summary-only", str(e)[:200])
        folded = body
        if inline:
            folded += "\n\n---\n### Inline findings\n" + "\n".join(
                f"- `{c['path']}:{c['line']}` — {c['body']}" for c in inline)
        try:
            url = _post({"body": folded, "event": ev})
        except Exception as e2:  # noqa: BLE001 — a self-review 422 can surface here too
            if _is_self_review(e2):
                log.warning("reviews API rejected self-review (%s); posting via comments",
                            str(e2)[:200])
                return _comments_fallback()
            raise
        return {"reviewed": True, "mode": "review", "selfReview": False, "event": ev,
                "inlineCount": 0, "inline": False, "url": url,
                "localOutputPath": written_path}
