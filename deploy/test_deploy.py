"""Tests for the landible deploy shim — auth, deploy short-circuit, health rollup.

No network / no docker: subprocess.run is monkeypatched. Run: `uv run pytest`.
"""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import main

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("DEPLOY_TOKEN", TOKEN)


@pytest.fixture
def client():
    return TestClient(main.app)


def _fake_proc(returncode=0, stdout="", stderr=""):
    cp = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    return cp


# ---- auth -----------------------------------------------------------------

def test_deploy_requires_token(client, monkeypatch):
    monkeypatch.delenv("DEPLOY_TOKEN", raising=False)
    assert client.post("/api/deploy").status_code == 503  # token not configured


def test_deploy_rejects_bad_token(client):
    r = client.post("/api/deploy", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ---- deploy step runner ---------------------------------------------------

def test_deploy_runs_all_steps_on_success(client, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _fake_proc(0, stdout="ok")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")
    monkeypatch.setattr(main, "_shim_files_changed", lambda *a: False)

    r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [s["step"] for s in body["results"]] == [
        "git fetch", "git reset --hard origin/main", "compose pull", "compose up -d",
    ]


def test_deploy_409_when_already_running(client):
    """A concurrent deploy is rejected, not raced."""
    main._deploy_lock.acquire()
    try:
        r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 409
    finally:
        main._deploy_lock.release()


def test_deploy_short_circuits_on_first_failure(client, monkeypatch):
    """A failing step stops the run — later steps must NOT execute."""
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        # fail on the reset (2nd step)
        if "reset" in cmd:
            return _fake_proc(1, stderr="fatal: boom")
        return _fake_proc(0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")

    r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 500
    steps = [s["step"] for s in r.json()["results"]]
    assert steps == ["git fetch", "git reset --hard origin/main"]  # compose steps never ran
    assert r.json()["results"][-1]["status"] == "error"
    # compose was never invoked
    assert not any("compose" in c for c in seen)


# ---- landible-mcp redeploy -------------------------------------------------

def test_deploy_redeploys_mcp_when_mcp_changed(client, monkeypatch):
    """mcp/ changed -> uv sync + restart land in body['mcp'], not in results."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _fake_proc(0, stdout="ok")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    # two distinct SHAs so _paths_changed doesn't early-return on before==after
    shas = iter(["before", "after"])
    monkeypatch.setattr(main, "_read_commit", lambda: next(shas))
    monkeypatch.setattr(main, "_paths_changed", lambda *a: True)
    monkeypatch.setattr(main, "_shim_files_changed", lambda *a: False)

    r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    # compose results untouched; mcp redeploy reported separately
    assert [s["step"] for s in body["results"]] == [
        "git fetch", "git reset --hard origin/main", "compose pull", "compose up -d",
    ]
    assert body["mcp"] == {"step": "mcp redeploy", "status": "ok"}
    assert ["systemctl", "restart", "landible-mcp"] in calls


def test_deploy_skips_mcp_when_unchanged(client, monkeypatch):
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: _fake_proc(0, stdout="ok"))
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")  # before==after
    monkeypatch.setattr(main, "_shim_files_changed", lambda *a: False)

    r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert "mcp" not in r.json()


def test_mcp_restart_failure_does_not_500_the_deploy(client, monkeypatch):
    """A broken mcp restart is reported but must not fail the (done) compose roll."""
    def fake_run(cmd, **kw):
        if "restart" in cmd:
            return _fake_proc(1, stderr="Job for landible-mcp.service failed")
        return _fake_proc(0, stdout="ok")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    shas = iter(["before", "after"])
    monkeypatch.setattr(main, "_read_commit", lambda: next(shas))
    monkeypatch.setattr(main, "_paths_changed", lambda *a: True)
    monkeypatch.setattr(main, "_shim_files_changed", lambda *a: False)

    r = client.post("/api/deploy", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200  # compose deploy still succeeded
    assert r.json()["status"] == "ok"
    assert r.json()["mcp"]["step"] == "mcp restart"
    assert r.json()["mcp"]["status"] == "error"


# ---- health rollup --------------------------------------------------------

def _ps_lines(states):
    return "\n".join(json.dumps({"Service": s, "State": st, "Health": h}) for s, (st, h) in states.items())


def test_health_ok_when_all_running(client, monkeypatch):
    all_up = {s: ("running", "healthy") for s in main.EXPECTED_SERVICES}

    def fake_run(cmd, **kw):
        return _fake_proc(0, stdout=_ps_lines(all_up))

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_degraded_when_service_down(client, monkeypatch):
    states = {s: ("running", "healthy") for s in main.EXPECTED_SERVICES}
    states["chaptarr"] = ("exited", None)  # one down

    def fake_run(cmd, **kw):
        return _fake_proc(0, stdout=_ps_lines(states))

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")
    r = client.get("/api/health")
    assert r.status_code == 200  # always 200 — watchdog distinguishes via body
    assert r.json()["status"] == "degraded"


def test_health_degraded_when_service_missing(client, monkeypatch):
    """A service compose doesn't report at all (never created) counts as down."""
    states = {s: ("running", None) for s in main.EXPECTED_SERVICES if s != "autoheal"}

    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: _fake_proc(0, stdout=_ps_lines(states)))
    monkeypatch.setattr(main, "_read_commit", lambda: "abc123")
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert {"name": "autoheal", "state": "missing", "health": None} in body["containers"]


def test_self_restart_targets_landible_deploy(monkeypatch):
    calls = []
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _fake_proc(0))
    assert "self-restart scheduled" in main._schedule_self_restart()
    assert calls[0][-3:] == ["systemctl", "restart", "landible-deploy"]


def test_lidarr_relay_is_gone(client):
    """Music-only route: landible has no Lidarr."""
    assert client.post("/api/events/lidarr", json={"eventType": "Download"}).status_code in (404, 405)
