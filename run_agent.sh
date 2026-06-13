#!/usr/bin/env bash
# run_agent.sh — boot + drive the S9 Browser DAG agent (two-host split).
#
# ARCHITECTURE RULE (carried from the S7/S8 working setup):
#   • Jetson Orin Nano : Ollama inference ONLY (chat qwen3.5:4b + embeddings).
#   • RPi5 (this host) : the app ONLY — DAG agent (flow.py) + V9 gateway (:8109)
#                        that FORWARDS chat/embeds to the Orin. NO local model.
#
# hosts.yaml is the single source of truth; scripts/hostsenv.py --role rpi5
# sets OLLAMA_URL -> the Orin and the Orin SSH creds. This launcher NEVER runs
# `ollama serve` on the RPi5; if the Orin's Ollama is down it remote-starts it
# over SSH (start_orin_ollama).
#
# Usage:
#   ./run_agent.sh check                 preflight Orin Ollama + gateway
#   ./run_agent.sh orin-start            SSH-start Ollama on the Orin (idempotent)
#   ./run_agent.sh gateway               start the V9 gateway only (:8109)
#   ./run_agent.sh query "your question" run one agent turn from the CLI
#   ./run_agent.sh replay <session_id>   replay a persisted run
#   ./run_agent.sh stop                  stop the gateway started by this script
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$ROOT/llm_gatewayV9"
CODE="$ROOT/S9SharedCode/code"
LOGS="$ROOT/.run_logs"
mkdir -p "$LOGS"

# Two-host wiring: derive OLLAMA_URL / OLLAMA_MODEL / EMBED_OLLAMA_MODEL /
# LLM_ORDER / GATEWAY_V9_PORT / S9_BROWSER_A11Y_PROVIDER / ORIN_SSH_* from
# hosts.yaml (role rpi5).
if [ -f "$ROOT/hosts.yaml" ]; then
  eval "$(python3 "$ROOT/scripts/hostsenv.py" --role rpi5)"
fi

GATEWAY_PORT="${GATEWAY_V9_PORT:-8109}"
OLLAMA_URL="${OLLAMA_URL:-}"     # MUST point at the Orin; empty => misconfigured
export GATEWAY_V9_PORT="$GATEWAY_PORT"

c_red()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel()  { printf '\033[33m%s\033[0m\n' "$*"; }
info()   { printf '\033[36m[run]\033[0m %s\n' "$*"; }

