"""RAXON (S9) — FastAPI backend + static UI host for the Browser Comparison agent.

Sits BESIDE flow.py and the Browser skill; never modifies either. It exposes:

  • viewer endpoints (no LLM): list sessions, read a session's §18 panels
    (goal, planner DAG, browser path, action log, screenshots, extracted
    records, comparison table, turns+cost) — works fully offline so persisted
    runs and capture bundles are reviewable with the model off.
  • launch endpoints (need the Orin for DAG runs; capture needs neither model
    nor gateway): POST /api/run starts the DAG Executor in a worker thread;
    POST /api/capture runs the deterministic capture engine; POST /api/login
    does a manual-login handoff (headed browser) and saves a reusable session.

The persistence dir (state/sessions/<sid>/) is the seam — both DAG runs and
capture bundles land there, so the UI lists and renders them uniformly with no
flow.py hook. Model-on-Orin rule preserved: this is an RPi5 app component; it
only calls the gateway (which forwards to the Orin), never a local model.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import capture_engine
from gateway import GATEWAY_URL
from persistence import SESSIONS_ROOT, SessionStore, list_sessions
from schemas import AgentResult

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
BACKUPS_ROOT = ROOT / "state" / "backups"

app = FastAPI(title="RAXON — Browser Comparison Terminal", version="1.0")

# in-process run registry (DAG runs + captures) keyed by session id
_RUNS: dict[str, dict] = {}
_run_lock = threading.Lock()


# ── request models ───────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    query: str


class CaptureRequest(BaseModel):
    url: str
    goal: str = ""
    steps: list[dict] | None = None
    want: int = 3
    auth: str | None = None        # name of a saved storage_state (state/auth/<name>.json)


class LoginRequest(BaseModel):
    login_url: str
    name: str
    wait_seconds: int = 180


# ── helpers ──────────────────────────────────────────────────────────────────
def _kind(sid: str) -> str:
    d = SESSIONS_ROOT / sid
    if (d / "capture.json").exists():
        return "capture"
    if (d / "graph.json").exists() or (d / "nodes").exists():
        return "dag"
    return "unknown"


def _read_capture(sid: str) -> dict:
    return json.loads((SESSIONS_ROOT / sid / "capture.json").read_text())


def _fetch_cost(sid: str) -> dict:
    """Per-agent cost rollup from the gateway; offline-tolerant. Includes a
    pre-summed `summary` (per-agent in/out tokens + dollars) and grand total
    so the UI can render §18 criterion 8 without re-aggregating ledger rows."""
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/cost/by_agent", params={"session": sid}, timeout=3)
        if r.status_code == 200:
            data = r.json()
            summary, total = [], 0.0
            if isinstance(data, dict):
                for agent, rows in sorted(data.items()):
                    rows = rows or []
                    dollars = sum(float(x.get("dollars") or 0.0) for x in rows)
                    total += dollars
                    summary.append({
                        "agent": agent,
                        "calls": len(rows),
                        "in_tok": sum(int(x.get("in_tok") or 0) for x in rows),
                        "out_tok": sum(int(x.get("out_tok") or 0) for x in rows),
                        "dollars": round(dollars, 4),
                    })
            return {"ok": True, "by_agent": data, "summary": summary,
                    "total_dollars": round(total, 4)}
    except Exception:
        pass
    return {"ok": False, "by_agent": {}, "summary": [], "total_dollars": None}


def _dag_view(store: SessionStore) -> dict | None:
    """{nodes, edges} snapshot of the growing DAG from disk (graph + per-node)."""
    graph = store.read_graph()
    if graph is None:
        return None
    states = {s.node_id: s for s in store.read_all_nodes()}
    nodes = []
    for nid, d in graph.nodes(data=True):
        st = states.get(nid)
        res = st.result if (st and isinstance(st.result, AgentResult)) else None
        output = res.output if res else {}
        meta = d.get("metadata") or {}
        nodes.append({
            "id": nid,
            "skill": d.get("skill"),
            "status": d.get("status", "pending"),
            "elapsed": round(res.elapsed_s, 2) if res else None,
            "provider": res.provider if res else None,
            "inputs": d.get("inputs", []),
            "recovery": meta.get("recovery_reason"),
            "output": output,
            "error": (res.error if res else None),
        })
    edges = [{"source": u, "target": v} for u, v in graph.edges()]
    return {"nodes": nodes, "edges": edges}


def _dag_progress(sid: str) -> dict:
    """Live DAG stage from disk: nodes done/total + what is running now."""
    try:
        store = SessionStore(sid)
        graph = store.read_graph()
    except Exception:
        graph = None
    if graph is None:
        return {"stage": "plan", "message": "planning — building the DAG…"}
    statuses = {nid: (d.get("status", "pending"), d.get("skill"))
                for nid, d in graph.nodes(data=True)}
    total = len(statuses)
    done = sum(1 for s, _ in statuses.values() if s in ("complete", "skipped"))
    running = [f"{nid} ({skill})" for nid, (s, skill) in statuses.items() if s == "running"]
    failed = [nid for nid, (s, _) in statuses.items() if s == "failed"]
    if running:
        msg = f"running {', '.join(running)}"
    elif failed:
        msg = f"node failed: {', '.join(failed)} — recovering"
    elif done >= total:
        msg = "all nodes complete — finalising"
    else:
        msg = "scheduling next node…"
    return {"stage": "dag", "step": done, "total": total, "message": msg}


def _run_status(sid: str) -> dict:
    """Status for one run (UI progress banner): done flag, elapsed, and a
    human-readable progress message (capture callback or live DAG state)."""
    run = _RUNS.get(sid)
    if not run:
        return {"sid": sid, "known": False, "done": True}
    out = {"sid": sid, "known": True, "done": run["done"], "kind": run.get("kind"),
           "error": run.get("error"), "elapsed": round(time.time() - run["t0"], 1)}
    if not run["done"]:
        out["progress"] = (_dag_progress(sid) if run.get("kind") == "dag"
                           else run.get("progress")
                           or {"stage": "start", "message": "starting…"})
    return out


def _comparison_table(sid: str, kind: str) -> dict | None:
    """The §18 comparison table: capture records, or a DAG distiller's records,
    or a markdown table in the formatter's final_answer."""
    if kind == "capture":
        recs = _read_capture(sid).get("records") or []
        return {"records": recs} if recs else None
    store = SessionStore(sid)
    records = None
    final_md = None
    for st in store.read_all_nodes():
        res = st.result if isinstance(st.result, AgentResult) else None
        out = res.output if res else {}
        if not isinstance(out, dict):
            continue
        if out.get("records"):
            records = out["records"]
        fa = out.get("final_answer")
        if isinstance(fa, str) and "|" in fa and "---" in fa:
            final_md = fa
    if records:
        return {"records": records}
    if final_md:
        return {"markdown": final_md}
    return None


