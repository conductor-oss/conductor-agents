"""Run-to-run regression diff + untestable lifecycle (item #4): 'could not retest' is never
reported as 'remediated' when a finding's confirmation channel was unavailable this run."""
from common import memory


def _ssrf(**over):
    f = {"title": "IPv6 loopback SSRF", "category": "ssrf", "source_tool": "oob_confirmed",
         "evidence_state": "runtime_oob_confirmed", "severity": "High"}
    f.update(over)
    return f


def test_prior_oob_finding_is_untestable_when_oob_down():
    d = memory.regression_diff([_ssrf()], [], channels={"oob": False, "inband": True})
    assert d["untestable"] == 1 and d["not_reobserved"] == 0 and d["reconfirmed"] == 0
    assert d["items"][0]["status"] == "untestable" and "not remediated" in d["items"][0]["reason"]


def test_prior_finding_not_reobserved_when_channel_available():
    d = memory.regression_diff([_ssrf()], [], channels={"oob": True, "inband": True})
    assert d["not_reobserved"] == 1 and d["untestable"] == 0


def test_reconfirmed_when_present_again():
    d = memory.regression_diff([_ssrf()], [_ssrf()], channels={"oob": True})
    assert d["reconfirmed"] == 1 and d["items"][0]["status"] == "reconfirmed"


def test_new_finding_tagged_new():
    d = memory.regression_diff([], [_ssrf(title="new bug", category="rce")])
    assert d["new_this_run"] == 1 and d["items"][0]["status"] == "new"


def test_merge_run_marks_untestable_not_stale_when_oob_down():
    prior = memory.empty_state()
    prior["all_confirmed"] = [_ssrf()]
    prior["app_version"] = "v1"
    state, stats = memory.merge_run(
        prior, fp="fp", host="h", app_version="v2",  # released (version changed)
        new_confirmed=[], new_rejected=[], new_blind=[], new_tried=[], gaps=[], coverage={},
        channels={"oob": False, "inband": True}, run_id="r2")
    assert stats["untestable"] == 1 and stats["stale_revalidated"] == 0
    assert state["all_confirmed"][0]["lifecycle"] == "untestable"


def test_merge_run_backward_compat_channels_none_marks_stale_on_release():
    prior = memory.empty_state()
    prior["all_confirmed"] = [_ssrf()]
    prior["app_version"] = "v1"
    state, stats = memory.merge_run(
        prior, fp="fp", host="h", app_version="v2",
        new_confirmed=[], new_rejected=[], new_blind=[], new_tried=[], gaps=[], coverage={},
        run_id="r2")  # channels omitted -> assume available -> stale on release (prior behaviour)
    assert stats["stale_revalidated"] == 1 and stats["untestable"] == 0
    assert state["all_confirmed"][0]["lifecycle"] == "stale"
