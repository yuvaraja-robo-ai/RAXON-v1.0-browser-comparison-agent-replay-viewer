"""Deterministic test for the Session 9 Browser action-log builder.

The replay deliverable (BrowserAgent §18 criterion 4) needs ≥ 3 *visible*
browser actions surfaced per run. `browser.skill._action_log_from_steps`
flattens the drivers' per-turn StepRecords into one entry per action with a
fixed shape. We drive it with mocked StepRecords — no browser, no LLM — so
the contract is verified deterministically and joins the offline suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser.driver import StepRecord
from browser.skill import _action_log_from_steps, _turn_screenshot

_REQUIRED_KEYS = {"layer", "turn", "action", "target", "ts", "screenshot", "raw"}


def _step(turn: int, actions: list[dict], outcome: str = "ok") -> StepRecord:
    return StepRecord(turn=turn, thinking="", actions=actions, outcome=outcome,
                      provider="gemini", model="m", latency_ms=10,
                      tokens_in=1, tokens_out=1)


def test_one_entry_per_action_with_required_keys():
    steps = [
        _step(1, [{"type": "type", "mark": 3, "value": "text generation"}]),
        _step(2, [{"type": "click", "mark": 7}]),
        _step(3, [{"type": "key", "value": "Enter"}, {"type": "click", "mark": 9}]),
    ]
    log = _action_log_from_steps(steps, "a11y")

    # 1 + 1 + 2 actions = 4 flattened entries → satisfies the ≥ 3 rule.
    assert len(log) == 4
    for e in log:
        assert _REQUIRED_KEYS.issubset(e), f"missing keys in {e}"
        assert e["layer"] == "a11y"
    assert [e["action"] for e in log] == ["type", "click", "key", "click"]
    # `target` prefers a human-meaningful value, falling back to the mark.
    assert log[0]["target"] == "text generation"
    assert log[1]["target"] == "7"


def test_min_one_entry_per_turn_even_with_no_actions():
    # A parse-failure turn produced no actions but must still be visible.
    log = _action_log_from_steps([_step(1, [], outcome="error: parse")], "vision")
    assert len(log) == 1
    assert log[0]["action"] == "none"
    assert log[0]["layer"] == "vision"
    assert log[0]["screenshot"] is None
    assert _REQUIRED_KEYS.issubset(log[0])


def test_empty_steps_yield_empty_log():
    assert _action_log_from_steps([], "a11y") == []


def test_screenshot_path_relative_to_artifacts_root(tmp_path):
    adir = tmp_path / "browser_123" / "a11y"
    adir.mkdir(parents=True)
    (adir / "turn_01_raw.png").write_bytes(b"png")

    rel = _turn_screenshot(str(adir), str(tmp_path), 1)
    assert rel == "browser_123/a11y/turn_01_raw.png"
    # A turn with no screenshot on disk resolves to None, not a dangling path.
    assert _turn_screenshot(str(adir), str(tmp_path), 9) is None
    # No artifacts dir at all → None.
    assert _turn_screenshot(None, str(tmp_path), 1) is None


def test_screenshot_threaded_into_action_log(tmp_path):
    adir = tmp_path / "a11y"
    adir.mkdir(parents=True)
    (adir / "turn_01_raw.png").write_bytes(b"png")
    steps = [_step(1, [{"type": "click", "mark": 2}])]

    log = _action_log_from_steps(steps, "a11y", str(adir), str(tmp_path))
    assert log[0]["screenshot"] == "a11y/turn_01_raw.png"
