"""Replay a persisted Session 8/9 run, one node at a time.

Stdin-driven. Reads `state/sessions/<sid>/` and walks its NodeState
records in completion order. For each node prints a fixed block, then
waits for the user to advance.

Session 9 (BrowserAgent §18) extends this into the replay deliverable:
Browser nodes print their chosen cascade `path`; `a` expands the visible
browser-action log; `s` lists / opens the per-turn screenshots; and at
end-of-session the viewer prints the final comparison table plus a
turn + cost summary (the latter pulled from the V9 gateway's
`/v1/cost/by_agent` ledger when it is reachable).

Usage:
    uv run python replay.py <session_id>

Keys:
    enter   advance to next node
    p       expand the full rendered prompt that was sent to the gateway
    o       expand the full AgentResult.output JSON
    a       expand the Browser actions log (Browser nodes only)
    s       list / open the Browser screenshots (Browser nodes only)
    q       quit
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from persistence import SESSIONS_ROOT, SessionStore, list_sessions
from schemas import NodeState


# ── node block ────────────────────────────────────────────────────────────────
def _print_block(i: int, n: int, st: NodeState) -> None:
    r = st.result
    skill = st.skill
    elapsed = f"{r.elapsed_s:.1f}s" if r and r.elapsed_s else "—"
    provider = (r.provider if r and r.provider else "—")
    retries = st.retries
    tools = ""
    print()
    print(f"node {i} / {n}")
    print(f"  agent      {skill}")
    print(f"  status     {st.status}")
    print(f"  elapsed    {elapsed}")
    print(f"  provider   {provider}")
    print(f"  retries    {retries}")
    print(f"  inputs     {', '.join(st.inputs) or '(none)'}")
    # Session 9: surface the Browser cascade layer that actually ran.
    if skill == "browser" and r and r.output:
        path = r.output.get("path")
        turns = r.output.get("turns")
        if path:
            print(f"  browser path  {path}"
                  + (f"  ({turns} turns)" if turns else ""))
    if tools:
        print(f"  tools      {tools}")
    if r and r.error:
        print(f"  error      {r.error[:240]}")
    if r and r.output:
        try:
            out_preview = json.dumps(r.output, ensure_ascii=False)
        except (TypeError, ValueError):
            out_preview = str(r.output)
        if len(out_preview) > 500:
            out_preview = out_preview[:500] + "…"
        print(f"  output     {out_preview}")
    if skill == "browser" and _browser_actions(st):
        print("  (press 'a' for the action log, 's' for screenshots)")


def _expand_prompt(st: NodeState) -> None:
    print()
    print("─" * 78)
    print(st.prompt_sent or "(no prompt captured)")
    print("─" * 78)


def _expand_output(st: NodeState) -> None:
    print()
    print("─" * 78)
    if st.result and st.result.output:
        print(json.dumps(st.result.output, indent=2, ensure_ascii=False))
    else:
        print("(no output)")
    print("─" * 78)


# ── Session 9: browser action / screenshot expanders ─────────────────────────
def _browser_actions(st: NodeState) -> list[dict]:
    """The flattened per-action log the Browser skill wrote into its output."""
    if st.skill == "browser" and st.result and st.result.output:
        acts = st.result.output.get("actions")
        if isinstance(acts, list):
            return [a for a in acts if isinstance(a, dict)]
    return []


def _expand_actions(st: NodeState) -> None:
    print()
    print("─" * 78)
    actions = _browser_actions(st)
    if not actions:
        print("(no browser actions recorded on this node)")
    else:
        print(f"  {len(actions)} action(s):")
        print(f"  {'turn':>4}  {'layer':<7} {'action':<8} "
              f"{'target':<30} screenshot")
        for a in actions:
            print(f"  {str(a.get('turn', '')):>4}  "
                  f"{str(a.get('layer', '')):<7} "
                  f"{str(a.get('action', '')):<8} "
                  f"{str(a.get('target', ''))[:30]:<30} "
                  f"{a.get('screenshot') or '—'}")
    print("─" * 78)


def _screenshot_paths(st: NodeState, session_id: str) -> list[Path]:
    """Resolve the relative screenshot paths in the action log against the
    Browser skill's artifacts root (state/sessions/<sid>/browser)."""
    root = SESSIONS_ROOT / session_id / "browser"
    out: list[Path] = []
    seen: set[str] = set()
    for a in _browser_actions(st):
        rel = a.get("screenshot")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        p = Path(rel)
        out.append(p if p.is_absolute() else root / rel)
    return out


