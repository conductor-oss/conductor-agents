# Outputs

Every run writes to `reports/<scan-id>/`:

| File | Contents |
|---|---|
| `report.pdf` / `report.md` | Human-readable findings report |
| `findings.json` | Structured findings — severity, CWE, OWASP, reproduction steps |
| `report.sarif` | SARIF 2.1.0 — ingest into GitHub code scanning, DefectDojo, or your SIEM |
| `dossier.json` | (`./assess` only) Attack graph + confirmed/blind findings + residual-risk statement |

The `report.pdf` is rendered by Conductor's `GENERATE_PDF` task, and `report.sarif` conforms to SARIF 2.1.0 so findings drop straight into existing code-scanning and vulnerability-management pipelines. The `dossier.json` produced by [`./assess`](scan-vs-assess.md) captures the incrementally-assembled attack graph and residual-risk statement described in [Architecture](architecture.md).
