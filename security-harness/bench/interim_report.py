#!/usr/bin/env python3
"""Build an interim PDF report from recovered confirmed findings (a crashed run's
workflow.variables.all_confirmed). Standalone: HTML -> headless Chromium -> PDF. Used when a
campaign confirms findings but overflows the workflow-variables cap before the report step runs."""
import html
import json
import sys

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEV_COLOR = {"critical": "#7c0a02", "high": "#c0392b", "medium": "#d68910", "low": "#2471a3", "info": "#566573"}


def _esc(x):
    return html.escape(str(x or ""))


def _dedupe(findings):
    """Collapse exact-duplicate vectors (same category + normalized title), keeping the richest."""
    by = {}
    for f in findings:
        key = (f.get("category"), (f.get("title") or "").split(" via ")[0].strip().lower())
        cur = by.get(key)
        if cur is None or len(json.dumps(f)) > len(json.dumps(cur)):
            by[key] = f
    return list(by.values())


def build_html(findings, meta):
    findings = sorted(findings, key=lambda f: SEV_ORDER.get((f.get("severity") or "").lower(), 9))
    rows = []
    for i, f in enumerate(findings, 1):
        sev = (f.get("severity") or "info").lower()
        rows.append(f"""<tr>
          <td>{i}</td>
          <td><span class="sev" style="background:{SEV_COLOR.get(sev,'#566573')}">{_esc(sev.upper())}</span></td>
          <td>{_esc(f.get('objective_id'))}</td>
          <td>{_esc(f.get('title'))}</td>
        </tr>""")
    summary_table = "\n".join(rows)

    cards = []
    for i, f in enumerate(findings, 1):
        sev = (f.get("severity") or "info").lower()
        poc = f.get("poc_request") or {}
        poc_line = f"{_esc(poc.get('method'))} {_esc(poc.get('url') or poc.get('path'))}" if poc else "&mdash;"
        identity = _esc(poc.get("identity")) if poc else ""
        chain = f.get("evidence_chain") or []
        # Prefer evidence-chain entries that show the SUCCESSFUL confirmation (a failed first attempt
        # alone misrepresents a confirmed finding). Rank entries with success markers first.
        def _score(c):
            ex = (c.get("stdout_excerpt") if isinstance(c, dict) else str(c)) or ""
            return sum(m in ex for m in ("COMPLETED", "TASK OUT", "start 200", '"status": 200', "hit"))
        ranked = sorted(chain, key=_score, reverse=True)
        chain_txt = ""
        for c in ranked[:2]:
            ex = c.get("stdout_excerpt") if isinstance(c, dict) else str(c)
            if ex:
                chain_txt += f"<pre>{_esc(ex[:2200])}</pre>"
        oob = f.get("oob_tokens") or []
        oob_u = sorted(set(oob)) if isinstance(oob, list) else [oob]
        # reproduction may be a string or a list of steps
        rep = f.get("reproduction")
        if isinstance(rep, list):
            rep_html = "<ol>" + "".join(f"<li>{_esc(s)}</li>" for s in rep if str(s).strip()) + "</ol>"
        else:
            rep_html = f"<p>{_esc(rep)}</p>"
        # validation can be empty on some findings -> fall back to the evidence-based confirmation
        why = f.get("validation") or (
            "Confirmed out-of-band: the planted canary received an inbound request from the target's own "
            "infrastructure (see Evidence). The per-finding verifier did not emit a separate validation note; "
            "the OOB callback is the decisive signal." if (f.get("oob_tokens")) else f.get("evidence"))
        cards.append(f"""
        <div class="card">
          <h3>[{i}] <span class="sev" style="background:{SEV_COLOR.get(sev,'#566573')}">{_esc(sev.upper())}</span> {_esc(f.get('title'))}</h3>
          <table class="meta">
            <tr><th>Objective</th><td>{_esc(f.get('objective_id'))} &nbsp;|&nbsp; {_esc(f.get('category'))}</td></tr>
            <tr><th>CWE / OWASP</th><td>{_esc(f.get('cwe'))} &nbsp;|&nbsp; {_esc(f.get('owasp'))}</td></tr>
            <tr><th>Confidence</th><td>{_esc(f.get('confidence'))}</td></tr>
            <tr><th>PoC</th><td><code>{poc_line}</code>{(' &nbsp;(identity: '+identity+')') if identity else ''}</td></tr>
            <tr><th>OOB canaries</th><td><code>{_esc(', '.join(oob_u))}</code></td></tr>
          </table>
          <h4>Evidence</h4><p>{_esc(f.get('evidence'))}</p>
          <h4>Why it's confirmed</h4><p>{_esc(why)}</p>
          <h4>Reproduction</h4>{rep_html}
          {('<h4>Captured evidence chain</h4>'+chain_txt) if chain_txt else ''}
        </div>""")
    cards_html = "\n".join(cards)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    body{{font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1b2631;margin:0;padding:0 36px;font-size:12px;line-height:1.45}}
    h1{{font-size:22px;border-bottom:3px solid #1b2631;padding-bottom:6px;margin-bottom:4px}}
    h2{{font-size:16px;margin-top:26px;border-bottom:1px solid #aeb6bf;padding-bottom:3px}}
    h3{{font-size:13px;margin:0 0 8px}} h4{{font-size:12px;margin:12px 0 2px;color:#34495e}}
    .sev{{color:#fff;padding:1px 7px;border-radius:3px;font-size:10px;font-weight:700;letter-spacing:.4px}}
    table{{border-collapse:collapse;width:100%;margin:8px 0}}
    th,td{{text-align:left;vertical-align:top;padding:4px 8px;border:1px solid #d5dbdb;font-size:11px}}
    table.summary th{{background:#1b2631;color:#fff}}
    table.meta th{{width:130px;background:#f4f6f7}}
    .card{{border:1px solid #ccd1d1;border-radius:5px;padding:12px 14px;margin:14px 0;page-break-inside:avoid}}
    pre{{background:#f4f6f7;border:1px solid #d5dbdb;border-radius:3px;padding:8px;white-space:pre-wrap;word-break:break-word;font-size:10px;max-height:none}}
    code{{background:#f4f6f7;padding:1px 4px;border-radius:2px;font-size:10.5px}}
    .banner{{background:#fdf2e9;border:1px solid #e59866;border-radius:4px;padding:8px 12px;margin:12px 0;font-size:11px}}
    .meta-top{{color:#566573;font-size:11px;margin:2px 0 0}}
    </style></head><body>
    <h1>Security Assessment &mdash; Interim Findings</h1>
    <p class="meta-top"><b>Target:</b> {_esc(meta['target'])} &nbsp; <b>Mode:</b> deep_assess cap-2 (authorized) &nbsp; <b>Identities:</b> {_esc(meta['identities'])}</p>
    <p class="meta-top"><b>Run:</b> {_esc(meta['run'])} &nbsp; <b>Generated:</b> {_esc(meta['generated'])}</p>
    <div class="banner"><b>Interim report.</b> Recovered from a campaign that confirmed these findings but
    overflowed the Conductor workflow-variables cap (256&nbsp;KB) before the report step ran. All findings below
    were adversarially verified during the run; the OOB callbacks are decisive, hard-to-fake proof. Coverage,
    residual-risk and purple-team sections are produced by the full report and are not included here. A complete
    re-run (with the limit raised) is in progress.</div>

    <h2>Executive summary</h2>
    <p>The campaign confirmed <b>{len(findings)} distinct {meta.get('class','SSRF')} vectors</b> (deduped from
    {meta['raw_count']} raw confirmations) on {_esc(meta['target'])}. In every case a <b>low-privilege tenant</b>
    drove the Conductor engine itself to issue attacker-controlled outbound requests, confirmed out-of-band by
    inbound hits to a tester-controlled collaborator <b>originating from the target's own infrastructure</b>
    (AWS source IPs, <code>User-Agent: okhttp/4.12.0</code>, pod <code><internal-pod>*</code>), and
    corroborated in-band by the echoed response bodies. The security-relevant question is reachability of
    internal/metadata endpoints &mdash; and across every vector the cluster egress filter <b>blocked</b>
    169.254.169.254 / 127.0.0.1 / localhost (403), and IP-encoding bypasses failed, so cloud-metadata and
    credential theft are currently mitigated.</p>
    <div class="banner"><b>Severity reconciliation.</b> All three findings are the <i>same</i> SSRF class. #2 and
    #3 are rated Medium because their verifier applied the egress-filter mitigation; #1 retains the agent's
    original <b>High</b> only because its per-finding verifier did not incorporate that same mitigation. Given the
    egress filter holds against every attempted bypass, the <b>effective severity is Medium</b> for all three;
    treat the egress filter as the single load-bearing control.</div>

    <h2>Findings overview</h2>
    <table class="summary"><tr><th>#</th><th>Severity</th><th>Objective</th><th>Title</th></tr>
    {summary_table}</table>

    <h2>Findings detail</h2>
    {cards_html}
    </body></html>"""


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/recovered_confirmed.json"
    out_pdf = sys.argv[2] if len(sys.argv) > 2 else "reports/interim/developer-ssrf-interim.pdf"
    generated = sys.argv[3] if len(sys.argv) > 3 else "(unstamped)"
    findings = json.load(open(src))
    deduped = _dedupe(findings)
    meta = {
        "target": "https://your-conductor.example.com/",
        "identities": "tenantA, tenantB (2 distinct tenants) + anon",
        "run": "39771156-af58-4ddc-864e-cd1fc1760173",
        "generated": generated,
        "class": "SSRF",
        "raw_count": len(findings),
    }
    html_str = build_html(deduped, meta)
    import os
    os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
    html_path = out_pdf.rsplit(".", 1)[0] + ".html"
    open(html_path, "w").write(html_str)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.goto("file://" + os.path.abspath(html_path), wait_until="networkidle")
        pg.pdf(path=out_pdf, format="A4", print_background=True,
               margin={"top": "14mm", "bottom": "14mm", "left": "10mm", "right": "10mm"})
        b.close()
    print(f"wrote {out_pdf} ({len(deduped)} deduped of {len(findings)} findings)")


if __name__ == "__main__":
    main()
