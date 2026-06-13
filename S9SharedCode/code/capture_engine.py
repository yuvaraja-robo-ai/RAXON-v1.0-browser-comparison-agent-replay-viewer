"""Deterministic full-UI capture engine (S9, §18 extension).

This sits BESIDE flow.py / the Browser skill and never modifies either. It is
the robust capture path the LLM cascade cannot guarantee on a weak local
model: instead of asking the model to choose clicks, the caller gives an
explicit (optional) list of deterministic steps and we capture EVERYTHING at
each step —

    • a full-page screenshot   (browser/<sid>/step_NN_full.png)
    • the complete DOM / HTML   (browser/<sid>/step_NN.html)
    • extracted structured data (site rules + trafilatura fallback)
    • a per-action log entry shaped exactly like the Browser skill's, so the
      replay viewer and the web UI render capture bundles and DAG browser
      nodes with the same panels.

Output is a "capture bundle" written under the SAME
`state/sessions/<sid>/` tree the DAG uses, so `replay.py`, `list_sessions()`
and the web UI all see it with no special-casing.

Authenticated capture uses Playwright `storage_state` (cookies + localStorage)
— NEVER stored passwords. `login_capture()` opens a headed browser for a
manual login handoff and persists the session; later captures pass
`storage_state=` to reuse it.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import trafilatura
from playwright.async_api import Page, async_playwright

SESSIONS_ROOT = Path(__file__).resolve().parent / "state" / "sessions"
AUTH_ROOT = Path(__file__).resolve().parent / "state" / "auth"


# ── extraction rules ─────────────────────────────────────────────────────────
# Site-specific record extractors run first (precise columns); a generic
# trafilatura text-dump is the always-available fallback. Add a host here to
# teach the engine a new site's listing shape — no LLM involved.

async def _extract_huggingface_models(page: Page, limit: int = 3) -> list[dict]:
    """Top-N model cards from a huggingface.co/models listing.

    The listing renders one <article> per model with the repo id and a likes
    counter. We read repo id, likes, downloads and the card href deterministically.
    """
    js = """
    () => {
      const out = [];
      const arts = document.querySelectorAll('article');
      for (const a of arts) {
        const link = a.querySelector('a[href^="/"]');
        if (!link) continue;
        const href = link.getAttribute('href') || '';
        // repo id is the first two path segments: /<org>/<model>
        const m = href.match(/^\\/([^\\/]+\\/[^\\/?#]+)/);
        const name = m ? m[1] : (link.textContent || '').trim();
        // numbers in the card footer: likes + downloads (formatted like 1.2k)
        const nums = Array.from(a.querySelectorAll('svg ~ *, [class*=like], [class*=download]'))
                          .map(e => (e.textContent || '').trim())
                          .filter(t => /[0-9]/.test(t));
        out.push({ name, href, footer: (a.textContent || '').replace(/\\s+/g,' ').trim().slice(0, 240) });
      }
      return out;
    }
    """
    try:
        rows = await page.evaluate(js)
    except Exception:
        rows = []
    records: list[dict] = []
    seen = set()
    num_re = re.compile(r"([\d.]+)\s*([kKmM]?)")

    def _to_int(tok: str) -> int | None:
        mt = num_re.search(tok or "")
        if not mt:
            return None
        val = float(mt.group(1))
        mult = {"k": 1e3, "m": 1e6}.get(mt.group(2).lower(), 1)
        return int(val * mult)

    base = page.url
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name or "/" not in name or name in seen:
            continue
        seen.add(name)
        footer = r.get("footer", "")
        # HF card footer order: "<name> Text Generation • <params>B •
        # Updated <date> • <downloads> • <likes>". params carry a B/M suffix
        # right after the task; downloads + likes are the trailing two numbers.
        param_tok = None
        pm = re.search(r"Text Generation\s*[•·]?\s*([\d.]+\s*[BMK])", footer)
        if pm:
            param_tok = re.sub(r"\s+", "", pm.group(1))
        # numeric tokens with magnitude suffix, in document order
        toks = re.findall(r"[\d.]+\s*[kKmMbB]?", footer)
        # drop the params token (first B-suffixed) so likes/downloads are clean
        tail = [t for t in toks if not re.search(r"[bB]\s*$", t)]
        likes = _to_int(tail[-1]) if tail else None
        downloads = _to_int(tail[-2]) if len(tail) > 1 else None
        records.append({
            "name": name,
            "parameter_count": param_tok or "—",
            "likes": likes,
            "downloads": downloads,
            "url": urljoin(base, r.get("href", "")),
            "description": footer[:140],
        })
        if len(records) >= limit:
            break
    return records


_SITE_EXTRACTORS = {
    "huggingface.co": _extract_huggingface_models,
}


# Generic record extractors — make ANY site yield a comparison table, not just
# hosts with a site rule. Priority: site rule > first HTML <table> > repeated
# heading/card structures. Both run in the page, no LLM.

async def _extract_table_records(page: Page, limit: int) -> list[dict]:
    """First data <table> on the page → records keyed by its header row."""
    js = """
    () => {
      for (const t of document.querySelectorAll('table')) {
        const rows = Array.from(t.querySelectorAll('tr'))
          .map(tr => Array.from(tr.querySelectorAll('th,td'))
            .map(c => (c.textContent || '').replace(/\\s+/g, ' ').trim()));
        const body = rows.filter(r => r.some(c => c));
        if (body.length >= 2 && body[0].length >= 2) return body;
      }
      return null;
    }
    """
    rows = await page.evaluate(js)
    if not rows:
        return []
    header = [h or f"col_{i}" for i, h in enumerate(rows[0])]
    records = []
    for row in rows[1:][: max(limit, 10)]:
        records.append({header[i]: (row[i] if i < len(row) else "")
                        for i in range(len(header))})
    return records


async def _extract_card_records(page: Page, limit: int) -> list[dict]:
    """Repeated linked headings / cards → {title, url, description} records.

    Scoped to the main content area and skipping nav/footer/aside chrome so
    language switchers and menus don't pollute the table. Two card shapes:
    a link wrapping a heading (tile grids), then a heading containing a link.
    """
    js = """
    () => {
      const scope = document.querySelector('main, [role=main], #content, article')
                    || document.body;
      const chrome = e => e.closest('nav, footer, header, aside');
      const out = [], seen = new Set();
      const add = (a, titleEl) => {
        if (!a || chrome(a)) return;
        const href = a.getAttribute('href') || '';
        const title = ((titleEl || a).textContent || '').replace(/[\\s¶#]+$/g, '').trim();
        if (title.length < 4 || !href || href.startsWith('#') || seen.has(href)) return;
        seen.add(href);
        const sect = a.closest('article, li, section');
        const desc = sect ? (sect.textContent || '').replace(/\\s+/g, ' ').trim() : '';
        out.push({ title, url: href, description: desc.slice(0, 140) });
      };
      for (const a of scope.querySelectorAll('a[href]')) {       // link wraps heading
        const h = a.querySelector('h1,h2,h3,h4,strong,b');
        if (h) add(a, h);
        if (out.length >= 12) return out;
      }
      for (const a of scope.querySelectorAll('h2 a[href], h3 a[href], li h4 a[href]')) {
        add(a);                                                  // heading holds link
        if (out.length >= 12) return out;
      }
      return out;
    }
    """
    rows = await page.evaluate(js) or []
    base = page.url
    return [{**r, "url": urljoin(base, r.get("url", ""))}
            for r in rows[: max(limit, 10)]]


async def _extract_section_records(page: Page, limit: int) -> list[dict]:
    """Content sections (h2/h3 + first paragraph) → records. The last-resort
    shape that works on documentation/article pages with no tables or cards."""
    js = """
    () => {
      const scope = document.querySelector('main, [role=main], #content, article')
                    || document.body;
      const out = [];
      for (const h of scope.querySelectorAll('h2, h3')) {
        if (h.closest('nav, footer, header, aside')) continue;
        const clone = h.cloneNode(true);
        clone.querySelectorAll('a').forEach(a => {       // strip ¶/# permalinks
          if (/^(#|¶|link to this section|permalink)/i.test((a.textContent || '').trim()))
            a.remove();
        });
        const title = (clone.textContent || '')
          .replace(/[\\s¶#]+$/g, '').replace(/^[\\s¶#]+/, '').trim();
        if (title.length < 4) continue;
        let el = h.nextElementSibling, desc = '';
        while (el && !desc && !/^H[1-6]$/.test(el.tagName)) {
          desc = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          el = el.nextElementSibling;
        }
        const id = h.id || (h.querySelector('[id]') || {}).id || '';
        out.push({ title, description: desc.slice(0, 140),
                   url: id ? location.origin + location.pathname + '#' + id : location.href });
        if (out.length >= 12) break;
      }
      return out;
    }
    """
    rows = await page.evaluate(js) or []
    return rows[: max(limit, 10)]


def _generic_extract(html: str) -> dict:
    """Always-available fallback: trafilatura main text + page title."""
    text = trafilatura.extract(html, include_comments=False,
                               include_tables=True, favor_recall=True) or ""
    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if mt:
        title = re.sub(r"\s+", " ", mt.group(1)).strip()
    return {"title": title, "text": text[:4000]}


async def _extract(page: Page, html: str, *, want: int = 3) -> dict:
    host = re.sub(r"^www\.", "", (page.url.split("/")[2] if "//" in page.url else ""))
    out: dict[str, Any] = {"generic": _generic_extract(html)}
    fn = _SITE_EXTRACTORS.get(host)
    if fn is not None:
        try:
            out["records"] = await fn(page, want)
        except Exception as e:  # noqa: BLE001
            out["records_error"] = f"{type(e).__name__}: {e}"
    if not out.get("records"):
        # generic fallbacks so unknown sites still get a comparison table
        for extractor, source in ((_extract_table_records, "table"),
                                  (_extract_card_records, "cards"),
                                  (_extract_section_records, "sections")):
            try:
                recs = await extractor(page, want)
            except Exception:
                recs = []
            if recs:
                out["records"] = recs
                out["records_source"] = source
                break
    return out


# ── one step = perform an action then snapshot everything ────────────────────

def _describe(step: dict) -> str:
    """Human-readable label for one capture step (for live progress)."""
    act = (step.get("action") or "goto").lower()
    tgt = step.get("selector") or step.get("value") or step.get("url") or ""
    verb = {"goto": "opening", "click": "clicking", "type": "typing into",
            "select": "selecting in", "press": "pressing", "scroll": "scrolling",
            "wait": "waiting"}.get(act, act)
    return f"{verb} {tgt}".strip()


async def _perform(page: Page, step: dict) -> dict:
    """Run one deterministic action. Returns the resolved action-log fields.

    Per-step `"timeout"` (seconds, default 15) bounds selector waits, so
    `"optional": true` steps on selectors a site doesn't have fail fast.
    """
    act = (step.get("action") or "goto").lower()
    sel = step.get("selector")
    val = step.get("value")
    target = sel or val or step.get("url") or ""
    t_ms = int(float(step.get("timeout", 15)) * 1000)
    if act == "goto":
        await page.goto(step["url"], wait_until="domcontentloaded", timeout=45000)
    elif act == "click":
        await page.click(sel, timeout=t_ms)
    elif act == "type":
        await page.fill(sel, val or "", timeout=t_ms)
    elif act == "select":
        await page.select_option(sel, val, timeout=t_ms)
    elif act == "press":
        await page.keyboard.press(val or "Enter")
    elif act == "scroll":
        await page.mouse.wheel(0, int(step.get("amount", 1200)))
    elif act == "wait":
        await page.wait_for_timeout(int(float(step.get("seconds", 1)) * 1000))
    else:
        raise ValueError(f"unknown capture action {act!r}")
    # let the page settle (network + render) before snapshotting
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return {"action": act, "target": str(target)}


async def _snapshot(page: Page, art_dir: Path, turn: int, *, want: int) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    png = art_dir / f"step_{turn:02d}_full.png"
    htmlf = art_dir / f"step_{turn:02d}.html"
    await page.screenshot(path=str(png), full_page=True)
    html = await page.content()
    htmlf.write_text(html, encoding="utf-8")
    data = await _extract(page, html, want=want)
    return {
        "turn": turn,
        "url": page.url,
        "title": await page.title(),
        "screenshot": f"step_{turn:02d}_full.png",
        "html": f"step_{turn:02d}.html",
        "data": data,
    }


# ── public API ───────────────────────────────────────────────────────────────

async def capture(
    url: str,
    *,
    goal: str = "",
    steps: list[dict] | None = None,
    want: int = 3,
    session_id: str | None = None,
    storage_state: str | None = None,
    headed: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Deterministically capture a page (and optional follow-up steps).

    Returns the bundle manifest (also persisted to disk). `steps` is an
    ordered list of {action, selector?, value?, url?} dicts performed after the
    initial goto; each step (and the initial load) produces one full snapshot.
    `storage_state` is a path to a Playwright storage-state JSON for
    authenticated capture (see `login_capture`).

    `on_progress` (optional) is called with
    {stage, message, step?, total?, ts} as the capture advances, so a host
    (e.g. the web UI) can show live status. Exceptions in the callback are
    swallowed — progress reporting must never break a capture.
    """

    def _say(stage: str, message: str, *, step: int | None = None,
             total: int | None = None) -> None:
        if on_progress is None:
            return
        evt: dict[str, Any] = {"stage": stage, "message": message, "ts": time.time()}
        if step is not None:
            evt["step"] = step
        if total is not None:
            evt["total"] = total
        try:
            on_progress(evt)
        except Exception:
            pass

    sid = session_id or f"cap-{int(time.time())%100000:05d}-{abs(hash(url))%1000:03d}"
    sdir = SESSIONS_ROOT / sid
    art_dir = sdir / "browser"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "query.txt").write_text(goal or f"Capture {url}", encoding="utf-8")

    plan: list[dict] = [{"action": "goto", "url": url}] + list(steps or [])
    snapshots: list[dict] = []
    actions: list[dict] = []
    t0 = time.time()
    error: str | None = None

    _say("launch", "launching browser…", step=0, total=len(plan))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        ctx = await browser.new_context(
            storage_state=storage_state if storage_state and Path(storage_state).exists() else None,
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()
        try:
            for turn, step in enumerate(plan):
                _say("step", _describe(step), step=turn, total=len(plan))
                try:
                    resolved = await _perform(page, step)
                except Exception as e:  # noqa: BLE001
                    # "optional": true → site doesn't have this control; log
                    # the miss and keep going (generic plans across sites).
                    if step.get("optional"):
                        actions.append({
                            "layer": "capture", "turn": turn,
                            "action": (step.get("action") or "goto").lower(),
                            "target": step.get("selector") or step.get("url") or "",
                            "ts": time.time(), "screenshot": None, "raw": step,
                            "status": "skipped",
                        })
                        _say("step", f"skipped (not on this page): {_describe(step)}",
                             step=turn, total=len(plan))
                        continue
                    raise
                # steps with "snapshot": false skip the capture — needed for
                # transient UI (e.g. an open dropdown menu) that a full-page
                # screenshot would dismiss before the NEXT step can click it.
                if step.get("snapshot") is False:
                    actions.append({
                        "layer": "capture", "turn": turn,
                        "action": resolved["action"], "target": resolved["target"],
                        "ts": time.time(), "screenshot": None, "raw": step,
                        "status": "ok",
                    })
                    continue
                _say("snapshot", f"capturing page state ({turn + 1}/{len(plan)})…",
                     step=turn, total=len(plan))
                snap = await _snapshot(page, art_dir, len(snapshots), want=want)
                snapshots.append(snap)
                actions.append({
                    "layer": "capture", "turn": turn,
                    "action": resolved["action"], "target": resolved["target"],
                    "ts": time.time(), "screenshot": f"browser/{snap['screenshot']}",
                    "raw": step,
                    "status": "ok",
                })
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            # best-effort snapshot of where the page was when the step failed,
            # so the bundle shows the state that broke instead of nothing
            try:
                snap = await _snapshot(page, art_dir, len(snapshots), want=want)
                snap["note"] = "state at failure"
                snapshots.append(snap)
            except Exception:
                pass
        finally:
            await ctx.close()
            await browser.close()

    # records = best snapshot's site-specific records (the goal of the capture).
    # Listing pages yield complete rows (likes populated); detail/other pages
    # may yield partial junk — score by populated likes, ties go to the later
    # snapshot so a sorted listing late in the plan wins.
    records: list[dict] = []
    best = -1
    for snap in snapshots:
        recs = snap.get("data", {}).get("records") or []
        score = sum(1 for r in recs if r.get("likes") is not None)
        if recs and score >= best:
            best, records = score, recs
    _say("done", "capture complete", step=len(plan), total=len(plan))

    manifest = {
        "kind": "capture",
        "session_id": sid,
        "url": url,
        "goal": goal,
        "steps": plan,
        "turns": len(snapshots),
        "snapshots": snapshots,
        "records": records,
        "actions": actions,
        "authenticated": bool(storage_state),
        "elapsed_s": round(time.time() - t0, 2),
        "error": error,
        "created_at": time.time(),
    }
    (sdir / "capture.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


async def login_capture(login_url: str, *, name: str, wait_seconds: int = 180) -> dict:
    """Manual login handoff: open a HEADED browser at `login_url`, let the user
    log in by hand, then persist the authenticated session (cookies +
    localStorage) to state/auth/<name>.json for reuse by `capture(...,
    storage_state=...)`. No passwords are stored — only the resulting session.

    Requires a display (DISPLAY env). Returns {storage_state, name}.
    """
    AUTH_ROOT.mkdir(parents=True, exist_ok=True)
    out = AUTH_ROOT / f"{name}.json"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded")
        # Give the human time to complete the login flow in the visible window.
        await page.wait_for_timeout(wait_seconds * 1000)
        await ctx.storage_state(path=str(out))
        await ctx.close()
        await browser.close()
    return {"name": name, "storage_state": str(out)}


def list_auth_sessions() -> list[str]:
    if not AUTH_ROOT.exists():
        return []
    return sorted(p.stem for p in AUTH_ROOT.glob("*.json"))


if __name__ == "__main__":  # tiny CLI for manual smoke tests
    import argparse
    ap = argparse.ArgumentParser(description="deterministic UI capture")
    ap.add_argument("url")
    ap.add_argument("--goal", default="")
    ap.add_argument("--want", type=int, default=3)
    ap.add_argument("--auth", default=None, help="storage_state name under state/auth/")
    args = ap.parse_args()
    ss = str(AUTH_ROOT / f"{args.auth}.json") if args.auth else None
    m = asyncio.run(capture(args.url, goal=args.goal, want=args.want, storage_state=ss))
    print(json.dumps({k: v for k, v in m.items() if k != "snapshots"}, indent=2))
    print(f"\nbundle: state/sessions/{m['session_id']}/")
