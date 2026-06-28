"""The §19.7 proposer (bench/proposer.py): the deterministic, offline edit-generator. Proves the
edit is bounded to the prompt's TACTICS region (method core frozen), is the failing-exemplar
gradient, fails closed on un-opted surfaces, and that surface_path maps diagnosed surfaces to
real files. The LLM tier is best-effort and gated — it degrades to None with no key, which is
all we assert here (no live API in unit tests)."""
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, os.path.join(ROOT, "workers"))
import proposer as P  # noqa: E402
from common import prompt_units  # noqa: E402

EXPLOIT = os.path.join(ROOT, "prompts", "exploit.md")

_PROMPT_PROPOSAL = {"surface": "prompt", "objective_id": "INFRA-RCE-INJECTION",
                    "diagnosis": "technique weak for class",
                    "evidence": ["rejected: SpEL payload filtered", "rejected: eval sink blocked"],
                    "signature": "INFRA-RCE-INJECTION|rejected|exec"}


def test_surface_path_maps_known_surfaces_and_rejects_unknown():
    assert P.surface_path("prompt").endswith("prompts/exploit.md")
    assert P.surface_path("evidence_bar").endswith("prompts/verify.md")
    assert os.path.isfile(P.surface_path("catalog"))
    assert P.surface_path("safety") is None and P.surface_path("nonsense") is None


def test_exploit_prompt_has_an_opted_in_tactics_region():
    """The opt-in that makes the deterministic proposer non-trivial — if this fails, someone
    removed the TACTICS markers and auto-tuning silently became a no-op."""
    parts = prompt_units.split(open(EXPLOIT, encoding="utf-8").read())
    assert parts["has_region"] is True and parts["tactics"].strip()


def test_deterministic_proposer_edits_only_the_tactics_region():
    text = open(EXPLOIT, encoding="utf-8").read()
    parts = prompt_units.split(text)
    out = P.deterministic_proposer()(_PROMPT_PROPOSAL, EXPLOIT)
    assert out and out != text
    assert parts["method_core"] in out                 # frozen method core preserved verbatim
    assert "Cases this must now handle" in out         # the failing-exemplar gradient was appended
    assert "SpEL payload filtered" in out
    # the result is still a single well-formed region (recombine round-trips)
    assert prompt_units.split(out)["has_region"] is True


def test_deterministic_proposer_is_fail_closed_off_prompt_surfaces():
    pf = P.deterministic_proposer()
    for surface in ("profile", "catalog", "evidence_bar"):
        assert pf({**_PROMPT_PROPOSAL, "surface": surface}, P.surface_path(surface) or "x") is None


def test_deterministic_proposer_skips_when_no_editable_region(tmp_path):
    plain = tmp_path / "plain.md"
    plain.write_text("a prompt with no tactics markers", encoding="utf-8")
    assert P.deterministic_proposer()(_PROMPT_PROPOSAL, str(plain)) is None


def test_deterministic_proposer_no_exemplars_is_noop():
    """No concrete missed cases -> no gradient -> no edit (don't churn config for nothing)."""
    assert P.deterministic_proposer()({**_PROMPT_PROPOSAL, "evidence": []}, EXPLOIT) is None


def test_llm_proposer_degrades_to_none_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    assert P.llm_proposer()(_PROMPT_PROPOSAL, EXPLOIT) is None