def _browser_panels(sid: str) -> dict:
    """Unified §18 panel payload for one session (DAG or capture)."""
    kind = _kind(sid)
    store = SessionStore(sid)
    try:
        goal = store.read_query()
    except Exception:
        goal = ""
    panel: dict = {"sid": sid, "kind": kind, "goal": goal}

    if kind == "capture":
        man = _read_capture(sid)
        panel.update({
            "url": man.get("url"),
            "path": "capture",
            "turns": man.get("turns"),
            "actions": man.get("actions", []),
            "screenshots": [s["screenshot"] for s in man.get("snapshots", [])],
            "snapshots": man.get("snapshots", []),
            "authenticated": man.get("authenticated"),
            "elapsed_s": man.get("elapsed_s"),
            "error": man.get("error"),
        })
    else:
        view = _dag_view(store) or {"nodes": [], "edges": []}
        panel["dag"] = view
        # browser-skill node(s): path, action log, screenshots, turns
        actions, screenshots, turns, bpath = [], [], 0, None
        for n in view["nodes"]:
            if n.get("skill") == "browser" and isinstance(n.get("output"), dict):
                o = n["output"]
                bpath = o.get("path") or bpath
                turns += int(o.get("turns") or 0)
                for a in (o.get("actions") or []):
                    actions.append(a)
                    shot = a.get("screenshot")
                    if shot and shot not in screenshots:
                        screenshots.append(shot)
        panel.update({"path": bpath, "turns": turns,
                      "actions": actions, "screenshots": screenshots})

    panel["table"] = _comparison_table(sid, kind)
    panel["cost"] = _fetch_cost(sid)
    return panel


