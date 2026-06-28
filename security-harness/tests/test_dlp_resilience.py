"""E2 data-classification + E3 resilience knee analysis."""
from common import authz, loadknee, sensitive


def test_classify_buckets_into_data_classes():
    text = "user joe@x.com card 4111111111111111 conn postgres://u:p@db.internal:5432/app"
    c = sensitive.classify(text)
    assert c["found"] is True
    assert c["classes"].get("pii", 0) >= 1          # email
    assert c["classes"].get("pci", 0) >= 1          # card
    assert c["classes"].get("financial", 0) >= 1    # card
    assert c["classes"].get("secret", 0) >= 1       # connection string
    assert c["classes"].get("infra", 0) >= 1        # connection string host


def test_classify_clean_text():
    assert sensitive.classify("hello world")["found"] is False


def test_imds_and_private_ip_flagged_infra():
    c = sensitive.classify("fetched http://169.254.169.254/latest/meta-data and 10.0.0.5")
    assert c["classes"].get("infra", 0) >= 2


def test_credit_card_requires_luhn():
    """A 16-digit value that fails Luhn (cluster/account/vpc IDs) must NOT be flagged as a card —
    this FP was halting read-only control-plane scans on /api/cluster."""
    assert sensitive.scan("clusterUniqueId 1234567812345678")["pii"].get("credit_card") is None
    assert sensitive.scan("card 4111111111111111")["pii"].get("credit_card") == 1   # real test card (Luhn-valid)


def test_private_ip_is_infra_not_pii_and_excluded_from_halt():
    """RFC1918 IPs / VPC CIDRs are infra metadata returned by design — bucketed as infra, NOT pii,
    so they don't feed the 'bulk access to real secrets/PII' halt."""
    s = sensitive.scan("vpcCidr 10.9.0.0/16 and 10.1.0.0/16 nodes 192.168.1.5")
    assert "private_ip" in s["infra"] and "private_ip" not in s["pii"]
    assert s["found"] is False                                  # infra-only -> not 'sensitive'
    assert sum(s["secrets"].values()) + sum(s["pii"].values()) == 0   # halt counts zero


def test_resilience_off_by_default():
    assert authz.resilience_allowed({}) is False
    assert authz.resilience_allowed({"allowed_classes": ["bola"]}) is False
    assert authz.resilience_allowed({"allowed_classes": ["resilience"]}) is True


def test_knee_detected_when_latency_inflects():
    steps = [
        {"concurrency": 1, "p95_ms": 100, "error_rate": 0.0},
        {"concurrency": 2, "p95_ms": 120, "error_rate": 0.0},
        {"concurrency": 4, "p95_ms": 800, "error_rate": 0.0},   # 8x baseline -> knee
    ]
    a = loadknee.analyze(steps)
    assert a["degraded"] is True
    assert a["knee_at"] == 4


def test_no_knee_when_flat():
    steps = [{"concurrency": c, "p95_ms": 100 + c, "error_rate": 0.0} for c in (1, 2, 4, 8)]
    a = loadknee.analyze(steps)
    assert a["degraded"] is False and a["knee_at"] is None


def test_step_verdict_aborts_on_high_error():
    v = loadknee.step_verdict({"concurrency": 8, "p95_ms": 100, "error_rate": 0.6}, 100)
    assert v["abort"] is True
