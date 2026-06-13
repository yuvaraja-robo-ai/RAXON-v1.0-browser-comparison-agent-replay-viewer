#!/usr/bin/env python3
"""Translate hosts.yaml into shell `export VAR=value` lines for run_agent.sh.

Single source of truth for the two-host split (ported from S8):
  Orin = Ollama inference ONLY (chat + embeddings).
  RPi5 = the app ONLY (this DAG agent + V9 gateway that FORWARDS to the Orin).

The RPi5 never runs a model — it only sets OLLAMA_URL to point the gateway at
the Orin, plus the Orin SSH creds so run_agent.sh can remote-start Ollama on
the Orin if it is down. It also sets S9_BROWSER_A11Y_PROVIDER=ollama so the
Browser skill's a11y driver targets the Orin model instead of its cloud
default (gemini).

Usage:
    eval "$(python3 scripts/hostsenv.py --role rpi5)"   # on the RPi5
    eval "$(python3 scripts/hostsenv.py --role orin)"   # on the Orin
    python3 scripts/hostsenv.py --check                 # resolve orin host, 0/1

Role resolution order: --role flag > $S9_ROLE > hosts.yaml `role:` field.
"""
from __future__ import annotations

import argparse
import os
import shlex
import socket
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HOSTS_YAML = REPO_ROOT / "hosts.yaml"


def _die(msg: str, code: int = 2) -> None:
    print(f"hostsenv: {msg}", file=sys.stderr)
    sys.exit(code)


def _load() -> dict | None:
    if not HOSTS_YAML.is_file():
        return None
    data = yaml.safe_load(HOSTS_YAML.read_text()) or {}
    if not isinstance(data, dict):
        _die(f"{HOSTS_YAML} is not a mapping")
    return data


def _resolve_role(cfg: dict, flag: str | None) -> str:
    role = flag or os.getenv("S9_ROLE") or cfg.get("role")
    if role not in ("orin", "rpi5"):
        _die(f"role must be 'orin' or 'rpi5' (got {role!r}); "
             "set --role, $S9_ROLE, or role: in hosts.yaml")
    return role


def _orin_block(cfg: dict) -> dict:
    orin = cfg.get("orin") or {}
    if not orin.get("host"):
        _die("hosts.yaml: orin.host is required (a LAN IP)")
    return orin


def _emit(pairs: list[tuple[str, str]]) -> None:
    for k, v in pairs:
        print(f"export {k}={shlex.quote(str(v))}")


def _rpi5_env(cfg: dict) -> list[tuple[str, str]]:
    orin = _orin_block(cfg)
    rpi = cfg.get("rpi5") or {}
    port = orin.get("ollama_port", 11434)
    url = f"http://{orin['host']}:{port}"
    pairs = [
        ("OLLAMA_URL", url),
        ("OLLAMA_MODEL", orin.get("chat_model", "qwen3.5:4b-q4_K_M")),
        ("EMBED_OLLAMA_MODEL", orin.get("embed_model", "nomic-embed-text")),
        ("GATEWAY_V9_PORT", rpi.get("gateway_port", 8109)),
        # All models live on the Orin; the RPi5 gateway forwards to it.
        ("LLM_ORDER", "ollama"),
        # Point the Browser skill's a11y driver at the Orin model (its code
        # default is the cloud `gemini` pin, which has no key in a local run).
        ("S9_BROWSER_A11Y_PROVIDER", "ollama"),
        # Same story for the memory classifier (hard-pins gemini by default —
        # 400s locally and pollutes memory with junk query "facts").
        ("S9_MEMORY_PROVIDER", "ollama"),
    ]
    if orin.get("user"):
        pairs.append(("ORIN_SSH_USER", orin["user"]))
    if orin.get("pass") is not None:
        pairs.append(("ORIN_SSH_PASS", str(orin["pass"])))
    return pairs


def _orin_env(cfg: dict) -> list[tuple[str, str]]:
    orin = _orin_block(cfg)
    port = orin.get("ollama_port", 11434)
    return [
        ("OLLAMA_HOST", f"0.0.0.0:{port}"),
        ("OLLAMA_CHAT_MODEL", orin.get("chat_model", "qwen3.5:4b-q4_K_M")),
        ("OLLAMA_EMBED_MODEL", orin.get("embed_model", "nomic-embed-text")),
    ]


def _check(cfg: dict) -> None:
    orin = _orin_block(cfg)
    host = orin["host"]
    try:
        addr = socket.gethostbyname(host)
    except OSError as e:
        _die(f"cannot resolve orin host {host!r} ({e}). Set a static LAN IP "
             "in hosts.yaml.", code=1)
    print(f"hostsenv: {host} -> {addr}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="hosts.yaml -> shell env")
    ap.add_argument("--role", choices=["orin", "rpi5"], default=None)
    ap.add_argument("--check", action="store_true",
                    help="resolve the orin host and exit 0/1 (no env emitted)")
    args = ap.parse_args()

    cfg = _load()
    if cfg is None:
        if args.check:
            _die("hosts.yaml not found", code=1)
        return

    if args.check:
        _check(cfg)
        return

    role = _resolve_role(cfg, args.role)
    emitter = {"rpi5": _rpi5_env, "orin": _orin_env}[role]
    _emit(emitter(cfg))


if __name__ == "__main__":
    main()
