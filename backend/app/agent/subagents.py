"""Sub-agent profiles (Codex CLI / Claude Code parity).

A sub-agent is a specialised LLM run spawned from the main chat. Each profile
defines its own system prompt + the tools it is allowed to see:

  * explore  - read-only knowledge-base digester. Best for "what does X say in
               our notes / codebase" style questions; no destructive tools.
  * plan     - emits a structured plan in the same shape as the existing
               planner_node output, but for non-research chat intents.
  * general  - full toolkit; the run-of-the-mill worker that can do anything
               the main agent can do (excluding permission-gated mutating
               MCP servers unless the user has explicitly allowed them).

Sub-agents stream their reasoning + tool calls through the standard
``answer_node_stream`` so the SSE taxonomy (``text_delta`` / ``tool`` /
``citations``) is preserved.

Usage from a route::

    from app.agent.subagents import run_subagent_stream

    async def generate():
        async for ev in run_subagent_stream(mode="explore", query="...",
                                            api_key=..., base_url=...):
            yield _sse("subagent", ev)
            yield "subevent-as-needed"
"""
from __future__ import annotations

import logging
import re
from typing import AsyncGenerator, Iterable

from app.config import settings
from app.agent.state import AgentState
from app.agent.nodes.answer import _build_messages

_log = logging.getLogger(__name__)


SUBAGENT_MODES = {"explore", "plan", "general"}


PROFILES: dict[str, dict[str, str]] = {
    "explore": {
        "label": "Explore",
        "system": (
            "You are the EXPLORE sub-agent (Codex CLI / Claude Code parity). "
            "Your job is to gather information from the available knowledge base "
            "and return a well-sourced answer. You are READ-ONLY: never delete, "
            "rewrite, or persist anything. Use hybrid_search whenever you would "
            "otherwise guess. Cite each claim with [n]. If you cannot find a "
            "definitive answer, say so plainly instead of inventing."
        ),
    },
    "plan": {
        "label": "Plan",
        "system": (
            "You are the PLAN sub-agent (Codex CLI / Claude Code parity). "
            "Decompose the user's request into a concrete, ordered plan. "
            "Output a JSON object with shape: "
            "{\"plan_summary\": str, \"steps\": [{\"query\": str}]}. "
            "Do NOT execute any tool. Do NOT answer the question; only the plan. "
            "The plan is consumed by the router / researcher downstream."
        ),
    },
    "general": {
        "label": "General",
        "system": (
            "You are the GENERAL-PURPOSE sub-agent (Codex CLI / Claude Code parity). "
            "You have full access to the project's tools, knowledge base, and "
            "skills. Prefer parallel tool calls when independent. After acting, "
            "summarise what changed so the main thread can continue."
        ),
    },
}


def _safe_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    return mode if mode in SUBAGENT_MODES else "general"


def _filter_tools_for_mode(all_tool_names: list[str], mode: str) -> list[str]:
    """Restrict the tool palette based on the sub-agent profile.

    Explore: drop obviously-mutating tools. Plan: drop everything not needed
    for planning. General: pass everything through.
    """
    if mode == "general":
        # Defense in depth: sub-agents never run user code through destructive
        # tools directly. They produce a plan, the orchestrator applies it via
        # the regular tool loop where the user-approved permission modal gates
        # every invocation.
        blocked_general = {
            "mcp:fs:fs_write", "mcp:fs:fs_delete", "mcp:fs:fs_mkdir",
            "ingest_url", "ingest_text", "ingest_file", "delete_note",
        }
        return [n for n in all_tool_names if n not in blocked_general]
    blocked_explore = {
        "mcp:fs:fs_write", "mcp:fs:fs_delete", "mcp:fs:fs_mkdir",
        "ingest_url", "ingest_text", "ingest_file", "delete_note",
    }
    blocked_plan = blocked_explore | {
        "hybrid_search",
        "mcp:fs:fs_read", "mcp:fs:fs_ls",
    }
    blocked = blocked_explore if mode == "explore" else blocked_plan
    return [n for n in all_tool_names if n not in blocked]


async def run_subagent_stream(
    mode: str,
    query: str,
    api_key: str | None,
    base_url: str | None,
    *,
    history: list[dict] | None = None,
    extra_context: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream sub-agent reasoning back as ``dict`` events.

    Each yielded dict has at minimum a ``phase`` key plus mode-specific
    payload. The route layer wraps each dict into an SSE ``subagent`` event.

    Never raises - errors are surfaced as ``{"phase": "error", ...}``.
    """
    mode = _safe_mode(mode)
    profile = PROFILES[mode]
    yield {
        "phase": "started",
        "mode": mode,
        "label": profile["label"],
        "query": query[:400],
    }
    try:
        # Reuse the answer-node streaming machinery so SSE event taxonomy is
        # uniform across main agent and sub-agents.
        from app.agent.nodes.answer import answer_node_stream

        state: AgentState = {
            "query": query,
            "rewritten_query": query,
            "messages": list(history or []) if history else [],
            "retrieved_chunks": [],
            "summary": (extra_context or {}).get("summary", ""),
            "memory_facts": (extra_context or {}).get("memory_facts") or [],
            "project_rules": (extra_context or {}).get("project_rules") or "",
            "agent_permission": (extra_context or {}).get("agent_permission") or "default",
            "provider_override": None,
            "model_override": None,
            "api_key_override": api_key,
            "base_url_override": base_url,
            "reasoning_level_override": None,
            "step_count": 0,
        }
        # Sub-agents run a *focused* profile prompt. Override system instructions
        # by stuffing the profile text into the messages ahead of the answer.
        state["subagent_mode"] = mode
        state["subagent_system"] = profile["system"]

        async for ev in answer_node_stream(state, instructions_override=profile["system"]):
            # Tag every event so consumers can isolate sub-agent traffic.
            ev["mode"] = mode
            yield ev
        yield {"phase": "done", "mode": mode}
    except Exception as e:  # noqa: BLE001 - we want to absorb any LLM hiccup.
        _log.warning("subagent %s failed: %s", mode, e)
        yield {"phase": "error", "mode": mode, "detail": str(e)[:300]}


def parse_plan_payload(text: str) -> dict | None:
    """Parse the JSON plan that ``plan``-mode sub-agents are expected to emit.

    Best-effort: returns None on any parse failure (the caller can then fall
    back to the live planner_node). Mirrors app/agent/nodes/planner.py.
    """
    if not text:
        return None
    cleaned = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return None
    try:
        import json
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    steps = obj.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    queries: list[str] = []
    for s in steps:
        q = (s.get("query") if isinstance(s, dict) else s) if s else ""
        q = str(q or "").strip()
        if q and q not in queries:
            queries.append(q)
    if not queries:
        return None
    return {
        "plan_summary": str(obj.get("plan_summary") or "").strip(),
        "steps": [{"query": q} for q in queries[: max(1, int(settings.planner_max_steps))]],
    }


__all__ = [
    "SUBAGENT_MODES",
    "PROFILES",
    "run_subagent_stream",
    "parse_plan_payload",
]
