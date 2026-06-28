"""Substrate metadata reference pack (§15) — proves the infra chain is generic-by-data.

Asserts the §15 table is present as loadable data with the right per-cloud endpoints,
headers, the AWS IMDSv2 handshake, credential paths, versioning, and fingerprint inference.
"""
from common import substrates as sub

PACK = sub.load()


def test_pack_loads_versioned_with_all_substrates():
    # §15 freshness rule: versioned + as_of-stamped.
    assert PACK and "substrates" in PACK
    assert sub.version(PACK).startswith("substrates@v") and PACK.get("as_of")
    ids = {e["id"] for e in sub.entries(PACK)}
    # the full §15 table
    assert {"aws", "gcp", "azure", "oci", "alibaba", "digitalocean", "kubernetes"} <= ids


def test_per_substrate_endpoints_headers_match_spec():
    assert "169.254.169.254" in sub.lookup(PACK, "aws")["endpoints"]
    assert sub.lookup(PACK, "gcp")["header"] == "Metadata-Flavor: Google"
    assert sub.lookup(PACK, "azure")["header"] == "Metadata: true"
    assert sub.lookup(PACK, "oci")["header"] == "Authorization: Bearer Oracle"
    assert sub.lookup(PACK, "alibaba")["endpoints"] == ["100.100.100.200"]


def test_aws_imdsv2_handshake_is_present():
    # D11/§15: the IMDSv2 token dance must be data the chain can execute.
    hs = sub.lookup(PACK, "aws")["handshake"]
    assert hs["method"] == "PUT"
    assert "X-aws-ec2-metadata-token-ttl-seconds" in hs["request_header"]
    assert hs["response_token_header"] == "X-aws-ec2-metadata-token"
    assert any("iam/security-credentials" in p for p in sub.lookup(PACK, "aws")["credential_paths"])


def test_imds_probe_targets_are_http_ssrf_only():
    targets = sub.imds_probe_targets(PACK)
    assert len(targets) >= 6
    aws = [t for t in targets if t["substrate"] == "aws"]
    assert aws and all(t["url"].startswith("http://169.254.169.254") or "fd00" in t["url"] for t in aws)
    assert all(t["handshake"]["method"] == "PUT" for t in aws)          # AWS carries the handshake
    gcp = [t for t in targets if t["substrate"] == "gcp"]
    assert gcp and all(t["header"] == "Metadata-Flavor: Google" for t in gcp)
    assert all(t["access"] == "http" and t["url"].startswith("http") for t in targets)
    # §15: a filesystem secret is NOT an SSRF target — k8s/host don't appear here.
    assert {"kubernetes", "host"} & {t["substrate"] for t in targets} == set()


def test_file_secret_targets_cover_orchestrator_and_host():
    files = sub.file_secret_targets(PACK)
    paths = {f["path"] for f in files}
    assert "/var/run/secrets/kubernetes.io/serviceaccount/token" in paths   # k8s SA token (file)
    assert "/proc/self/environ" in paths and "~/.aws/credentials" in paths   # host secrets
    assert all(f["access"] == "file" for f in files)


def test_fingerprint_inference_picks_the_right_substrate():
    assert "aws" in sub.infer(PACK, "Server: nginx; X-Amz-Cf-Id: ...; awselb")
    assert "kubernetes" in sub.infer(PACK, "powered by ingress-nginx on k8s, KUBERNETES_SERVICE_HOST set")
    assert "gcp" in sub.infer(PACK, "hosted on Google Cloud / GKE")
    assert sub.infer(PACK, "nothing recognizable here") == []          # empty -> caller probes all


def test_missing_pack_returns_empty_not_raises():
    assert sub.load("/no/such/substrates.yaml") == {}
    assert sub.entries({}) == [] and sub.imds_probe_targets({}) == []
