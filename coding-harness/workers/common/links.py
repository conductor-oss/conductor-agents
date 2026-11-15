"""What a pasted link points at: a repository, an issue or a pull request (docs/WORK_FROM_LINK.md).

GitHub is the provider today. Links to other hosts are recognised and refused with a plain message, so a
person sees at once that the host is not supported rather than a clone or API failure later on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


class LinkError(ValueError):
    """A link the harness cannot work from, said in words a person can act on."""


@dataclass(frozen=True)
class Link:
    provider: str          # "github"
    slug: str              # owner/repo
    kind: str              # "repo" | "issue" | "pull" | "number" (owner/repo#12: issue or pull, resolved later)
    number: int = 0
    base: str = ""         # a branch named by the link (…/tree/<branch>)
    url: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "slug": self.slug, "kind": self.kind, "number": self.number,
                "base": self.base, "url": self.url}


_NAME = r"[A-Za-z0-9_.-]+"
_SHORT = re.compile(rf"^({_NAME})/({_NAME})(?:#(\d+))?$")
_OTHER_HOSTS = {
    "gitlab.com": "GitLab", "bitbucket.org": "Bitbucket", "dev.azure.com": "Azure DevOps",
    "visualstudio.com": "Azure DevOps", "atlassian.net": "Jira", "linear.app": "Linear",
}


def _repo_name(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def parse(link: str) -> Link:
    """``https://github.com/o/r[/issues/N | /pull/N[/…] | /tree/<branch>]``, ``git@github.com:o/r.git``,
    ``o/r`` or ``o/r#N``."""
    s = str(link or "").strip()
    if not s:
        raise LinkError("No link given.")
    if s.startswith("git@"):
        host, _, path = s[4:].partition(":")
        if host.lower() != "github.com":
            raise LinkError(_unsupported(host, s))
        owner, _, name = path.strip("/").partition("/")
        if not owner or not name:
            raise LinkError(f"Not a repository link: {s}")
        return Link("github", f"{owner}/{_repo_name(name)}", "repo", url=f"https://github.com/{owner}/{_repo_name(name)}")
    m = _SHORT.match(s)
    if m:
        slug = f"{m.group(1)}/{_repo_name(m.group(2))}"
        if m.group(3):
            return Link("github", slug, "number", int(m.group(3)), url=f"https://github.com/{slug}/issues/{m.group(3)}")
        return Link("github", slug, "repo", url=f"https://github.com/{slug}")
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host != "github.com":
        raise LinkError(_unsupported(host, s))
    segs = [unquote(p) for p in parts.path.split("/") if p]
    if len(segs) < 2:
        raise LinkError(f"Not a repository, issue or pull request link: {s}")
    slug = f"{segs[0]}/{_repo_name(segs[1])}"
    rest = segs[2:]
    clean = f"https://github.com/{slug}"
    if not rest:
        return Link("github", slug, "repo", url=clean)
    if rest[0] in ("issues", "pull", "pulls") and len(rest) > 1 and rest[1].isdigit():
        kind = "issue" if rest[0] == "issues" else "pull"
        return Link("github", slug, kind, int(rest[1]), url=f"{clean}/{'issues' if kind == 'issue' else 'pull'}/{rest[1]}")
    if rest[0] in ("tree", "blob") and len(rest) > 1:
        # A branch name may contain slashes; for a blob link the file path follows it, and the two cannot be
        # told apart from the URL alone, so a blob link names its first segment as the branch.
        branch = "/".join(rest[1:]) if rest[0] == "tree" else rest[1]
        return Link("github", slug, "repo", base=branch, url=clean)
    raise LinkError(f"Not a repository, issue or pull request link: {s}")


def _unsupported(host: str, link: str) -> str:
    name = next((v for k, v in _OTHER_HOSTS.items() if host == k or host.endswith("." + k)), host or "this host")
    return (f"{name} links are not supported yet ({link}). GitHub issue, pull request and repository links "
            "work today; for another host, give a local checkout as the repository path and the request as text.")