# ── viewer API ───────────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    up = False
    try:
        up = httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=2).status_code == 200
    except Exception:
        up = False
    return {"app": "s9-browser", "gateway": {"url": GATEWAY_URL, "up": up},
            "sessions": len(list_sessions()),
            "auth_sessions": capture_engine.list_auth_sessions()}


@app.get("/api/sessions")
def sessions() -> dict:
    out = []
    for sid in reversed(list_sessions()):
        d = SESSIONS_ROOT / sid
        kind = _kind(sid)
        goal = ""
        try:
            goal = (d / "query.txt").read_text().strip()[:140]
        except Exception:
            pass
        run = _RUNS.get(sid)
        running = bool(run and not run.get("done"))
        row = {
            "sid": sid, "kind": kind, "goal": goal,
            "mtime": d.stat().st_mtime if d.exists() else 0,
            "running": running,
        }
        if running:
            row["stage"] = (_run_status(sid).get("progress") or {}).get("message")
        out.append(row)
    return {"sessions": out}


@app.get("/api/session/{sid}")
def session_detail(sid: str) -> dict:
    if not (SESSIONS_ROOT / sid).exists():
        raise HTTPException(404, f"no session {sid}")
    return _browser_panels(sid)


@app.delete("/api/session/{sid}")
def delete_session(sid: str) -> dict:
    """Remove one persisted session (artifacts + registry entry). Running
    sessions are refused — deleting the dir would race the worker thread
    still writing snapshots into it."""
    root = SESSIONS_ROOT.resolve()
    d = (SESSIONS_ROOT / sid).resolve()
    if d == root or not str(d).startswith(str(root) + os.sep) or not d.is_dir():
        raise HTTPException(404, f"no session {sid}")
    run = _RUNS.get(sid)
    if run and not run.get("done"):
        raise HTTPException(409, f"session {sid} is still running")
    shutil.rmtree(d)
    _RUNS.pop(sid, None)
    return {"ok": True, "sid": sid}


@app.get("/api/session/{sid}/artifact/{path:path}")
def artifact(sid: str, path: str):
    f = (SESSIONS_ROOT / sid / "browser" / path).resolve()
    base = (SESSIONS_ROOT / sid / "browser").resolve()
    if not str(f).startswith(str(base)) or not f.exists():
        raise HTTPException(404, f"no artifact {path}")
    return FileResponse(str(f))


# ── offline backup / export API ─────────────────────────────────────────────
def _zip_session(z: zipfile.ZipFile, sid: str) -> None:
    sdir = SESSIONS_ROOT / sid
    for f in sorted(sdir.rglob("*")):
        if f.is_file():
            z.write(f, f"{sid}/{f.relative_to(sdir)}")
    # self-describing: the rendered §18 panel payload rides along so the
    # bundle is reviewable offline without this server.
    z.writestr(f"{sid}/panel.json",
               json.dumps(_browser_panels(sid), default=str, indent=2))


@app.get("/api/session/{sid}/export")
def export_session(sid: str) -> Response:
    """One session as a downloadable zip (artifacts + manifest + §18 panel)."""
    if not (SESSIONS_ROOT / sid).exists():
        raise HTTPException(404, f"no session {sid}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        _zip_session(z, sid)
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{sid}.zip"'})


@app.post("/api/backup")
def backup_all() -> dict:
    """Back up ALL session data to state/backups/s9-backup-<ts>.zip for
    offline processing. Returns the backup descriptor."""
    sids = list_sessions()
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)
    name = f"s9-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    path = BACKUPS_ROOT / name
    index = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for sid in sids:
            _zip_session(z, sid)
            index.append({"sid": sid, "kind": _kind(sid)})
        z.writestr("backup_index.json", json.dumps(
            {"created_at": time.time(), "sessions": index}, indent=2))
    return {"ok": True, "name": name, "sessions": len(sids),
            "size": path.stat().st_size}


