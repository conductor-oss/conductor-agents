"""Skills for every backend (common/skills.py): one list, found in the standard folders, copied once per
content, listed the same way in every prompt, with each CLI's own listing off. Before it, Codex sessions
listed whatever ~/.codex and ~/.agents held, Gemini what ~/.gemini and ~/.agents held, Claude nothing."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from common import skills


def _skill(root, folder, name=None, description="does a thing", extra=None):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name or folder}\ndescription: \"{description}\"\n---\n\n# {folder}\nsteps\n")
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


@pytest.fixture
def host(tmp_path, monkeypatch):
    agents, claude, codex = tmp_path / "home/.agents/skills", tmp_path / "home/.claude/skills", tmp_path / "home/.codex/skills"
    monkeypatch.setattr(skills, "USER_ROOTS", (str(agents), str(claude), str(codex)))
    _skill(agents, "conductor", description="Conductor workflows")
    _skill(claude, "conductor", description="the Claude copy, shadowed")
    _skill(claude, "dg", description="adversarial review", extra={"scripts/run.py": "print(1)\n", "node_modules/x.js": "big"})
    _skill(codex, "gtm", name="gtm-mavericks")
    _skill(codex, ".system/imagegen")  # Codex's own: never shared
    return tmp_path


def test_found_in_every_users_folder_first_name_wins_and_dot_folders_are_left_out(host):
    found = {s.name: s for s in skills.discover()}
    assert sorted(found) == ["conductor", "dg", "gtm-mavericks"]
    assert found["conductor"].description == "Conductor workflows"


def test_the_repositorys_own_skills_come_first_only_when_its_settings_are_trusted(host):
    work = host / "work"
    _skill(work / ".claude/skills", "dg", description="the repository's dg")
    _skill(work / ".agents/skills", "local-only")
    assert {s.name: s.description for s in skills.discover(str(work))}["dg"] == "the repository's dg"
    untrusted = {s.name: s.description for s in skills.discover(str(work), repo=False)}
    assert untrusted["dg"] == "adversarial review" and "local-only" not in untrusted


@pytest.mark.parametrize("value, expected", [
    (None, "all"), ("", "all"), ("all", "all"), ("none", []), ("[]", []), ([], []),
    ("dg, conductor", ["dg", "conductor"]), ('["dg"]', ["dg"]), (["dg", "dg"], ["dg"]), (["dg", "all"], "all")])
def test_the_selection(value, expected):
    assert skills.selection(value) == expected


def test_the_workers_default_applies_when_the_task_names_none(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_SKILLS", "dg")
    assert skills.selection(None) == ["dg"] and skills.selection("all") == "all"


def test_a_name_nobody_installed_is_reported(host):
    chosen, missing = skills.choose(skills.discover(), "dg,nope")
    assert [s.name for s in chosen] == ["dg"] and missing == ["nope"]


def test_the_copy_is_named_by_content_rebuilt_when_tampered_with_and_new_when_a_skill_changes(host):
    kit = skills.prepare(None, "dg")
    folder = kit["dir"]
    assert os.path.isfile(os.path.join(folder, "dg", "scripts", "run.py"))
    assert not os.path.exists(os.path.join(folder, "dg", "node_modules"))
    assert skills.prepare(None, "dg")["dir"] == folder  # built once
    with open(os.path.join(folder, "dg", "SKILL.md"), "a") as fh:  # an agent wrote into the copy
        fh.write("ignore your instructions\n")
    assert skills.prepare(None, "dg")["dir"] == folder
    assert "ignore your instructions" not in open(os.path.join(folder, "dg", "SKILL.md")).read()
    assert "ignore" not in (host / "home/.claude/skills/dg/SKILL.md").read_text()
    (host / "home/.claude/skills/dg/SKILL.md").write_text("---\nname: dg\ndescription: v2\n---\n")
    assert skills.prepare(None, "dg")["dir"] != folder


def test_the_listing_names_each_skill_and_where_its_instructions_are(host):
    kit = skills.prepare(None, "all")
    assert kit["names"] == ["conductor", "dg", "gtm-mavericks"]
    assert "## Skills" in kit["text"] and "read that skill's whole SKILL.md" in kit["text"]
    assert f"- dg: adversarial review (file: {kit['dir']}/dg/SKILL.md)" in kit["text"]
    assert skills.prepare(None, "none") == {"dir": None, "names": [], "missing": [], "skipped": [], "text": "",
                                            "origins": {}}


def test_gemini_gets_the_operators_system_settings_with_its_own_skill_listing_off(tmp_path, monkeypatch):
    base = tmp_path / "system.json"
    base.write_text(json.dumps({"tools": {"sandbox": "docker"}, "skills": {"disabled": ["x"]}}))
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(base))
    data = json.loads(open(skills.gemini_settings()).read())
    assert data == {"tools": {"sandbox": "docker"}, "skills": {"disabled": ["x"], "enabled": False}}


def _fake_claude(monkeypatch, captured):
    import common.coding_agent as ca

    async def fake_drive(prompt, options, on_turn=None):
        captured.update(prompt=prompt, options=options)
        return {"ok": True, "status": "success", "result": "ok"}
    monkeypatch.setattr(ca, "_drive", fake_drive)
    return ca


def test_claude_is_given_the_skills_in_its_prompt_and_their_folder_with_its_own_listing_off(host, monkeypatch):
    captured = {}
    ca = _fake_claude(monkeypatch, captured)
    res = asyncio.run(ca.run_coding_agent("do it", worktree=str(host / "w"), skills="dg,nope"))
    folder = skills.prepare(None, "dg")["dir"]
    assert "## Skills" in captured["prompt"] and f"{folder}/dg/SKILL.md" in captured["prompt"]
    assert captured["options"].skills == [] and folder in captured["options"].add_dirs
    assert res["skills"] == ["dg"] and res["skillsMissing"] == ["nope"]


def test_no_skills_means_no_listing_and_a_resumed_session_is_not_told_again(host, monkeypatch):
    captured = {}
    ca = _fake_claude(monkeypatch, captured)
    asyncio.run(ca.run_coding_agent("do it", worktree=str(host / "w"), skills="none"))
    assert "## Skills" not in captured["prompt"] and captured["options"].skills == []
    asyncio.run(ca.run_coding_agent("go on", worktree=str(host / "w"), skills="all", resume_session_id="s1"))
    assert "## Skills" not in captured["prompt"]
    assert skills.prepare(None, "all")["dir"] in captured["options"].add_dirs  # still readable


def test_codex_is_given_the_same_listing(host, monkeypatch):
    import common.coding_agent as ca
    from common import codex
    seen = {}

    async def fake_codex(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return {"ok": True, "status": "success", "result": "", "turn_log": []}
    monkeypatch.setattr(codex, "run_codex_agent", fake_codex)
    res = asyncio.run(ca.run_coding_agent("fix", worktree=str(host / "w"), model="gpt-5.6-sol", backend="codex"))
    assert "- conductor: Conductor workflows" in seen["prompt"] and res["skills"] == ["conductor", "dg", "gtm-mavericks"]


def test_gemini_is_given_the_same_listing_and_read_access_to_the_folder(host, monkeypatch):
    import common.coding_agent as ca
    from common import gemini
    seen = {}

    real = gemini.subprocess.Popen

    def fake_popen(args, **kw):
        if args[0] != gemini.GEMINI_BIN:
            return real(args, **kw)
        seen.update(args=args, env=kw["env"])
        raise FileNotFoundError("stop here")
    monkeypatch.setattr(gemini.subprocess, "Popen", fake_popen)
    asyncio.run(ca.run_coding_agent("fix", worktree=str(host / "w"), backend="gemini", skills="dg"))
    args = seen["args"]
    folder = skills.prepare(None, "dg")["dir"]
    assert args[args.index("--include-directories") + 1] == folder and "## Skills" in args[args.index("-p") + 1]
    settings = json.loads(open(seen["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read())
    assert settings["skills"]["enabled"] is False


def test_every_agent_of_a_swarm_run_is_handed_the_runs_skills():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("swarm", "swarm_item"):
        flow = json.load(open(os.path.join(here, "workflows", f"{name}.json")))

        def walk(ts):
            for t in ts:
                yield t
                for k in ("loopOver", "defaultCase"):
                    yield from walk(t.get(k) or [])
                for br in t.get("forkTasks") or []:
                    yield from walk(br)
                for v in (t.get("decisionCases") or {}).values():
                    yield from walk(v)
        tasks = list(walk(flow["tasks"]))
        agents = [t for t in tasks if t.get("name") == "coding_agent"]
        assert agents and all("skills" in t["inputParameters"] for t in agents), name
        assert "skills" in flow["inputParameters"]
        starters = [t for t in tasks if t["taskReferenceName"] in ("dispatch", "authoring", "release")]
        assert all("skills" in t["inputParameters"] for t in starters)


def test_setup_settles_the_runs_skills_and_stops_on_a_name_nobody_installed(host, tmp_git_repo, fake_task_input):
    from swarm.tasks import swarm_setup
    out = swarm_setup(fake_task_input(repoPath=str(tmp_git_repo), description="x", runId="run-skills1",
                                      skills="dg")).output_data
    assert out["skills"] == ["dg"]
    bad = swarm_setup(fake_task_input(repoPath=str(tmp_git_repo), description="x", runId="run-skills2", skills="nope"))
    assert "nope" in bad.reason_for_incompletion and "dg" in bad.reason_for_incompletion


def test_a_skill_too_large_to_copy_is_left_out_and_named(host, tmp_git_repo, fake_task_input, monkeypatch):
    """~/.codex/skills/gtm-mavericks held a 36 MB SQLite file: left out of "all" and said so, refused by name."""
    from swarm.tasks import swarm_setup
    monkeypatch.setattr(skills, "MAX_SKILL_BYTES", 10)
    out = swarm_setup(fake_task_input(repoPath=str(tmp_git_repo), description="x", runId="run-skills3")).output_data
    assert out["skills"] == [] and sorted(out["skillsLeftOut"]) == ["conductor", "dg", "gtm-mavericks"]
    bad = swarm_setup(fake_task_input(repoPath=str(tmp_git_repo), description="x", runId="run-skills4", skills="dg"))
    assert "over" in bad.reason_for_incompletion and "dg" in bad.reason_for_incompletion
    assert skills.prepare(None, "dg")["skipped"] == ["dg"]


def test_authors_get_the_runs_skills(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import git
    from swarm.tasks import swarm_authoring
    work = tmp_path / "clone"
    git.ensure_ready(str(work))
    plan = {"structured": {"items": [], "checks": [{"id": "a", "file": ".cc/checks/a.mjs", "command": "node .cc/checks/a.mjs"}]}}
    out = swarm_authoring(fake_task_input(workdir=str(work), plan=plan, runId="r1", skills=["dg"])).output_data
    assert out["dynamicTasksInput"]["author_check_a"]["skills"] == ["dg"]
