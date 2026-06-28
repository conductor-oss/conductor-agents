"""Hill-climbing worker task (design §19 meta-loop).

  hc_analyze     — §19.2 / P4-a: the READ-ONLY half of the self-improvement loop. It mines
                   the structured trace corpus, keeps only corroborated (multi-trace, H4)
                   failure signatures, sanitizes them (H7: traces are untrusted), and emits
                   config-change PROPOSALS via the diagnosis→surface map (§19.5). It performs
                   NO write-back — proposals go to the benchmark gate + ratification (§19.4-6).

The §5/D12 start-of-loop intel refresh (CISA KEV + FIRST EPSS + substrate-pack version,
``feeds_as_of`` stamp) is NOT a separate task: it lives inline in ``dep_cve_scan``
(``workers/recon/tasks.py`` ``_intel_feeds`` + ``substrates_version``), which is already wired
into ``deep_assess`` and threads ``feeds_as_of`` into the dossier. Keeping it there avoids a
redundant second live KEV/EPSS fetch.

Workers never raise: any failure is returned in the result dict so the loop can't crash.
"""

from __future__ import annotations

import datetime
import logging

from conductor.client.worker.worker_task import worker_task

from common import trace, hillclimb

log = logging.getLogger(__name__)


def _now() -> str:
    # Workers may read the wall clock (only workflow *scripts* may not).
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@worker_task(task_definition_name="hc_analyze")
def hc_analyze(task):
    inp = task.input_data or {}
    records = inp.get("trace_records")
    if not records and inp.get("trace_path"):
        records = trace.load(inp["trace_path"])
    records = records or []
    benchmark = inp.get("benchmark") if isinstance(inp.get("benchmark"), dict) else {}
    min_count = int(inp.get("min_count") or 2)

    # H7: sanitize untrusted trace content before it informs any proposal.
    safe = [hillclimb.sanitize_trace(r) for r in records]
    recurring = trace.recurring(safe, min_count=min_count)       # H4: corroborated signal only
    proposals = hillclimb.propose(recurring, benchmark)          # §19.5 diagnosis→surface map
    return {
        "mode": "read-only",                                     # P4-a: proposes, never writes back
        "recurring_signatures": len(recurring),
        "proposals": proposals,
        "unmeasured": hillclimb.unmeasured_warnings(benchmark),  # §19.2: don't auto-tune unmeasured classes
        "as_of": _now(),
    }
