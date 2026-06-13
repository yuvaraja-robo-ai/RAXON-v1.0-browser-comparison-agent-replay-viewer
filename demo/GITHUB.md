# GitHub publishing pack — RAXON

## Repo name

`RAXON-browser-comparison-agent`

## About (repo description, ≤350 chars)

Browser-capable multi-agent system: compares the top-3 most-liked Hugging Face
text-generation models with real browser actions (filter, sort, detail page),
then replays the run in a web viewer — goal, planner DAG, action log,
screenshots, extracted data, comparison table, turns + cost. Runs on a RPi5 +
Jetson Orin at $0 LLM cost.

## Topics

browser-automation · playwright · multi-agent · llm-agents · web-scraping ·
fastapi · react · ollama · raspberry-pi · jetson-orin · replay · agentic-ai

## First-release notes (v1.0 tag)

- §18 assignment complete: 5 visible browser actions on huggingface.co,
  replay viewer with all 8 panels, orchestrator (`flow.py`) unmodified.
- Deterministic capture engine (per-step full-page screenshot + HTML + records
  + action log), generic-site fallbacks, optional steps, auth handoff.
- Web observation UI on :8200 (launch + live SSE progress + §18 panels +
  offline backup/export) and CLI replay.
- Two-host split: app on RPi5, LLM (qwen3.5:4b via Ollama) on Jetson Orin
  behind the V9 gateway. 48 tests passing.
- Demo video pipeline: `scripts/record_demo.py` + `demo/make_video.sh`.

## Pre-publish checklist

- `hosts.yaml` and `state/auth/` must be gitignored (hosts.yaml holds SSH
  creds; auth holds live cookies). Ship `hosts.yaml.example` only.
- Don't commit `state/sessions/` HTML/PNG bundles or `state/backups/*.zip`
  (large; may embed logged-in page content) — keep one small sample session if
  you want the panels to render out of the box.
- `llm_gatewayV9/gateway_v8.db` contains the local cost ledger — exclude it.
- Replace `VIDEO_ID` in README's demo-video link after the YouTube upload.
