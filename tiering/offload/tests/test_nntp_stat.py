import gzip

import pytest

from fakes_intake import FakeNntp, make_nzb, self_signed_cert
from tiering.offload import nntp_stat as ns

IDS = [f"seg{i}@t" for i in range(53)]


@pytest.fixture
def nzb(tmp_path):
    p = tmp_path / "a.nzb"
    p.write_bytes(make_nzb(IDS))
    return str(p)


def test_all_present(nzb):
    s = FakeNntp(IDS)
    try:
        r = ns.check_nzb(nzb, s.cfg())
    finally:
        s.close()
    assert r == {"total": 53, "missing": 0, "missing_ids": []}
    assert s.stats == 53
    assert 1 <= s.connections <= 3  # pooled


def test_missing_ids_reported(nzb):
    s = FakeNntp(IDS[:10] + IDS[12:])
    try:
        r = ns.check_nzb(nzb, s.cfg())
    finally:
        s.close()
    assert r["missing"] == 2 and r["missing_ids"] == IDS[10:12]


def test_missing_ids_capped_at_100(tmp_path):
    ids = [f"m{i}@t" for i in range(250)]
    p = tmp_path / "b.nzb.gz"
    p.write_bytes(gzip.compress(make_nzb(ids)))
    s = FakeNntp([])
    try:
        r = ns.check_nzb(str(p), s.cfg())
    finally:
        s.close()
    assert r["total"] == 250 and r["missing"] == 250 and len(r["missing_ids"]) == 100


def test_tls_with_self_signed_cert(tmp_path, nzb):
    cert = self_signed_cert(tmp_path)
    if cert is None:
        pytest.skip("no openssl")
    s = FakeNntp(IDS, cert=cert)
    try:
        r = ns.check_nzb(nzb, s.cfg(tls=True, cafile=cert[0], host="localhost"))
        assert r["missing"] == 0
        with pytest.raises(ns.NntpUnavailable) as e:  # cert not trusted without cafile
            ns.check_nzb(nzb, s.cfg(tls=True, host="localhost"))
        assert e.value.kind == "network"
    finally:
        s.close()


def test_bad_login_is_auth_not_missing(nzb):
    s = FakeNntp(IDS)
    s.refuse_auth = True
    try:
        with pytest.raises(ns.NntpUnavailable) as e:
            ns.check_nzb(nzb, s.cfg())
    finally:
        s.close()
    assert e.value.kind == "auth"


def test_quota_is_quota_not_missing(nzb):
    s = FakeNntp(IDS)
    s.quota = True
    try:
        with pytest.raises(ns.NntpUnavailable) as e:
            ns.check_nzb(nzb, s.cfg())
    finally:
        s.close()
    assert e.value.kind == "quota"


def test_refused_greeting_is_auth(nzb):
    s = FakeNntp(IDS)
    s.greeting_code = "502"
    try:
        with pytest.raises(ns.NntpUnavailable) as e:
            ns.check_nzb(nzb, s.cfg())
    finally:
        s.close()
    assert e.value.kind == "auth"


def test_server_down_is_network(nzb):
    s = FakeNntp(IDS)
    cfg = s.cfg()
    s.close()
    with pytest.raises(ns.NntpUnavailable) as e:
        ns.check_nzb(nzb, cfg)
    assert e.value.kind == "network"


def test_checker_reuses_connections(nzb):
    s = FakeNntp(IDS)
    try:
        c = ns.Checker(s.cfg(conns=2))
        c(nzb)
        n = s.connections
        c(nzb)
        assert s.connections == n
        c.close()
    finally:
        s.close()


def test_odd_ids_count_as_missing_without_breaking_the_stream(tmp_path):
    p = tmp_path / "c.nzb"
    p.write_bytes(make_nzb(["ok@t", "bad id@t", "ok2@t"]))
    s = FakeNntp(["ok@t", "ok2@t"])
    try:
        r = ns.check_nzb(str(p), s.cfg())
    finally:
        s.close()
    assert r["total"] == 3 and r["missing_ids"] == ["bad id@t"]


def test_from_env_and_repr_hides_secrets():
    env = {"NNTP_PRIMARY_HOST": "h.example", "NNTP_PRIMARY_PORT": "119", "NNTP_PRIMARY_USER": "me",
           "NNTP_PRIMARY_PASS": "hunter2", "NNTP_PRIMARY_CONNS": "4"}
    c = ns.ProviderCfg.from_env("primary", env)
    assert (c.port, c.conns, c.tls) == (119, 4, True)
    assert "hunter2" not in repr(c) and "h.example" not in repr(c)
    assert ns.ProviderCfg.from_env("secondary", env) is None
