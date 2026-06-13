"""Generic-site behavior of the deterministic capture engine.

The engine must produce useful bundles on sites WITHOUT a site rule in
_SITE_EXTRACTORS: records fall back to the first HTML <table> (then linked
headings/cards), and "optional": true steps whose selector a site doesn't
have are skipped instead of failing the whole capture. Runs headless
chromium against a data: URL — no network, no gateway.
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture_engine

PAGE = """
<html><head><title>CNC institutes</title></head><body>
  <h1>Compare CNC training institutes</h1>
  <table>
    <tr><th>institute</th><th>city</th><th>fees</th></tr>
    <tr><td>Alpha CNC Academy</td><td>Bangalore</td><td>45000</td></tr>
    <tr><td>Beta VMC School</td><td>Bangalore</td><td>52000</td></tr>
    <tr><td>Gamma Tooling Institute</td><td>Mysore</td><td>38000</td></tr>
  </table>
</body></html>
"""
DATA_URL = "data:text/html;charset=utf-8," + urllib.parse.quote(PAGE)


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_engine, "SESSIONS_ROOT", tmp_path / "sessions")
    return tmp_path / "sessions"


async def test_generic_table_records_and_optional_skip(sessions_root):
    man = await capture_engine.capture(
        DATA_URL,
        goal="generic site capture",
        steps=[
            # selector that exists nowhere: optional + fast timeout → skipped
            {"action": "click", "selector": "#no-such-button",
             "optional": True, "timeout": 1},
        ],
        session_id="cap-generic-test",
    )
    assert man["error"] is None
    # table fallback produced records keyed by the header row
    recs = man["records"]
    assert [r["institute"] for r in recs] == [
        "Alpha CNC Academy", "Beta VMC School", "Gamma Tooling Institute"]
    assert recs[0]["fees"] == "45000"
    assert man["snapshots"][0]["data"]["records_source"] == "table"
    # the optional miss is logged as skipped, not fatal
    statuses = [a["status"] for a in man["actions"]]
    assert statuses == ["ok", "skipped"]
    assert (sessions_root / "cap-generic-test" / "capture.json").exists()


DOCS_PAGE = """
<html><head><title>Docs</title></head><body><main>
  <h1>Product docs</h1>
  <h2 id="install"><a href="#install">¶</a>Installation</h2>
  <p>Install with pip in one line.</p>
  <h2 id="usage"><a href="#usage">Link to this section</a>Quick usage</h2>
  <p>Run the CLI against your data.</p>
</main></body></html>
"""


async def test_generic_section_records(sessions_root):
    url = "data:text/html;charset=utf-8," + urllib.parse.quote(DOCS_PAGE)
    man = await capture_engine.capture(url, session_id="cap-sections-test")
    assert man["error"] is None
    recs = man["records"]
    assert [r["title"] for r in recs] == ["Installation", "Quick usage"]
    assert recs[0]["description"] == "Install with pip in one line."
    assert man["snapshots"][0]["data"]["records_source"] == "sections"


async def test_failed_step_still_snapshots(sessions_root):
    man = await capture_engine.capture(
        DATA_URL,
        steps=[{"action": "click", "selector": "#missing", "timeout": 1}],
        session_id="cap-fail-test",
    )
    assert man["error"] and "TimeoutError" in man["error"]
    # best-effort snapshot of the state at failure rides along
    assert man["snapshots"][-1].get("note") == "state at failure"
    assert man["records"]  # initial-load table records survive the failure