def _try_open(path: Path) -> None:
    if not path.exists():
        print(f"  (file not present on disk: {path})")
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        try:
            subprocess.Popen([opener, str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  (opened {path.name} with {opener})")
        except OSError as e:
            print(f"  (could not launch {opener}: {e})")
    else:
        print(f"  ({opener} not available — path printed above)")


def _expand_screenshots(st: NodeState, session_id: str) -> None:
    print()
    print("─" * 78)
    shots = _screenshot_paths(st, session_id)
    if not shots:
        print("(no screenshots recorded on this node)")
    else:
        for p in shots:
            mark = "" if p.exists() else "  (missing)"
            print(f"  {p}{mark}")
        _try_open(shots[0])
    print("─" * 78)


# ── Session 9: end-of-session summary (comparison table + turn/cost) ─────────
def _find_node(states: list[NodeState], skill: str) -> NodeState | None:
    """Last successful node of the given skill (recovery may re-run a skill;
    the final one carries the answer)."""
    for st in reversed(states):
        if st.skill == skill and st.result and st.result.output:
            return st
    return None


def _records_to_markdown(records: list[dict]) -> str:
    cols: list[str] = []
    for r in records:
        for k in r:
            if k not in cols:
                cols.append(k)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    rows = ["| " + " | ".join(str(r.get(c, "")) for c in cols) + " |"
            for r in records]
    return "\n".join([header, sep, *rows])


def comparison_table(states: list[NodeState]) -> str | None:
    """§18 criterion 7. Prefer the Formatter's final answer when it already
    parses as a markdown table; else rebuild one from the Distiller's
    `records`; else fall back to the Formatter's prose answer."""
    fmt = _find_node(states, "formatter")
    fmt_answer = None
    if fmt:
        fa = fmt.result.output.get("final_answer")
        if isinstance(fa, str) and fa.strip():
            fmt_answer = fa.strip()
            if "|" in fmt_answer and "---" in fmt_answer:
                return fmt_answer
    dis = _find_node(states, "distiller")
    if dis:
        recs = dis.result.output.get("records")
        if isinstance(recs, list) and recs and all(isinstance(r, dict) for r in recs):
            return _records_to_markdown(recs)
    return fmt_answer


def browser_turn_total(states: list[NodeState]) -> int:
    total = 0
    for st in states:
        if st.skill == "browser" and st.result and st.result.output:
            try:
                total += int(st.result.output.get("turns") or 0)
            except (TypeError, ValueError):
                pass
    return total


def fetch_cost(session_id: str, base_url: str | None = None) -> dict | None:
    """Pull the V9 ledger rollup for this session. Offline-tolerant: returns
    None when the gateway is unreachable so replay still works without it."""
    base = (base_url or os.environ.get("S9_GATEWAY_URL")
            or "http://localhost:8109").rstrip("/")
    try:
        r = httpx.get(f"{base}/v1/cost/by_agent",
                      params={"session": session_id}, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def summarize_cost(cost: dict) -> str:
    lines: list[str] = []
    grand = 0.0
    for agent, rows in sorted(cost.items()):
        rows = rows or []
        a_in = sum(int(r.get("in_tok") or 0) for r in rows)
        a_out = sum(int(r.get("out_tok") or 0) for r in rows)
        a_dol = sum(float(r.get("dollars") or 0.0) for r in rows)
        grand += a_dol
        lines.append(f"    {agent:<14} in={a_in:>7}  out={a_out:>7}  ${a_dol:.4f}")
    lines.append(f"    {'TOTAL':<14} {'':>10}  {'':>11}  ${grand:.4f}")
    return "\n".join(lines)


def print_summary(states: list[NodeState], session_id: str) -> None:
    print()
    print("=" * 78)
    print("COMPARISON TABLE")
    print("=" * 78)
    table = comparison_table(states)
    print(table or "(no comparison table — no formatter/distiller output found)")
    print()
    print("=" * 78)
    print("TURN + COST SUMMARY")
    print("=" * 78)
    print(f"  browser turns (total): {browser_turn_total(states)}")
    cost = fetch_cost(session_id)
    if cost is None:
        print("  cost: (gateway offline — start the V9 gateway for a cost rollup)")
    elif not cost:
        print("  cost: (no ledger rows recorded for this session)")
    else:
        print("  cost by agent:")
        print(summarize_cost(cost))


# ── driver loop ───────────────────────────────────────────────────────────────
def replay(session_id: str) -> int:
    store = SessionStore(session_id)
    states = store.read_all_nodes()
    if not states:
        print(f"replay: no nodes under state/sessions/{session_id}/", file=sys.stderr)
        return 2

    query = store.read_query() or ""
    print(f"session  {session_id}")
    print(f"query    {query[:200]}")
    print(f"nodes    {len(states)}")
    print()
    print("press enter to advance, p prompt, o output, a actions, "
          "s screenshots, q quit")

    i = 0
    while i < len(states):
        st = states[i]
        _print_block(i + 1, len(states), st)
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if cmd == "q":
            return 0
        if cmd == "p":
            _expand_prompt(st)
            continue
        if cmd == "o":
            _expand_output(st)
            continue
        if cmd == "a":
            _expand_actions(st)
            continue
        if cmd == "s":
            _expand_screenshots(st, session_id)
            continue
        i += 1
    print("\n(end of session)")
    print_summary(states, session_id)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        sessions = list_sessions()
        if not sessions:
            print("replay: no sessions under state/sessions/", file=sys.stderr)
            return 2
        print("available sessions:")
        for s in sessions:
            print(f"  {s}")
        print("\nusage: uv run python replay.py <session_id>")
        return 0
    return replay(args[0])


if __name__ == "__main__":
    sys.exit(main())
