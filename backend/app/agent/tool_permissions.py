"""Per-tool permission rules (Codex CLI / Claude Code parity).

A rule is a triple (tool_name, decision, source) where:
  * tool_name -> a tool identifier from app/agent/tools/inventory (e.g.
                  "mcp:fs:fs_write", "skill:browser.search", "hybrid_search")
  * decision -> "allow" | "deny" | "ask"
  * source -> "user" or "default" (defaults are seeded on first load)

Rules are layered: deny beats allow, ask beats allow, deny beats ask. The
hook + permission-broker code consult ``is_tool_allowed()`` before running
anything expensive so a misbehaving agent cannot pick the lock.

We persist the user-managed allow/deny list to a JSON file under
``backend/data/permissions.json`` so it survives restarts but does not require
a DB migration. Default rules ship with the package and merge in on init.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable

from app.config import settings

_log = logging.getLogger(__name__)

DATA_PERMS_FILENAME = "permissions.json"

# Tools that mutate user-visible state by default require explicit approval
# ("ask"). Read-only tools are "allow". Pure-overhead tools (mcp_discover) are
# "allow" so the agent can find its own capabilities.
#
# NOTE: do NOT add a bare "mcp_invoke" key here. The runtime permission check
# in mcp_tools.py uses the three-segment form "mcp:<server>:<tool>" (see
# mcp_tools.is_tool_allowed invocation). The agent-layer alias "mcp_invoke"
# is gated by the permission broker in app/agent/tools/permissions.py before
# it reaches this layer, so listing it here would either be dead code or, worse,
# mislead the operator into believing a broad allow was in effect.
_DEFAULT_RULES: dict[str, str] = {
  "hybrid_search": "allow",
  "mcp_discover_tools": "allow",
  "mcp:fs:fs_read": "allow",
  "mcp:fs:fs_ls": "allow",
  "mcp:fs:fs_write": "ask",
  "mcp:fs:fs_mkdir": "ask",
  "mcp:fs:fs_delete": "deny",   # default deny; user must opt-in
  "delete_note": "ask",
  "ingest_url": "ask",
  "ingest_text": "ask",
  "ingest_file": "ask",
  "skill:*": "allow",
}


_user_rules: dict[str, str] = {}
_lock = threading.Lock()


def _perms_path() -> Path:
    p = Path(settings.data_dir) / DATA_PERMS_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load() -> None:
    """Reload user rules from disk. Idempotent / thread-safe."""
    global _user_rules
    p = _perms_path()
    if not p.is_file():
        _user_rules = {}
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _user_rules = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
        else:
            _user_rules = {}
    except Exception as e:
        _log.warning("permissions: failed to read %s: %s", p, e)
        _user_rules = {}


def save() -> None:
    """Persist current user rules to disk."""
    with _lock:
        _perms_path().write_text(
            json.dumps(_user_rules, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def list_rules() -> list[dict]:
    """Return merged rules sorted by tool name."""
    merged: dict[str, str] = dict(_DEFAULT_RULES)
    merged.update(_user_rules)
    return sorted(
      ({"tool": k, "decision": v, "source": "user" if k in _user_rules else "default"} for k, v in merged.items()),
      key=lambda r: r["tool"],
    )


def set_rule(tool: str, decision: str) -> dict:
    """Set a single user-level rule. Empty tool or decision raises ValueError.

    Decisions outside the known set are allowed (``"custom"``) so the
    frontend can experiment; the enforcer only special-cases allow/deny/ask.
    """
    if not tool:
        raise ValueError("tool name required")
    decision = (decision or "").strip().lower()
    if decision not in ("allow", "deny", "ask", "inherit"):
        raise ValueError("decision must be one of allow|deny|ask|inherit")
    if decision == "inherit":
        _user_rules.pop(tool, None)
    else:
        _user_rules[tool] = decision
    save()
    return {"tool": tool, "decision": _user_rules.get(tool, "inherit")}


def _match(tool_pattern: str, tool_name: str) -> bool:
    if tool_pattern == "*":
        return True
    if tool_pattern.endswith(":*"):
        return tool_name.startswith(tool_pattern[:-1])
    return tool_pattern == tool_name


def is_tool_allowed(tool_name: str) -> tuple[str, str]:
    """Return (decision, source) for a given tool.

    Resolution order:
      1. user deny   -> always deny
      2. user allow  -> always allow
      3. user ask    -> deferred to the permission broker
      4. default rule
      5. fallback 'ask' for unknowns
    """
    name = tool_name or "*"
    # User rules first
    for pat, dec in _user_rules.items():
        if _match(pat, name):
            return dec, "user"
    # Then defaults
    for pat, dec in _DEFAULT_RULES.items():
        if _match(pat, name):
            return dec, "default"
    return "ask", "fallback"


def requires_approval(tool_name: str) -> bool:
    """True iff ``is_tool_allowed`` returned ask or deny."""
    dec, _ = is_tool_allowed(tool_name)
    return dec in ("ask", "deny")


# Eager-load on import so the API can list them immediately.
load()


__all__ = [
    "list_rules",
    "set_rule",
    "is_tool_allowed",
    "requires_approval",
    "load",
    "save",
]
