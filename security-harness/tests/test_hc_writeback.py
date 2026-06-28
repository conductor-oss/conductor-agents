"""The §19 write-back loop (hc_writeback): the gated champion/challenger promotion that was the
missing half. Proves auto-adopt only fires on a significant, non-regressing, MEASURED change on an
adequate oracle; everything else is downgraded to human ratification / rejected / re-baselined; and
that an adoption is an actual reversible lineage edition."""
from common import hc_writeback as wb
from common import config_lineage

# Adequate vs thin vs unmeasured oracle (the D16/§19.2 gate inputs).
ADEQUATE = {"objective_coverage": {"unmeasured": []}, "scored_targets": 2, "name": "bench-v1"}
THIN = {"objective_coverage": {"unmeasured": []}, "scored_targets": 1, "name": "bench-v1"}
UNMEASURED = {"objective_coverage": {"unmeasured": ["INFRA-RCE-INJECTION"]}, "scored_targets": 2, "name": "bench-v1"}

GENUINE_BEFORE = [{"recall": .60, "fp_rate": .05, "cost": 1.0, "per_class": {"infra": .6, "tenancy": .5}}] * 5
GENUINE_AFTER = [{"recall": r, "fp_rate": .05, "cost": 1.0, "per_class": {"infra": .75, "tenancy": .5}}
                 for r in (.74, .76, .73, .77, .75)]
NOISY_AFTER = [{"recall": r, "fp_rate": .05, "cost": 1.0, "per_class": {"infra": .6, "tenancy": .5}}
               for r in (.95, .55, .60, .58, .62)]                       # not significant
FORGET_AFTER = [{"recall": r, "fp_rate": .05, "cost": 1.0, "per_class": {"infra": .85, "tenancy": .30}}
                for r in (.74, .76, .73, .77, .75)]                      # per-class recall drop


def _ch(surface, *, objective_id="INFRA-SSRF", after=None, path=None):
    return {"id": "ch1", "surface": surface, "objective_id": objective_id,
            "path": path or {"prompt": "prompts/exploit.md", "profile": "profiles/conductor.json",
                             "catalog": "catalog/objectives.yaml", "evidence_bar": "prompts/verify.md"}.get(surface, "x"),
            "diagnosis": "technique weak for class", "content": "new edited content",
            "before_runs": GENUINE_BEFORE, "after_runs": after or GENUINE_AFTER}


# ── oracle-adequacy gate (the iron rule) ──

def test_oracle_adequate_requires_measured_class_and_two_targets():
    assert wb.oracle_adequate(ADEQUATE, "prompt", "INFRA-SSRF")[0] is True
    ok, why = wb.oracle_adequate(THIN, "prompt", "INFRA-SSRF")
    assert ok is False and "scored targets" in why
    ok2, why2 = wb.oracle_adequate(UNMEASURED, "prompt", "INFRA-RCE-INJECTION")
    assert ok2 is False and "UNMEASURED" in why2


# ── decide(): the composed §19 gates ──

def test_auto_surface_adopts_only_when_significant_measured_adequate():
    assert wb.decide(_ch("prompt"), champion_model="m", current_model="m", benchmark=ADEQUATE)["disposition"] == "auto_adopt"


def test_thin_oracle_downgrades_auto_to_ratify():
    d = wb.decide(_ch("prompt"), champion_model="m", current_model="m", benchmark=THIN)
    assert d["disposition"] == "ratify" and "scored targets" in d["reason"]


def test_unmeasured_class_downgrades_to_ratify():
    d = wb.decide(_ch("prompt", objective_id="INFRA-RCE-INJECTION"), champion_model="m", current_model="m", benchmark=UNMEASURED)
    assert d["disposition"] == "ratify" and "UNMEASURED" in d["reason"]


def test_catalog_and_evidence_bar_are_ratify_only():
    for s in ("catalog", "evidence_bar"):
        d = wb.decide(_ch(s), champion_model="m", current_model="m", benchmark=ADEQUATE)
        assert d["disposition"] == "ratify" and "human-ratify" in d["reason"]


def test_not_significant_is_rejected():
    d = wb.decide(_ch("prompt", after=NOISY_AFTER), champion_model="m", current_model="m", benchmark=ADEQUATE)
    assert d["disposition"] == "reject" and "significant" in d["reason"]


def test_protected_regression_is_rejected():
    d = wb.decide(_ch("prompt", after=FORGET_AFTER), champion_model="m", current_model="m", benchmark=ADEQUATE)
    assert d["disposition"] == "reject" and "regress" in d["reason"]


def test_model_change_demands_rebaseline():
    d = wb.decide(_ch("prompt"), champion_model="claude-opus-4-8", current_model="claude-sonnet-4-6", benchmark=ADEQUATE)
    assert d["disposition"] == "rebaseline"


def test_non_tunable_surface_is_never_adopted():
    d = wb.decide(_ch("safety", path="x"), champion_model="m", current_model="m", benchmark=ADEQUATE)
    assert d["disposition"] == "reject" and "not tunable" in d["reason"]


# ── promote(): the actual write-back (commits a reversible lineage edition) ──