http_code() { curl -s -o /dev/null -w '%{http_code}' -m 4 "$1" 2>/dev/null || echo 000; }
probe_ollama()  { [ "$(http_code "$OLLAMA_URL/api/tags")" = "200" ]; }
ollama_has_embed() { curl -s -m 4 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -q "${EMBED_OLLAMA_MODEL%%:*}"; }
gateway_up()    { [ "$(http_code "http://127.0.0.1:$GATEWAY_PORT/v1/cost/by_agent")" = "200" ]; }
ui_up()         { [ "$(http_code "http://127.0.0.1:${S9_UI_PORT:-8200}/api/health")" = "200" ]; }

_guard_remote_model() {
  # Refuse to proceed if OLLAMA_URL points at this host — the model must run on
  # the Orin, never on the RPi5.
  case "$OLLAMA_URL" in
    ""|*127.0.0.1*|*localhost*)
      c_red "OLLAMA_URL='$OLLAMA_URL' points at the local host."
      c_red "The model must run on the Orin. Fix orin.host in hosts.yaml."
      return 1 ;;
  esac
  return 0
}

# ── remote-start Ollama on the Orin (never local) ────────────────────────────
orin_ssh() {
  local host; host="$(echo "$OLLAMA_URL" | sed 's|http://||;s|:.*||')"
  local user="${ORIN_SSH_USER:-ds}" pass="${ORIN_SSH_PASS:-}"
  [ -z "$pass" ] && { c_yel "  • ORIN_SSH_PASS not set — cannot remote-start"; return 1; }
  command -v sshpass >/dev/null 2>&1 || { c_yel "  • sshpass not installed (apt install sshpass)"; return 1; }
  sshpass -p "$pass" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 "${user}@${host}" "$@"
}

start_orin_ollama() {
  _guard_remote_model || return 1
  if probe_ollama; then info "Orin Ollama already up at $OLLAMA_URL"; return 0; fi
  local host; host="$(echo "$OLLAMA_URL" | sed 's|http://||;s|:.*||')"
  info "Ollama not reachable at $OLLAMA_URL — remote-starting on $host via SSH"
  local chat="${OLLAMA_MODEL:-qwen3.5:4b-q4_K_M}" embed="${EMBED_OLLAMA_MODEL:-nomic-embed-text}"
  orin_ssh bash -s <<EOF || { c_red "  ✗ could not start Ollama on $host"; return 1; }
set -e
if ! pgrep -x ollama >/dev/null 2>&1; then
  OLLAMA_HOST=0.0.0.0:11434 nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  for i in \$(seq 1 20); do curl -fsS --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 0.5; done
fi
ollama list 2>/dev/null | grep -qiF "$chat"  || ollama pull "$chat"
ollama list 2>/dev/null | grep -qiF "$embed" || ollama pull "$embed"
echo "[orin] ollama ready"
EOF
  for _ in $(seq 1 10); do probe_ollama && break; sleep 1; done
  probe_ollama && { c_grn "  ✓ Orin Ollama reachable at $OLLAMA_URL"; return 0; } \
               || { c_red "  ✗ started on Orin but still unreachable at $OLLAMA_URL"; return 1; }
}

# ── gateway (on the RPi5; forwards to the Orin) ──────────────────────────────
start_gateway() {
  if gateway_up; then info "gateway already up on :$GATEWAY_PORT"; return 0; fi
  info "starting V9 gateway on :$GATEWAY_PORT (log: $LOGS/gateway.log)"
  ( cd "$GATEWAY_DIR" && exec uv run python main.py ) >"$LOGS/gateway.log" 2>&1 &
  echo $! > "$LOGS/gateway.pid"
  for _ in $(seq 1 45); do gateway_up && { c_grn "  ✓ gateway up on :$GATEWAY_PORT"; return 0; }; sleep 1; done
  c_red "  ✗ gateway failed to start in 45s — see $LOGS/gateway.log"; return 1
}

cmd_check() {
  local ok=0
  info "preflight (model host=$OLLAMA_URL  gateway=:$GATEWAY_PORT)"
  _guard_remote_model || ok=1
  if probe_ollama; then
    c_grn "  ✓ Orin Ollama reachable at $OLLAMA_URL"
    if ollama_has_embed; then c_grn "  ✓ embed model ${EMBED_OLLAMA_MODEL} present on Orin"
    else ok=1; c_red "  ✗ embed model ${EMBED_OLLAMA_MODEL} missing on Orin (dense RAG will 503)"
         c_yel "    fix on Orin: ollama pull ${EMBED_OLLAMA_MODEL}"; fi
  else
    ok=1; c_red "  ✗ Orin Ollama NOT reachable at $OLLAMA_URL"
    c_yel "    power on the Orin (see orin.host in hosts.yaml) and connect it to wifi, then:"
    c_yel "    ./run_agent.sh orin-start"
  fi
  gateway_up && c_grn "  ✓ gateway up on :$GATEWAY_PORT" || c_yel "  • gateway not running (auto-starts on demand)"
  [ "$ok" = 0 ] && c_grn "preflight OK" || c_red "preflight has blockers (see above)"
  return "$ok"
}

cmd_query() {
  local q="${1:-}"
  [ -z "$q" ] && { c_red "usage: $0 query \"your question\""; exit 2; }
  start_orin_ollama || c_yel "continuing; the Orin model may be unavailable…"
  start_gateway || exit 1
  info "agent: $q"
  ( cd "$CODE" && exec uv run python flow.py "$q" )
}

cmd_replay() {
  local sid="${1:-}"
  ( cd "$CODE" && exec uv run python replay.py "$sid" )
}

cmd_ui() {
  # Web observation UI (FastAPI + static). Viewer works offline; DAG launches
  # need the Orin, deterministic captures need neither model nor gateway. We
  # best-effort bring the gateway up first so the cost panels + DAG launch work
  # out of the box; the UI still serves if it is down.
  local port="${S9_UI_PORT:-8200}"
  export S9_UI_PORT="$port"
  if ui_up; then
    c_grn "  ✓ UI already up on :$port — open http://0.0.0.0:$port (use '$0 stop' to restart)"
    return 0
  fi
  start_gateway || c_yel "  • gateway unavailable — viewer/capture still work; DAG launch + cost won't"
  info "S9 UI on http://0.0.0.0:$port  (sessions: $CODE/state/sessions)"
  ( cd "$CODE" && exec uv run python api_server.py )
}

cmd_up() {
  # Bring up EVERYTHING for a full setup check: model on the Orin, the gateway
  # on this box, a preflight, then the web UI (foreground). The gateway keeps
  # running in the background after you Ctrl-C the UI; use `$0 stop` to stop it.
  c_yel "── bringing up the S9 stack ─────────────────────────────────────────"
  start_orin_ollama || c_yel "continuing; the Orin model may be unavailable…"
  start_gateway      || c_yel "continuing; the gateway failed to start…"
  echo
  cmd_check || true
  echo
  c_grn "── launching the web UI (Ctrl-C stops the UI; gateway stays up) ──────"
  cmd_ui
}

cmd_stop() {
  local pf="$LOGS/gateway.pid"
  if [ -f "$pf" ]; then
    local pid; pid="$(cat "$pf")"
    kill "$pid" 2>/dev/null && c_grn "  ✓ stopped gateway (pid $pid)" || c_yel "  • gateway not running"
    rm -f "$pf"
  fi
  # stale gateway + UI processes started outside the pidfile (e.g. background runs)
  while read -r pid _; do [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null && c_grn "  ✓ stopped stale gateway $pid"; done \
    < <(pgrep -af "uv run python main.py" 2>/dev/null || true)
  while read -r pid _; do [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null && c_grn "  ✓ stopped UI $pid"; done \
    < <(pgrep -af "api_server.py" 2>/dev/null || true)
}

case "${1:-}" in
  up)         cmd_up ;;
  check)      cmd_check ;;
  orin-start) start_orin_ollama ;;
  gateway)    start_gateway ;;
  query)      shift; cmd_query "${*:-}" ;;
  replay)     shift; cmd_replay "${1:-}" ;;
  ui)         cmd_ui ;;
  stop)       cmd_stop ;;
  *) cat <<EOF
S9 Browser DAG agent runner (two-host: model on Orin, app on RPi5)

  $0 up                    START ALL APPS: Orin Ollama + gateway + preflight + UI
  $0 check                 preflight Orin Ollama (chat+embed) + gateway (no start)
  $0 orin-start            SSH-start Ollama on the Orin (idempotent)
  $0 gateway               start the V9 gateway (:$GATEWAY_PORT)
  $0 query "question"      run one agent turn from the CLI
  $0 replay <session_id>   step through a persisted run (a=actions, s=screens)
  $0 ui                    web observation UI (:${S9_UI_PORT:-8200}) — view + launch + capture
  $0 stop                  stop the gateway started by this script

model host: Orin=${OLLAMA_URL:-<unset>}   gateway=:${GATEWAY_PORT}
EOF
     ;;
esac
