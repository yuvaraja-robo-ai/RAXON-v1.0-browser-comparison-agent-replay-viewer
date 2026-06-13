"""Tests for the S9 web UI backend (api_server.py).

Covers the §18 viewer payload plus the two UX features layered on top:
live run status (progress banner) and offline backup/export (zip bundles).
Everything runs against a synthetic capture session written to a temp
SESSIONS_ROOT — no browser, no gateway, no network.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_server
import persistence


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with SESSIONS_ROOT/BACKUPS_ROOT redirected to a temp tree
    holding one synthetic capture session."""
    sessions = tmp_path / "sessions"
    sid = "cap-test1234"
    sdir = sessions / sid
    (sdir / "browser").mkdir(parents=True)
    (sdir / "query.txt").write_text("Compare top 3 HF text-generation models")
    (sdir / "browser" / "step_00_full.png").write_bytes(b"\x89PNG fake")
    manifest = {
        "kind": "capture", "session_id": sid, "url": "https://huggingface.co/models",
        "goal": "Compare top 3 HF text-generation models",
        "turns": 2,
        "snapshots": [{"turn": 0, "screenshot": "step_00_full.png"}],
        "records": [{"name": "org/model-a", "likes": 100, "downloads": 5,
                     "parameter_count": "7B", "url": "https://x", "description": "d"}],
        "actions": [
            {"layer": "capture", "turn": 0, "action": "goto",
             "target": "https://huggingface.co/models",
             "screenshot": "browser/step_00_full.png"},
            {"layer": "capture", "turn": 1, "action": "click",
             "target": "li:has-text('Most likes')", "screenshot": None},
        ],
        "authenticated": False, "elapsed_s": 7.0, "error": None,
    }
    (sdir / "capture.json").write_text(json.dumps(manifest))

    for mod in (api_server, persistence):
        monkeypatch.setattr(mod, "SESSIONS_ROOT", sessions)
    monkeypatch.setattr(api_server, "BACKUPS_ROOT", tmp_path / "backups")
    monkeypatch.setattr(api_server, "_fetch_cost",
                        lambda _sid: {"ok": True, "by_agent": {}, "summary": [],
                                      "total_dollars": 0.0})
    api_server._RUNS.clear()
    return TestClient(api_server.app), sid


def test_sessions_and_panel(client):
    c, sid = client
    rows = c.get("/api/sessions").json()["sessions"]
    assert [r["sid"] for r in rows] == [sid]
    assert rows[0]["kind"] == "capture" and rows[0]["running"] is False

    p = c.get(f"/api/session/{sid}").json()
    # the eight §18 criteria surface
    assert p["goal"].startswith("Compare top 3")            # 1 goal
    assert p["path"] == "capture"                           # 3 path
    assert len(p["actions"]) == 2                           # 4 actions
    assert p["screenshots"] == ["step_00_full.png"]         # 5 screenshots
    assert p["table"]["records"][0]["name"] == "org/model-a"  # 6+7 data/table
    assert p["turns"] == 2 and p["cost"]["ok"]              # 8 turns+cost


def test_run_status_progress(client):
    c, sid = client
    # unknown session → done, not known
    st = c.get("/api/session/nope/status").json()
    assert st == {"sid": "nope", "known": False, "done": True}

    # live capture run with an engine progress event → banner payload
    api_server._RUNS[sid] = {
        "done": False, "error": None, "t0": 0.0, "kind": "capture",
        "progress": {"stage": "step", "message": "clicking li:has-text('Most likes')",
                     "step": 2, "total": 5},
    }
    st = c.get(f"/api/session/{sid}/status").json()
    assert st["known"] and not st["done"]
    assert st["progress"]["step"] == 2 and st["progress"]["total"] == 5
    assert "clicking" in st["progress"]["message"]

    # the sessions list mirrors the stage message while running
    row = c.get("/api/sessions").json()["sessions"][0]
    assert row["running"] and "clicking" in row["stage"]


def test_export_session_zip(client):
    c, sid = client
    r = c.get(f"/api/session/{sid}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert f"{sid}/capture.json" in names
    assert f"{sid}/browser/step_00_full.png" in names
    panel = json.loads(z.read(f"{sid}/panel.json"))
    assert panel["table"]["records"]            # offline §18 payload rides along
    assert c.get("/api/session/missing/export").status_code == 404


def test_delete_session(client):
    c, sid = client
    # a live run is protected (worker still writes into the session dir)
    api_server._RUNS[sid] = {"done": False, "error": None, "t0": 0.0, "kind": "capture"}
    assert c.delete(f"/api/session/{sid}").status_code == 409
    assert c.get("/api/sessions").json()["sessions"]      # still there

    api_server._RUNS[sid]["done"] = True
    r = c.delete(f"/api/session/{sid}")
    assert r.status_code == 200 and r.json() == {"ok": True, "sid": sid}
    assert c.get("/api/sessions").json()["sessions"] == []
    assert c.get(f"/api/session/{sid}").status_code == 404

    # gone is gone; traversal out of SESSIONS_ROOT is guarded
    assert c.delete(f"/api/session/{sid}").status_code == 404
    assert c.delete("/api/session/%2e%2e").status_code in (400, 404)
    assert c.delete("/api/session/%2e").status_code in (400, 404)


def test_backup_all_and_download(client):
    c, sid = client
    assert c.get("/api/backups").json()["backups"] == []
    r = c.post("/api/backup").json()
    assert r["ok"] and r["sessions"] == 1 and r["size"] > 0

    listed = c.get("/api/backups").json()["backups"]
    assert len(listed) == 1 and listed[0]["name"] == r["name"]

    dl = c.get(f"/api/backup/{r['name']}")
    assert dl.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(dl.content))
    assert f"{sid}/capture.json" in z.namelist()
    idx = json.loads(z.read("backup_index.json"))
    assert idx["sessions"] == [{"sid": sid, "kind": "capture"}]
    # path traversal guarded
    assert c.get("/api/backup/../../secrets.zip").status_code in (404, 400)