def test_promote_auto_adopt_commits_a_shadow_edition():
    store = []
    out = wb.promote(store, [_ch("prompt")], champion_model="m", current_model="m", benchmark=ADEQUATE, as_of="t1")
    assert len(out["promoted"]) == 1 and out["pending_ratification"] == [] and out["rejected"] == []
    head = config_lineage.head(store, "prompt", "prompts/exploit.md")
    assert head and head["version"] == 1 and head["provenance"]["shadow"] is True
    assert head["model"] == "m" and head["scores"]["after"]["recall"] > head["scores"]["before"]["recall"]
    assert config_lineage.verify_chain(store)["ok"] is True        # tamper-evident lineage intact


def test_promote_thin_oracle_writes_nothing_and_queues_ratification():
    store = []
    out = wb.promote(store, [_ch("prompt")], champion_model="m", current_model="m", benchmark=THIN, as_of="t1")
    assert out["promoted"] == [] and len(out["pending_ratification"]) == 1
    assert store == []                                              # no edition committed on a thin oracle


def test_apply_ratified_commits_a_ratified_edition():
    store = []
    rec = wb.apply_ratified(store, {"surface": "catalog", "path": "catalog/objectives.yaml", "reason": "sharpen how_to_test"},
                            "new catalog content", current_model="m", benchmark=ADEQUATE, as_of="t1")
    assert rec["provenance"]["ratified"] is True
    assert config_lineage.head(store, "catalog", "catalog/objectives.yaml")["version"] == 1


# ── rollback (H5 automatic revert on regression) ──

def test_should_rollback_on_protected_regression_and_plan_targets_prior():
    regressed, reasons = wb.should_rollback(GENUINE_BEFORE, FORGET_AFTER)
    assert regressed is True and reasons
    assert wb.should_rollback(GENUINE_BEFORE, GENUINE_AFTER)[0] is False
    # rollback_plan returns the prior edition to revert to
    store = []
    config_lineage.commit(store, surface="prompt", path="p", content="v1", as_of="t1")
    config_lineage.commit(store, surface="prompt", path="p", content="v2", as_of="t2")
    target = wb.rollback_plan(store, "prompt", "p")
    assert target and target["version"] == 1


# ── run_cycle: the full propose -> eval -> gated promote loop (stub proposer + eval) ──

# Two recurring 'rejected' traces -> diagnose maps to the prompt surface (technique weak).
RECUR_TRACES = [{"objective_id": "INFRA-SSRF", "outcome": "rejected", "evidence_bar": "exec",
                 "reason": "payload filtered"}] * 2
_BASE = {"recall": .60, "fp_rate": .05, "per_class": {"infra": .6}}
_BETTER = {"recall": .80, "fp_rate": .05, "per_class": {"infra": .8}}


def _cycle(store, *, propose_fn, eval_fn, surface_path, benchmark=ADEQUATE, traces=RECUR_TRACES):
    return wb.run_cycle(store, traces, propose_fn=propose_fn, eval_fn=eval_fn,
                        surface_path=surface_path, champion_model="m", current_model="m",
                        benchmark=benchmark, as_of="t1", holdout_targets=[{"name": "x"}], n_runs=5)


def test_run_cycle_promotes_winning_challenger_and_commits_lineage():
    store = []
    out = _cycle(store, propose_fn=lambda p, path: "edited tactics",
                 eval_fn=lambda overlay, t: (_BETTER if overlay else _BASE),
                 surface_path=lambda s, o: "prompts/exploit.md" if s == "prompt" else None)
    assert out["proposals"] >= 1 and len(out["promoted"]) == 1
    head = config_lineage.head(store, "prompt", "prompts/exploit.md")
    assert head and head["version"] == 1
    assert head["content_hash"] == config_lineage.content_hash("edited tactics")
    assert config_lineage.verify_chain(store)["ok"] is True


def test_run_cycle_skips_when_proposer_declines():
    out = _cycle([], propose_fn=lambda p, path: None,
                 eval_fn=lambda overlay, t: (_BETTER if overlay else _BASE),
                 surface_path=lambda s, o: "prompts/exploit.md")
    assert out["promoted"] == [] and len(out["skipped"]) >= 1
    assert "no edit" in out["skipped"][0]["why"]


def test_run_cycle_skips_unmapped_surface():
    out = _cycle([], propose_fn=lambda p, path: "x",
                 eval_fn=lambda overlay, t: (_BETTER if overlay else _BASE),
                 surface_path=lambda s, o: None)               # nothing maps
    assert out["promoted"] == [] and len(out["skipped"]) >= 1
    assert "no config file" in out["skipped"][0]["why"]


def test_run_cycle_no_recurring_signal_is_a_noop():
    out = _cycle([], propose_fn=lambda p, path: "x",
                 eval_fn=lambda overlay, t: _BASE,
                 surface_path=lambda s, o: "prompts/exploit.md",
                 traces=[RECUR_TRACES[0]])                     # a single trace -> not corroborated (H4)
    assert out["proposals"] == 0 and out["promoted"] == []


def test_run_cycle_thin_oracle_downgrades_winning_challenger_to_ratify():
    store = []
    out = _cycle(store, propose_fn=lambda p, path: "edited tactics",
                 eval_fn=lambda overlay, t: (_BETTER if overlay else _BASE),
                 surface_path=lambda s, o: "prompts/exploit.md", benchmark=THIN)
    assert out["promoted"] == [] and len(out["pending_ratification"]) == 1 and store == []
