"""Deterministic tests for the Session 9 replay end-of-session summary.

BrowserAgent §18 criteria 7 (comparison table) and 8 (turn + cost) are
rendered by pure functions in `replay.py`. We feed them synthetic NodeStates
— no live web, no gateway required (the cost path is offline-tolerant) — and
assert the table and the turn/cost block render.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay
from schemas import AgentResult, NodeState


def _node(skill: str, output: dict, node_id: str = "n:1") -> NodeState:
    return NodeState(
        node_id=node_id, skill=skill, status="complete",
        result=AgentResult(success=True, agent_name=skill, output=output),
    )


def test_comparison_table_rebuilt_from_distiller_records():
    states = [
        _node("planner", {"nodes": []}, "n:1"),
        _node("browser", {"path": "a11y", "turns": 4, "actions": []}, "n:2"),
        _node("distiller", {"records": [
            {"name": "A", "likes": "100"},
            {"name": "B", "likes": "90"},
        ]}, "n:3"),
        _node("formatter", {"final_answer": "some prose answer"}, "n:4"),
    ]
    table = replay.comparison_table(states)
    assert "| name | likes |" in table
    assert "| --- | --- |" in table
    assert "| A | 100 |" in table
    assert "| B | 90 |" in table


def test_comparison_table_prefers_formatter_markdown():
    md = "| name | likes |\n| --- | --- |\n| Z | 5 |"
    states = [
        _node("distiller", {"records": [{"name": "ignored"}]}, "n:1"),
        _node("formatter", {"final_answer": md}, "n:2"),
    ]
    assert replay.comparison_table(states) == md


def test_comparison_table_none_when_no_structured_output():
    states = [_node("browser", {"path": "a11y", "turns": 1}, "n:1")]
    assert replay.comparison_table(states) is None


def test_browser_turn_total_sums_across_browser_nodes():
    states = [
        _node("browser", {"turns": 4, "path": "a11y"}, "n:1"),
        _node("formatter", {"final_answer": "x"}, "n:2"),
        _node("browser", {"turns": 2, "path": "vision"}, "n:3"),
    ]
    assert replay.browser_turn_total(states) == 6


def test_summarize_cost_renders_per_agent_and_total():
    cost = {
        "browser": [{"provider": "gemini", "in_tok": 100, "out_tok": 50, "dollars": 0.0}],
        "planner": [{"provider": "github", "in_tok": 200, "out_tok": 80, "dollars": 0.0012}],
    }
    out = replay.summarize_cost(cost)
    assert "browser" in out
    assert "planner" in out
    assert "TOTAL" in out
    assert "$0.0012" in out


def test_fetch_cost_offline_returns_none():
    # Unreachable gateway must degrade to None, never raise.
    assert replay.fetch_cost("no-sess", base_url="http://127.0.0.1:1") is None


def test_print_summary_smoke(capsys, monkeypatch):
    monkeypatch.setattr(replay, "fetch_cost", lambda *a, **k: None)
    states = [
        _node("browser", {"turns": 3, "path": "a11y"}, "n:1"),
        _node("distiller", {"records": [{"name": "A", "likes": "9"}]}, "n:2"),
        _node("formatter", {"final_answer": "prose"}, "n:3"),
    ]
    replay.print_summary(states, "sess-x")
    out = capsys.readouterr().out
    assert "COMPARISON TABLE" in out
    assert "| name | likes |" in out
    assert "TURN + COST SUMMARY" in out
    assert "browser turns (total): 3" in out
    assert "gateway offline" in out
