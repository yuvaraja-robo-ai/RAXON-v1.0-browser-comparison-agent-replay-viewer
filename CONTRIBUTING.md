# Contributing to RAXON

Thanks for your interest! RAXON is a browser-capable comparison agent with a
replay viewer — contributions to site rules, capture robustness, the UI, and
docs are all welcome.

## Dev setup

```bash
git clone https://github.com/<you>/RAXON-browser-comparison-agent
cd RAXON-browser-comparison-agent

# agent + viewer (Python 3.11, uv)
cd S9SharedCode/code
uv sync
uv run playwright install chromium     # arm64: see "ARM note" below

# two-host LLM wiring (only needed for DAG runs — capture & viewer work without it)
cp ../../hosts.yaml.example ../../hosts.yaml   # fill in your Ollama host
```

Run things:

```bash
./run_agent.sh ui          # web viewer/launcher on :8200
./run_agent.sh check       # preflight the LLM host + gateway
cd S9SharedCode/code && uv run pytest tests/ -q    # 48 tests, no network LLM needed
```

**ARM note (RPi/Jetson):** Playwright ships no arm64 Linux chromium; symlink the
distro browser into the Playwright cache for BOTH `chromium-*` and
`chromium_headless_shell-*` directories (`ln -sf /usr/bin/chromium …/headless_shell`).

## Ground rules

- **`flow.py` and `schemas.py` are immutable.** New behavior plugs in through
  the skill catalogue (`skills.py`), the Browser skill (`browser/`), the
  capture engine, prompts, or the viewer. PRs that modify the orchestrator
  will be declined.
- **No agent frameworks** (LangChain, CrewAI, AutoGen, …). Stack is Playwright,
  FastAPI, httpx, trafilatura, no-build React.
- **Never commit secrets or captured state**: `hosts.yaml`, `state/` (cookies,
  page snapshots), `*.db`. The `.gitignore` enforces this — don't weaken it.
- **Tests accompany changes.** Capture/extraction changes should add a case in
  `tests/` (headless chromium against `data:` URLs keeps them offline —
  remember `charset=utf-8`).

## Good first contributions

- New **site rules** for the capture engine (a selector map + record extractor
  for a site you care about), with a generic-fallback test.
- More **step actions** (hover, drag, frame switching) in `capture_engine.py`.
- Replay viewer polish (diff view between snapshots, record filtering).
- Hardening the a11y/vision browser layers for small local models.

## PR flow

1. Fork, branch from `main` (`feat/…`, `fix/…`).
2. `uv run pytest tests/ -q` must pass; keep the suite green.
3. Open a PR using the template; one logical change per PR.
4. A maintainer reviews — expect requests to add tests or trim scope.

## Code of conduct

Be kind and constructive. We follow the
[Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
Report issues to the maintainer (see repo profile).