@app.get("/api/backups")
def backups() -> dict:
    out = []
    if BACKUPS_ROOT.exists():
        for f in sorted(BACKUPS_ROOT.glob("*.zip"), reverse=True):
            out.append({"name": f.name, "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime})
    return {"backups": out}


@app.get("/api/backup/{name}")
def download_backup(name: str) -> FileResponse:
    f = (BACKUPS_ROOT / name).resolve()
    if not str(f).startswith(str(BACKUPS_ROOT.resolve())) or not f.is_file():
        raise HTTPException(404, f"no backup {name}")
    return FileResponse(str(f), media_type="application/zip", filename=name)


# ── launch API ────────────────────────────────────────────────────────────────
@app.post("/api/run")
def run(req: RunRequest) -> dict:
    if not req.query.strip():
        raise HTTPException(400, "empty query")
    sid = f"s8-{os.urandom(4).hex()}"
    _RUNS[sid] = {"done": False, "final": None, "error": None, "t0": time.time(), "kind": "dag"}

    def worker() -> None:
        try:
            from flow import Executor
            _RUNS[sid]["final"] = asyncio.run(Executor().run(req.query, session_id=sid))
        except Exception as e:  # noqa: BLE001
            _RUNS[sid]["error"] = f"{type(e).__name__}: {e}"
        finally:
            _RUNS[sid]["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return {"sid": sid, "kind": "dag"}


@app.post("/api/capture")
def start_capture(req: CaptureRequest) -> dict:
    if not req.url.strip():
        raise HTTPException(400, "empty url")
    sid = f"cap-{os.urandom(4).hex()}"
    _RUNS[sid] = {"done": False, "final": None, "error": None, "t0": time.time(), "kind": "capture"}
    ss = None
    if req.auth:
        ss = str(capture_engine.AUTH_ROOT / f"{req.auth}.json")

    def worker() -> None:
        try:
            man = asyncio.run(capture_engine.capture(
                req.url, goal=req.goal, steps=req.steps, want=req.want,
                session_id=sid, storage_state=ss,
                on_progress=lambda p: _RUNS[sid].__setitem__("progress", p)))
            _RUNS[sid]["final"] = {"records": man.get("records"), "turns": man.get("turns")}
            if man.get("error"):
                _RUNS[sid]["error"] = man["error"]
        except Exception as e:  # noqa: BLE001
            _RUNS[sid]["error"] = f"{type(e).__name__}: {e}"
        finally:
            _RUNS[sid]["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return {"sid": sid, "kind": "capture"}


@app.post("/api/login")
def login(req: LoginRequest) -> dict:
    if not os.environ.get("DISPLAY"):
        raise HTTPException(400, "manual login handoff needs a display (DISPLAY env). "
                                 "Run on a machine with a screen, or pre-seed state/auth/<name>.json.")
    try:
        res = asyncio.run(capture_engine.login_capture(
            req.login_url, name=req.name, wait_seconds=req.wait_seconds))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"login handoff failed: {e}")
    return {"ok": True, **res}


@app.get("/api/session/{sid}/status")
def run_status(sid: str) -> dict:
    return _run_status(sid)


@app.get("/api/session/{sid}/stream")
def stream(sid: str) -> StreamingResponse:
    """SSE: emit the §18 panel snapshot whenever it changes (plus ~1 Hz status
    events while the run is live), until the run ends."""
    def gen():
        last = None
        last_status = None
        last_ping = time.time()
        while True:
            try:
                snap = _browser_panels(sid)
                payload = json.dumps(snap, default=str, sort_keys=True)
                if payload != last:
                    yield f"data: {json.dumps({'type': 'panel', **snap}, default=str)}\n\n"
                    last = payload
                    last_ping = time.time()
            except Exception:
                pass
            try:
                st = _run_status(sid)
                if st["known"] and not st["done"]:
                    st["elapsed"] = int(st.get("elapsed") or 0)  # 1 Hz, not 2 Hz
                    spay = json.dumps(st, default=str, sort_keys=True)
                    if spay != last_status:
                        yield f"data: {json.dumps({'type': 'status', **st}, default=str)}\n\n"
                        last_status = spay
                        last_ping = time.time()
            except Exception:
                pass
            run = _RUNS.get(sid)
            if run and run["done"]:
                yield f"data: {json.dumps({'type': 'done', 'sid': sid, 'error': run.get('error'), 'final': run.get('final')}, default=str)}\n\n"
                break
            if run is None:  # external/finished session
                yield f"data: {json.dumps({'type': 'done', 'sid': sid})}\n\n"
                break
            if time.time() - last_ping >= 5:
                yield ": ping\n\n"
                last_ping = time.time()
            time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── static UI (mounted last so /api/* wins) ──────────────────────────────────
if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("S9_UI_PORT", "8200"))
    uvicorn.run(app, host="0.0.0.0", port=port)
