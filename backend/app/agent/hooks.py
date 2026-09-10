"""Hooks system (Codex CLI / Claude Code parity).

Lifecycle hooks: small user-supplied scripts that run at well-defined
points in the agent loop. Two phases are exposed:

  * **PreToolUse**  - fires *before* a tool (MCP / skill / native) runs.
                     Returning ``block: <reason>`` aborts the call.
  * **PostToolUse** - fires *after* a tool returns. Can rewrite the
                     ``result_preview`` that ends up in the chat log.

Hooks live in ``data/hooks/`` as plain Python files. Each file declares
a list of dicts with::

    {
        "name": "lint-python",
        "phase": "PreToolUse" | "PostToolUse",
        "tool": "mcp:fs_write" | "skill:<skill_id>" | "*",
        "script": "scripts/lint_python.py",
    }

Script protocol:
    stdin:  JSON {"event": {...}, "phase": "PreToolUse", "tool": "..."}
    stdout (PreToolUse only): JSON {"block": "reason"} to veto
    exit code: 0 = allow, non-zero = block (stderr surfaced as the reason)

The runner is intentionally simple: it shells out, so user code can
be in any language that consumes JSON on stdin. Cost is bounded by a
``timeout_s`` cap (default 5s) so a misbehaving hook can't wedge the
agent loop.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from app.config import settings

_log = logging.getLogger(__name__)

DATA_HOOKS_DIRNAME = "hooks"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"

_DEFAULT_TIMEOUT_S = 5.0
_MAX_HOOKS_PER_EVENT = 8


@dataclass
class HookSpec:
    name: str
    phase: str        # PreToolUse | PostToolUse
    tool: str         # "mcp:server_id:tool" | "skill:skill_id" | "*"
    script: str       # path relative to hooks dir, or absolute
    timeout_s: float = _DEFAULT_TIMEOUT_S
    enabled: bool = True


# === In-memory registry =====================================================
_hooks: list[HookSpec] = []
_lock = threading.Lock()
_last_run: dict[str, dict] = {}  # hook_name -> last run summary


def hooks_dir() -> Path:
    """Project-local hooks directory. Created lazily on first write."""
    p = Path(settings.data_dir) / DATA_HOOKS_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def hooks_index_path() -> Path:
    return hooks_dir() / "hooks.json"


def reload() -> None:
    """Reload hook specs from disk. Safe to call concurrently."""
    global _hooks
    p = hooks_index_path()
    if not p.is_file():
        _hooks = []
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        out: list[HookSpec] = []
        for entry in raw if isinstance(raw, list) else []:
            try:
                out.append(HookSpec(
                    name=entry["name"],
                    phase=entry["phase"],
                    tool=entry.get("tool", "*"),
                    script=entry["script"],
                    timeout_s=float(entry.get("timeout_s") or _DEFAULT_TIMEOUT_S),
                    enabled=bool(entry.get("enabled", True)),
                ))
            except Exception as e:
                _log.warning("bad hook entry %r: %s", entry, e)
        with _lock:
            _hooks = out
    except Exception as e:
        _log.warning("hooks reload failed: %s", e)


def list_hooks() -> list[dict]:
    if not _hooks:
        reload()
    return [asdict(h) for h in _hooks]


def set_hooks(specs: list[dict]) -> int:
    """Replace the hook registry. Returns the number of saved entries."""
    hooks_index_path().parent.mkdir(parents=True, exist_ok=True)
    hooks_index_path().write_text(
        json.dumps(specs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reload()
    return len(specs)


def _match_tool(spec_tool: str, event_tool: str) -> bool:
    if spec_tool in ("*", ""):
        return True
    return spec_tool == event_tool


def _specs_for(phase: str, event_tool: str) -> list[HookSpec]:
    if not _hooks:
        reload()
    return [h for h in _hooks if h.enabled and h.phase == phase and _match_tool(h.tool, event_tool)][: _MAX_HOOKS_PER_EVENT]


@dataclass
class HookRunResult:
    name: str
    phase: str
    tool: str
    duration_ms: int
    decision: str = "allow"   # allow | block
    reason: str = ""
    error: str = ""
    stdout: str = ""


def _resolve_script_path(script: str) -> Optional[Path]:
    p = Path(script)
    if p.is_absolute() and p.is_file():
        return p
    cand = hooks_dir() / script
    if cand.is_file():
        return cand
    cand2 = hooks_dir() / "scripts" / script
    if cand2.is_file():
        return cand2
    return None


def _run_one(spec: HookSpec, event: dict) -> HookRunResult:
    """Invoke a single hook script. Returns timing + outcome."""
    script_path = _resolve_script_path(spec.script)
    started = time.monotonic()
    result = HookRunResult(name=spec.name, phase=spec.phase, tool=spec.tool, duration_ms=0)
    if not script_path:
        result.error = f"script not found: {spec.script}"
        result.decision = "block" if spec.phase == PRE_TOOL_USE else "allow"
        result.reason = result.error
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps({"event": event, "phase": spec.phase, "tool": event.get("tool")}, ensure_ascii=False),
            capture_output=True,
            text=True,
            # Bounded hook execution: floor 0.5s, ceiling 30s. A
            # misconfigured /user/ hook must never wedge the agent.
            timeout=max(0.5, min(30.0, spec.timeout_s)),
        )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.stdout = (proc.stdout or "").strip()
        if proc.returncode != 0:
            result.decision = "block" if spec.phase == PRE_TOOL_USE else "allow"
            result.reason = (proc.stderr or proc.stdout or "").strip()[:500] or f"exit {proc.returncode}"
        else:
            # PreToolUse hooks can still veto via {"block": "..."} JSON
            if spec.phase == PRE_TOOL_USE and result.stdout:
                try:
                    payload = json.loads(result.stdout)
                    if isinstance(payload, dict) and payload.get("block"):
                        result.decision = "block"
                        result.reason = str(payload.get("block"))[:500]
                except json.JSONDecodeError:
                    pass
    except subprocess.TimeoutExpired:
        result.duration_ms = int(spec.timeout_s * 1000)
        result.error = f"timeout after {spec.timeout_s}s"
        result.decision = "block" if spec.phase == PRE_TOOL_USE else "allow"
        result.reason = result.error
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.decision = "block" if spec.phase == PRE_TOOL_USE else "allow"
        result.reason = result.error
    return result


def fire(phase: str, event: dict) -> list[HookRunResult]:
    """Fire all matching hooks for the given phase. Never raises.

    ``_last_run`` is a module-level dict shared across all threads and the
    API surface (``last_runs``). Concurrent ``fire`` calls on the same hook
    name would otherwise race on the dict write AND ``last_runs`` could see
    ``RuntimeError: dictionary changed size during iteration``. Guard both
    the read and write paths with the existing ``_lock``.
    """
    out: list[HookRunResult] = []
    tool_name = event.get("tool") or "*"
    for spec in _specs_for(phase, tool_name):
        r = _run_one(spec, event)
        out.append(r)
        # Atomic update: snapshot dataclass under the lock so concurrent
        # ``last_runs()`` callers cannot observe a half-built dict.
        with _lock:
            _last_run[spec.name] = asdict(r)
    return out


def is_blocked(results: list[HookRunResult]) -> tuple[bool, str]:
    """Return (blocked, reason) - True if any PreToolUse hook vetoed."""
    for r in results:
        if r.phase == PRE_TOOL_USE and r.decision == "block":
            return True, r.reason or f"blocked by {r.name}"
    return False, ""


def last_runs() -> list[dict]:
    """Snapshot of the last-run registry. Safe under concurrent ``fire()``."""
    with _lock:
        # Materialize values under the lock so the returned list cannot
        # mutate out from under the caller if ``fire()`` writes concurrently.
        return [dict(v) for v in _last_run.values()]


__all__ = [
    "HookSpec",
    "HookRunResult",
    "list_hooks",
    "set_hooks",
    "reload",
    "fire",
    "is_blocked",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
]