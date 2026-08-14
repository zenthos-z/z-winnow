"""Tests for scheduler.preflight — dependency checks with mocked probes."""

from __future__ import annotations

import aiosqlite

from z_winnow.scheduler import preflight as pf


async def _make_db(tmp_path) -> str:
    db = tmp_path / "t.db"
    async with aiosqlite.connect(db) as c:
        await c.execute("CREATE TABLE t(x)")
    return str(db)


async def test_db_ok_and_fail(tmp_path):
    ok = str(tmp_path / "ok.db")
    async with aiosqlite.connect(ok) as c:
        await c.execute("CREATE TABLE t(x)")
    r = await pf._check_db(ok)
    assert r.status == "ok"

    r2 = await pf._check_db(str(tmp_path / "nope" / "missing.db"))
    # connecting to a path under a missing dir fails at execute/connect
    assert r2.status == "fail"
    assert r2.critical is True


async def test_check_environment_mock_mode_skips_docker(tmp_path, monkeypatch):
    # In mock mode (real_memos=False, real_llm=False) docker/containers/qdrant/memos/llm are skipped.
    async def fake_probe(values, targets=None):
        return {t: "ok" for t in (targets or [])}

    monkeypatch.setattr("z_winnow.web.services.config_service.probe_connectivity", fake_probe)
    db = await _make_db(tmp_path)

    report = await pf.check_environment(db, real_llm=False, real_memos=False)

    assert report.get(pf.CONTAINERS).status == "skip"
    assert report.get(pf.QDRANT).status == "skip"
    assert report.get(pf.MEMOS).status == "skip"
    assert report.get(pf.LLM).status == "skip"
    assert report.get(pf.CIPHERTALK).status == "ok"
    assert report.get(pf.DB).status == "ok"
    assert report.ok is True
    assert report.critical_failures == []


async def test_check_environment_docker_down_blocks(tmp_path, monkeypatch):
    # real_memos=True + docker missing -> critical failures include docker + containers.
    async def fake_probe(values, targets=None):
        return {t: "ok" for t in (targets or [])}

    monkeypatch.setattr("z_winnow.web.services.config_service.probe_connectivity", fake_probe)
    monkeypatch.setattr("shutil.which", lambda name: None)  # no docker binary

    db = await _make_db(tmp_path)
    report = await pf.check_environment(db, real_llm=True, real_memos=True)

    assert report.get(pf.DOCKER).status == "fail"
    assert report.get(pf.CONTAINERS).status == "fail"
    names = {c.name for c in report.critical_failures}
    assert {pf.DOCKER, pf.CONTAINERS}.issubset(names)
    assert report.ok is False


async def test_check_environment_ciphertalk_fail_blocks(tmp_path, monkeypatch):
    async def fake_probe(values, targets=None):
        return {t: "fail: connection refused" for t in (targets or [])}

    monkeypatch.setattr("z_winnow.web.services.config_service.probe_connectivity", fake_probe)
    db = await _make_db(tmp_path)

    report = await pf.check_environment(db, real_llm=False, real_memos=False)
    assert report.get(pf.CIPHERTALK).status == "fail"
    assert any(c.name == pf.CIPHERTALK for c in report.critical_failures)
    assert report.ok is False


async def test_qdrant_missing_collection_is_critical(tmp_path, monkeypatch):
    class _Resp:
        status_code = 404

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(pf.httpx, "AsyncClient", _Client)
    r = await pf._check_qdrant_collection()
    assert r.status == "warn"
    assert r.critical is True  # missing collection writes 404 -> must block


def test_to_compact_line():
    report = pf.PreflightReport()
    report.add(pf.CheckResult(pf.DOCKER, "ok", ""))
    report.add(pf.CheckResult(pf.CONTAINERS, "ok", ""))
    report.add(pf.CheckResult(pf.CIPHERTALK, "fail", "x"))
    report.add(pf.CheckResult(pf.LLM, "skip", ""))
    line = pf.to_compact_line(report)
    assert "Docker✓" in line
    assert "容器✓" in line
    assert "数据源✗" in line
    assert "LLM" not in line  # skipped items omitted


def test_check_result_icon():
    assert pf.CheckResult("x", "ok", "").icon == "✓"
    assert pf.CheckResult("x", "fail", "").icon == "✗"
    assert pf.CheckResult("x", "warn", "").icon == "⚠"
    assert pf.CheckResult("x", "skip", "").icon == "–"
