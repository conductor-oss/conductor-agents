"""Logic cores of the wiring items: P1-1 intel feeds, P1-4 IMDSv2/replay, P1-5 sitemap, P2-1 chaining."""
from common import deps, substrates as sub, sitemap, chaining


# ── P1-1: KEV / EPSS feed parsers (§5/§14) ───────────────────────────────────────────
def test_kev_and_epss_parsers():
    kev = deps.parse_kev({"vulnerabilities": [{"cveID": "CVE-2021-44228"}, {"cveID": "CVE-2017-5638"}]})
    assert kev == {"CVE-2021-44228", "CVE-2017-5638"}
    epss = deps.parse_epss({"data": [{"cve": "CVE-2021-44228", "epss": "0.97"}, {"cve": "X", "epss": "bad"}]})
    assert epss["CVE-2021-44228"] == 0.97 and "X" not in epss          # malformed row dropped, not coerced
    # and they plug into prioritize: a KEV+high-EPSS CVE rises
    recs = deps.prioritize([{"dependency": "log4j@2.14", "version_known": True,
                             "top": [{"id": "CVE-2021-44228", "severity": "critical"}]}],
                           kev=kev, epss=epss, exploitable=kev)
    assert recs[0]["kev"] and recs[0]["top"][0]["epss"] == 0.97


# ── P1-4: IMDSv2 handshake plan + bounded replay-validation (§15/D11) ────────────────
def test_aws_imdsv2_plan_orders_token_then_read_with_token_header():
    plan = sub.imdsv2_plan(sub.lookup(sub.load(), "aws"))
    assert plan[0]["method"] == "PUT" and "X-aws-ec2-metadata-token-ttl-seconds" in plan[0]["headers"]
    assert plan[0]["captures"] == "X-aws-ec2-metadata-token"
    reads = [s for s in plan[1:] if s["method"] == "GET"]
    assert reads and all("X-aws-ec2-metadata-token" in s["headers"] for s in reads)   # token carried to the read


def test_gcp_plan_single_get_with_required_header():
    plan = sub.imdsv2_plan(sub.lookup(sub.load(), "gcp"))
    assert all(s["method"] == "GET" for s in plan)                       # no handshake
    assert all(s["headers"].get("Metadata-Flavor") == "Google" for s in plan)


def test_replay_check_is_bounded_and_read_only():
    chk = sub.replay_check("aws")
    assert chk["bounded"] is True and chk["read_only"] is True and "get-caller-identity" in chk["check"]


# ── P1-5: sitemap enumeration + relevance filter (§11) ───────────────────────────────
def test_sitemap_extracts_and_filters_security_pages():
    xml = """<urlset>
      <url><loc>https://x/content/access-control-applications</loc></url>
      <url><loc>https://x/content/blog/marketing-post</loc></url>
      <url><loc>https://x/content/secrets-management</loc></url>
    </urlset>"""
    assert len(sitemap.urls(xml)) == 3
    rel = sitemap.security_relevant(xml)
    assert any("access-control" in u for u in rel) and any("secrets" in u for u in rel)
    assert not any("marketing" in u for u in rel)                        # noise filtered out


# ── P2-1: incremental attack-graph chaining (§5/§8) ──────────────────────────────────
def test_chaining_unlocks_deeper_objectives_and_builds_graph_edges():
    # a confirmed secret leak unlocks "credential_held" -> deeper authz/cross-tenant objectives
    leak = {"objective_id": "INFRA-SECRET-SURFACE", "category": "secret_exposure", "finding_sig": "n1"}
    assert "credential_held" in chaining.preconditions(leak)
    assert "AUTHZ-FUNCTION-LEVEL" in chaining.unlocked_objectives(chaining.preconditions(leak))

    g = chaining.attach({"nodes": [], "edges": []}, leak)
    # now a confirmed function-level authz finding chains FROM the leak (machine-driven edge)
    g = chaining.attach(g, {"objective_id": "AUTHZ-FUNCTION-LEVEL", "category": "authz", "finding_sig": "n2"})
    assert {"from": "n1", "to": "n2", "via": "chaining"} in g["edges"]
    assert len(g["nodes"]) == 2


def test_admin_privesc_unlocks_engine_level_exploitation():
    pre = chaining.preconditions({
        "objective_id": "INTEG-PRIVESC-MASSASSIGN",
        "category": "privesc",
    })
    unlocked = chaining.unlocked_objectives(pre)
    assert "privileged_access" in pre
    assert {"INFRA-SSRF", "INFRA-RCE-INJECTION", "CONF-CROSS-TENANT-READ"} <= set(unlocked)
