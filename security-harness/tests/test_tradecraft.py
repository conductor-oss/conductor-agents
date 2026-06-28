"""Tradecraft-as-data (§19 HC ratify-surface): ladders + classifier signatures in
catalog/tradecraft.yaml, overlaying the in-code defaults, HC-proposable, golden-stable at rest."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
from common import tradecraft, deepen, features  # noqa: E402


def test_tradecraft_yaml_mirrors_the_in_code_defaults():
    # golden: the data file overlays to EXACTLY the defaults -> no behavior change at rest
    tradecraft.load.cache_clear()
    assert tradecraft.ladders(deepen._LADDERS_DEFAULT) == deepen._LADDERS_DEFAULT
    assert deepen.LADDERS == deepen._LADDERS_DEFAULT          # live ladders unchanged
    assert "sqlite_error" in features._SQL_SIGNATURES         # signatures loaded from data
    assert "q" in features.COMMON_QUERY_PARAMS
    assert "ssrf" in deepen.LADDERS                           # the dedicated SSRF ladder is live
    # the internal-target / egress-bypass corpus + reach-oracle signatures load from the data file
    assert "[::1]" in deepen.INTERNAL_TARGET_CORPUS and "[fd00:ec2::254]" in deepen.INTERNAL_TARGET_CORPUS
    assert "invalid_token" in features._INTERNAL_REACH_SIGNATURES
    assert "blocked in this cluster" in features._EGRESS_BLOCK_SIGNATURES


def test_overlay_can_add_a_ladder_rung(tmp_path):
    import yaml
    f = tmp_path / "tc.yaml"
    custom = {"ladders": {"sqli": [{"family": "error-based", "idea": "x"}, {"family": "new-rung", "idea": "y"}]}}
    f.write_text(yaml.safe_dump(custom))
    tradecraft.load.cache_clear()
    try:
        os.environ["SC_TRADECRAFT"] = str(f)
        tradecraft.load.cache_clear()
        merged = tradecraft.ladders(deepen._LADDERS_DEFAULT)
        assert any(r["family"] == "new-rung" for r in merged["sqli"])   # YAML overlaid the sqli ladder
        assert "js-sandbox-escape" in merged                            # untouched classes kept
    finally:
        del os.environ["SC_TRADECRAFT"]; tradecraft.load.cache_clear()


def test_deterministic_proposer_appends_candidate_rung_ratify_gated(tmp_path):
    import yaml
    import proposer
    f = tmp_path / "tc.yaml"
    f.write_text(yaml.safe_dump({"ladders": {"js-sandbox-escape": [{"family": "direct-eval", "idea": "x"}]}}))
    fn = proposer.deterministic_proposer()
    # ladder-exhaustion proposal (carries sink_class) -> appends ONE candidate rung
    prop = {"surface": "tradecraft", "sink_class": "js-sandbox-escape", "diagnosis": "ladder exhausted", "evidence": []}
    out = fn(prop, str(f))
    assert out and "hc-candidate-escalation" in out
    rungs = yaml.safe_load(out)["ladders"]["js-sandbox-escape"]
    assert rungs[-1]["family"] == "hc-candidate-escalation"
    # idempotent: applying to the already-augmented file makes no further change
    f.write_text(out)
    assert fn(prop, str(f)) is None
    # classifier-gap (no sink_class) -> deterministic proposer refuses to fabricate a signature
    assert fn({"surface": "tradecraft", "triage_class": "sqli", "evidence": []}, str(f)) is None


def test_candidate_rung_folds_in_deepen_lessons(tmp_path):
    """The deepen LESSONS (why each prior attempt was blocked) flow through `evidence` and must be
    folded into the candidate rung so it is concrete/actionable, not a bare placeholder."""
    import yaml
    import proposer
    f = tmp_path / "tc.yaml"
    f.write_text(yaml.safe_dump({"ladders": {"js-sandbox-escape": [{"family": "direct-eval", "idea": "x"}]}}))
    prop = {"surface": "tradecraft", "sink_class": "js-sandbox-escape", "diagnosis": "ladder exhausted",
            "evidence": ["getClass blocked by the sandbox", "Java is undefined in the engine"]}
    out = proposer.deterministic_proposer()(prop, str(f))
    rung = yaml.safe_load(out)["ladders"]["js-sandbox-escape"][-1]
    assert rung["family"] == "hc-candidate-escalation"
    assert "getClass blocked by the sandbox" in rung["idea"] and "Java is undefined" in rung["idea"]


class _FakeBlock:
    def __init__(self, text): self.text = text


class _FakeMessages:
    def __init__(self, text): self._text = text; self.calls = []
    def create(self, **kw): self.calls.append(kw); return type("R", (), {"content": [_FakeBlock(self._text)]})()


class _FakeClient:
    def __init__(self, text): self.messages = _FakeMessages(text)


def test_llm_proposer_synthesizes_a_concrete_tradecraft_rung(tmp_path, monkeypatch):
    """With a key, the LLM tier turns a ladder-exhaustion diagnosis into ONE concrete rung whose
    idea is the synthesized technique; the rung STRUCTURE stays code-controlled (family fixed)."""
    import yaml
    import proposer
    f = tmp_path / "tc.yaml"
    f.write_text(yaml.safe_dump({"ladders": {"js-sandbox-escape": [{"family": "direct-eval", "idea": "x"}]}}))
    fake = _FakeClient("Use the Function-constructor gadget via ''.getClass to reach Runtime.")
    monkeypatch.setattr(proposer, "_anthropic_client", lambda: fake)
    prop = {"surface": "tradecraft", "sink_class": "js-sandbox-escape", "diagnosis": "ladder exhausted",
            "evidence": ["getClass blocked by the sandbox"]}
    out = proposer.llm_proposer()(prop, str(f))
    rung = yaml.safe_load(out)["ladders"]["js-sandbox-escape"][-1]
    assert rung["family"] == "hc-candidate-escalation"            # structure code-controlled
    assert "Function-constructor gadget" in rung["idea"]          # idea is the synthesized technique
    # the diagnosis + lesson were fed to the model (sanitized), not raw target content
    assert "ladder exhausted" in fake.messages.calls[0]["messages"][0]["content"]


def test_llm_proposer_falls_back_to_deterministic_when_llm_yields_nothing(tmp_path, monkeypatch):
    """A tradecraft proposal must NEVER be silently dropped when the LLM tier is selected: an empty
    LLM response falls back to the deterministic candidate rung."""
    import yaml
    import proposer
    f = tmp_path / "tc.yaml"
    f.write_text(yaml.safe_dump({"ladders": {"sqli": [{"family": "error-based", "idea": "x"}]}}))
    monkeypatch.setattr(proposer, "_anthropic_client", lambda: _FakeClient("   "))   # empty synthesis
    prop = {"surface": "tradecraft", "sink_class": "sqli", "diagnosis": "ladder exhausted", "evidence": []}
    out = proposer.llm_proposer()(prop, str(f))
    assert out and yaml.safe_load(out)["ladders"]["sqli"][-1]["family"] == "hc-candidate-escalation"


def test_tradecraft_surface_is_registered_ratify_and_reversible():
    from common import hillclimb, config_lineage, hc_writeback
    assert hillclimb.SURFACE_MODE["tradecraft"] == "ratify"
    assert "tradecraft" in config_lineage.SURFACES
    assert "tradecraft" in hc_writeback.RATIFY_SURFACES
