"""Report quality gate: secret redaction, required sections, table syntax, PDF-hostile chars
(PLAN_V3 Phase 6.2)."""
from common import report_gate as rg


def test_redacts_secrets():
    md = ("token: abcdef0123456789ABCDEF here and "
          "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w and "
          "AKIAIOSFODNN7EXAMPLE plus a 0123456789abcdef0123456789abcdef01234567 hash")
    out = rg.check(md, required_sections=(), deep=False)
    assert rg.REDACTION in out["markdown"]
    assert "eyJhbGci" not in out["markdown"]          # JWT gone
    assert "AKIAIOSFODNN7EXAMPLE" not in out["markdown"]
    assert any(v.startswith("secret-leak:") for v in out["violations"])


def test_missing_required_section_is_blocking():
    out = rg.check("## Executive Summary\nall good\n", deep=True)
    assert out["ok"] is False
    assert any("missing-section:Findings Overview" == v for v in out["violations"])


def test_all_required_sections_pass():
    md = "## Executive Summary\nx\n## Findings Overview\ny\n## Detailed Findings\nz\n"
    out = rg.check(md, deep=True)
    assert out["ok"] is True and not out["violations"]


def test_tail_section_expected_when_flagged():
    md = "## Executive Summary\nx\n## Findings Overview\ny\n## Detailed Findings\nz\n"
    out = rg.check(md, deep=True, tail_expected=True)
    assert out["ok"] is False
    assert any("Corner and Neglected" in v for v in out["violations"])
    md2 = md + "## Corner and Neglected Feature Coverage\ntable\n"
    assert rg.check(md2, deep=True, tail_expected=True)["ok"] is True


def test_malformed_table_flagged_but_wellformed_passes():
    bad = "## Executive Summary\n## Findings Overview\n## Detailed Findings\n| A | B |\n| 1 | 2 |\n"
    out = rg.check(bad, deep=True)
    assert any(v.startswith("malformed-table") for v in out["violations"]) and out["ok"] is False
    good = ("## Executive Summary\n## Findings Overview\n## Detailed Findings\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert rg.check(good, deep=True)["ok"] is True


def test_pdf_hostile_chars_stripped():
    out = rg.check("## Executive Summary\nfoo -> bar\n", required_sections=(), deep=False)
    # em-dash / arrow replaced with ASCII; recorded as a (non-blocking) violation
    md = rg.check("A — B → C", required_sections=(), deep=False)["markdown"]
    assert "—" not in md and "→" not in md and "->" in md
    assert out["ok"] is True


def test_redacts_camelcase_suffix_and_quoted_secrets():
    # The exact shapes that leaked into the reviewed Orkes PDF: camelCase-suffix labels and
    # quote-delimited values that the original prefix-only regex missed.
    md = "\n".join([
        "conductor.security.jwt.secret=K+pK6OkMyEniDszb46BlpcF/6w/xRgg1Afemr",
        "application-local worker secret 'AAb1fidkrdkkd75cmgh'.",
        "accessKeySecret=AAb1fidkrdkkd75cmgh",
        "conductor.security.auth0.clientSecret=bLAlMyHvDqSomethingLong",
        "sentry_key=b64162b4187a4c5caae8a68a7e291793",
        "encryptionKey 'AI_will_tak3_0veR_S00N11224'",
    ])
    out = rg.check(md, required_sections=(), deep=False)["markdown"]
    for leaked in ("K+pK6OkMyEni", "AAb1fidkrdkkd75cmgh", "bLAlMyHvDq",
                   "b64162b4187a4c5caae8a68a7e291793", "AI_will_tak3_0veR_S00N11224"):
        assert leaked not in out, f"secret leaked: {leaked}"


def test_report_gate_wired_between_sanitize_and_pdf():
    """Item #1: the quality gate runs on the sanitized Markdown and feeds BOTH the PDF and the
    persisted report.md, so secrets/format issues cannot bypass it via either artifact."""
    import json
    from pathlib import Path

    wf = json.loads((Path(__file__).resolve().parents[1]
                     / "conductor" / "workflows" / "deep_assess.json").read_text(encoding="utf-8"))
    tasks = {t["taskReferenceName"]: t for t in wf["tasks"]}
    assert "report_gate" in tasks, "report_quality_gate not wired into deep_assess"
    assert tasks["report_gate"]["name"] == "report_quality_gate"
    assert tasks["report_gate"]["inputParameters"]["markdown"] == "${sanitize_md.output.result}"
    assert tasks["report_pdf"]["inputParameters"]["markdown"] == "${report_gate.output.markdown}"
    assert tasks["persist"]["inputParameters"]["report_md"] == "${report_gate.output.markdown}"
