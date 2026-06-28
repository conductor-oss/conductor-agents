"""hc worker package: the self-improving meta-loop's runtime task (design §19).

``hc_analyze`` is the read-only analysis agent that proposes config improvements from the
trace corpus (§19.2). Importing this module registers the @worker_task. The write-back half
(champion promotion) is gated on the benchmark + ratification (§19.4-6) and runs out-of-band,
not inside an assessment campaign. (Start-of-loop CVE/metadata freshness, §5/D12, is not a
separate task — it is inline in ``dep_cve_scan`` via ``_intel_feeds`` + ``substrates_version``.)"""

from . import tasks  # noqa: F401
