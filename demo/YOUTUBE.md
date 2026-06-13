# YouTube upload — S9 assignment demo

**File:** `RAXON_browser_agent_demo.mp4` (1:14, 720p30, 2.0 MB, H.264 +faststart)

## Suggested title

RAXON — Browser Comparison Agent + Replay Viewer (Hugging Face top-3 by likes)

## Suggested description

Browser-capable agent for the S9 assignment: compares the top 3 most-liked
Hugging Face text-generation models with real browser interaction (filter,
sort dropdown, detail page) — actions that web_search/fetch_url cannot do —
then replays the run in a web viewer with all 8 required panels.

Run: 5 visible browser actions, 4 turns, ~15 s, $0 LLM cost (deterministic
capture path). Orchestrator unmodified — everything plugs in via the Browser
skill / capture engine. Result: DeepSeek-R1 (13.4k likes), Meta-Llama-3-8B
(6.57k), Llama-3.1-8B-Instruct (6.04k).

Chapters:
0:00 Intro
0:04 Live browser run — goto, filter text-generation, open Sort menu, pick
     "Most likes", open the top model's page
0:40 Replay viewer — sessions list
0:43 ② Planner DAG (LLM-planned run)
0:48 ① Goal · ③ path · ⑦ table · ⑧ turns + cost (assignment session)
0:52 ④ Action log with selectors
0:56 ⑤ Full-page screenshots per step
1:02 ⑥ Extracted raw records
1:06 ⑦ Final comparison table

## Tags

browser agent, AI agent, playwright automation, hugging face, multi-agent
system, LLM agent, web automation, raspberry pi 5, jetson orin, ollama, local
LLM, replay viewer, school of AI

## Re-recording

cd S9SharedCode/code && .venv/bin/python ../../scripts/record_demo.py \
  [dag_sid] [cap_sid]   # defaults: s8-5f92d7fb cap-ca5357dc; UI must be on :8200
../../demo/make_video.sh
