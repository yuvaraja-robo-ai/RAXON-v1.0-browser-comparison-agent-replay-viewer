"""Record the S9 assignment demo as two video clips (for the YouTube submission).

Clip 1 — the live browser task: the §18 HF plan (filter -> sort by likes ->
open top model) re-run with Playwright video recording on, a caption strip
showing each action as it happens.
Clip 2 — the replay viewer: walks the web UI through every §18 panel
(goal, planner DAG, path, action log, screenshots, data, table, cost).

Usage:  .venv/bin/python ../../scripts/record_demo.py   (from S9SharedCode/code)
Output: webm clips in <repo>/demo/raw/ — assembled into one mp4 by ffmpeg
(see demo/make_video.sh, written by the same session).
"""
import asyncio
import shutil
import sys
from pathlib import Path

from playwright.async_api import async_playwright

S9_ROOT = Path(__file__).resolve().parent.parent
RAW = S9_ROOT / "demo" / "raw"
UI = "http://localhost:8200"
SIZE = {"width": 1280, "height": 720}

CAPTION_JS = """(text) => {
  let el = document.getElementById('s9-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 's9-caption';
    el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:999999;' +
      'background:rgba(10,14,25,.92);color:#ffd24a;font:600 22px/1.4 system-ui;' +
      'padding:14px 24px;border-top:2px solid #ffd24a';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


async def caption(page, text):
    try:
        await page.evaluate(CAPTION_JS, text)
    except Exception:
        pass  # mid-navigation; the re-inject after the step will catch up


async def record_live_run(p):
    """Clip 1: the assignment's 5 visible browser actions on huggingface.co."""
    browser = await p.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(RAW / "live"),
                                    record_video_size=SIZE)
    page = await ctx.new_page()
    steps = [
        ("goto https://huggingface.co/models",
         lambda: page.goto("https://huggingface.co/models", wait_until="domcontentloaded")),
        ("action 1/4 — click filter: pipeline_tag = text-generation",
         lambda: page.click("a[href='/models?pipeline_tag=text-generation']")),
        ("action 2/4 — open the Sort: dropdown",
         lambda: page.click("button:has-text('Sort:')")),
        ("action 3/4 — pick 'Most likes' (sort=likes)",
         lambda: page.click("li:has-text('Most likes')")),
        ("action 4/4 — open the top model's detail page",
         lambda: page.click("article a[href^='/']")),
    ]
    await caption(page, "S9 Browser Comparison Agent — live run: "
                        "top 3 most-liked HF text-generation models")
    await asyncio.sleep(2)
    for label, act in steps:
        await caption(page, label)
        await asyncio.sleep(1.5)
        await act()
        await asyncio.sleep(2.5)
        await caption(page, label + "  ✓")
        await asyncio.sleep(1.5)
    await caption(page, "done — 5 actions, 4 page states captured; "
                        "records land in the replay viewer →")
    await asyncio.sleep(3)
    await ctx.close()
    await browser.close()


async def record_ui_walkthrough(p, dag_sid, cap_sid):
    """Clip 2: every §18 panel in the replay viewer."""
    browser = await p.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(RAW / "ui"),
                                    record_video_size=SIZE)
    page = await ctx.new_page()
    await page.goto(UI, wait_until="domcontentloaded")
    await page.wait_for_selector(".sess")
    await caption(page, "Replay viewer — sessions on the left, §18 panels on the right")
    await asyncio.sleep(3)

    await caption(page, f"② Planner DAG — LLM-planned run {dag_sid}")
    await page.click(f".sess:has-text('{dag_sid}')")
    await page.wait_for_selector(".tab:has-text('dag')")
    await page.click(".tab:has-text('dag')")
    await asyncio.sleep(4.5)

    await caption(page, f"assignment session {cap_sid} — ① goal · ③ path · "
                        "⑦ table · ⑧ turns + cost")
    await page.click(f".sess:has-text('{cap_sid}')")
    await page.wait_for_selector(".tabs")
    await asyncio.sleep(4.5)

    for tab, label in [
        ("actions", "④ Action log — 5 visible browser actions with selectors"),
        ("screens", "⑤ Screenshots — full-page state after each step"),
        ("data", "⑥ Extracted data — raw records from the sorted listing"),
        ("table", "⑦ Final comparison table — top 3 by likes"),
    ]:
        await caption(page, label)
        await page.click(f".tabs .tab:has-text('{tab}')")
        await asyncio.sleep(1)
        if tab == "screens":
            await page.mouse.wheel(0, 700)
            await asyncio.sleep(2)
            await page.mouse.wheel(0, 700)
        await asyncio.sleep(3)

    await caption(page, "S9 — deterministic capture + replay · $0 LLM cost · "
                        "orchestrator unmodified")
    await asyncio.sleep(3.5)
    await ctx.close()
    await browser.close()


async def main():
    dag_sid = sys.argv[1] if len(sys.argv) > 1 else "s8-5f92d7fb"
    cap_sid = sys.argv[2] if len(sys.argv) > 2 else "cap-ca5357dc"
    shutil.rmtree(RAW, ignore_errors=True)
    RAW.mkdir(parents=True)
    async with async_playwright() as p:
        print("recording clip 1: live HF run …")
        await record_live_run(p)
        print("recording clip 2: replay UI walkthrough …")
        await record_ui_walkthrough(p, dag_sid, cap_sid)
    for f in sorted(RAW.rglob("*.webm")):
        print("clip:", f)


if __name__ == "__main__":
    asyncio.run(main())
