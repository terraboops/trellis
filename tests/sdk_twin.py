"""Digital twin for ClaudeSDKClient — simulates agent behavior without real Claude.

This module provides FakeClaudeSDKClient, a drop-in replacement for the
Claude Agent SDK's ClaudeSDKClient. Instead of calling Claude, it executes
scripted "agent behaviors" that call MCP tools directly, simulating what a
real agent would do during each pipeline phase.

Design follows the StrongDM digital twin pattern: a lightweight fake that
honors the same API contract as the real service, producing realistic
responses that exercise downstream code paths.

Usage in tests:
    with patch("trellis.core.agent.ClaudeSDKClient", FakeClaudeSDKClient):
        # agent.run() will use the fake instead of real Claude
"""

from __future__ import annotations

from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)


# ── Scripted behaviors per agent role ──────────────────────────────────

# Each behavior is a list of (tool_name, tool_input) pairs the fake agent
# will "call", plus a final text response.  The MCP tools are not actually
# invoked here — instead, FakeClaudeSDKClient builds realistic message
# sequences that look like what the pool/orchestrator expects to see.

AGENT_SCRIPTS: dict[str, dict] = {
    "ideation": {
        "artifacts": [
            ("feasibility.html", "<h1>Feasibility Analysis</h1><p>Market is viable.</p>"),
            ("market-landscape.html", "<h1>Market Landscape</h1><p>Three competitors.</p>"),
        ],
        "recommendation": "proceed",
        "reasoning": "Market research complete. Ready for implementation.",
        "text": "I've completed the market research and feasibility analysis.",
    },
    "implementation": {
        "artifacts": [
            ("implementation-plan.html", "<h1>Implementation Plan</h1><p>MVP in 4 weeks.</p>"),
        ],
        "recommendation": "proceed",
        "reasoning": "MVP plan is solid. Ready for validation.",
        "text": "Implementation plan created with timeline and resource estimates.",
    },
    "validation": {
        "artifacts": [
            ("validation-report.html", "<h1>Validation Report</h1><p>All checks passed.</p>"),
        ],
        "recommendation": "proceed",
        "reasoning": "Validation complete. Ready for release.",
        "text": "QA validation passed. No blocking issues found.",
    },
    "release": {
        "artifacts": [
            ("launch-blueprint.html", "<h1>Launch Blueprint</h1><p>Go-to-market plan.</p>"),
        ],
        "recommendation": "proceed",
        "reasoning": "Release preparation complete.",
        "text": "Launch blueprint and release materials are ready.",
    },
}

# Default script for unknown roles
DEFAULT_SCRIPT = {
    "artifacts": [],
    "recommendation": "proceed",
    "reasoning": "Work complete.",
    "text": "Done.",
}


def get_script(role: str) -> dict:
    """Get the behavior script for a given agent role."""
    return AGENT_SCRIPTS.get(role, DEFAULT_SCRIPT)


# ── Fake SDK Client ───────────────────────────────────────────────────


class FakeClaudeSDKClient:
    """Drop-in replacement for ClaudeSDKClient that simulates agent behavior.

    Instead of launching a Claude subprocess, this fake:
    1. Extracts the agent role from the system prompt or options
    2. Looks up a scripted behavior for that role
    3. Calls MCP blackboard tools directly to create artifacts
    4. Yields realistic AssistantMessage/ResultMessage sequences

    This exercises the full Trellis pipeline (scheduling, result handling,
    phase transitions) without requiring Claude API access.
    """

    # Track all invocations for test assertions
    invocations: list[dict] = []

    # Set by _patch_agent_run() to pass role from agent.config.name
    _current_role: str | None = None

    def __init__(self, *, options: ClaudeAgentOptions) -> None:
        self.options = options
        self._query_text: str = ""
        # Prefer explicitly-set role (from patched agent.run), fall back to detection
        self._role = FakeClaudeSDKClient._current_role or self._detect_role(options)
        FakeClaudeSDKClient._current_role = None  # consume
        self._script = get_script(self._role)
        # Extract blackboard MCP server for direct tool calls
        self._bb_server = (options.mcp_servers or {}).get("blackboard")

    def _detect_role(self, options: ClaudeAgentOptions) -> str:
        """Detect the agent role from cwd path or system prompt.

        The cwd is set by BaseAgent.get_working_dir() to:
            {project_root}/agents/{role_name}
        So we extract the directory name after 'agents/'.
        """
        from pathlib import PurePosixPath

        cwd = options.cwd or ""
        # Primary: extract role from cwd path (most reliable)
        parts = PurePosixPath(cwd).parts
        if "agents" in parts:
            idx = parts.index("agents")
            if idx + 1 < len(parts):
                candidate = parts[idx + 1]
                if candidate in AGENT_SCRIPTS:
                    return candidate

        # Fallback: check system prompt for role keywords
        prompt = options.system_prompt or ""
        for role in AGENT_SCRIPTS:
            if f"You are the {role} agent" in prompt:
                return role

        prompt_lower = prompt.lower()
        for role in AGENT_SCRIPTS:
            if role in prompt_lower:
                return role
        return "unknown"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def query(self, prompt: str) -> None:
        """Record the query for later response generation."""
        self._query_text = prompt

    async def receive_response(self) -> AsyncIterator:
        """Yield messages simulating agent behavior.

        Produces a sequence of:
        1. AssistantMessage with tool_use blocks (artifact creation)
        2. AssistantMessage with text summary
        3. ResultMessage with cost data
        """
        # Record invocation for test assertions
        FakeClaudeSDKClient.invocations.append(
            {
                "role": self._role,
                "query": self._query_text,
                "system_prompt": self.options.system_prompt or "",
                "model": self.options.model,
            }
        )

        # If we have a blackboard MCP server, call its tools directly
        # to create artifacts and set phase recommendation
        if self._bb_server:
            await self._execute_tools()

        # Yield tool_use message (shows what the agent "did")
        tool_blocks = []
        for filename, content in self._script["artifacts"]:
            tool_blocks.append(
                ToolUseBlock(
                    id=f"tool_{filename.replace('.', '_')}",
                    name="write_blackboard",
                    input={"filename": filename, "content": content},
                )
            )
        # Phase recommendation tool call
        tool_blocks.append(
            ToolUseBlock(
                id="tool_phase_rec",
                name="set_phase_recommendation",
                input={
                    "recommendation": self._script["recommendation"],
                    "reasoning": self._script["reasoning"],
                },
            )
        )

        model = self.options.model or "claude-sonnet-4-6"

        if tool_blocks:
            yield AssistantMessage(
                content=tool_blocks,
                model=model,
            )

        # Yield text response
        yield AssistantMessage(
            content=[TextBlock(text=self._script["text"])],
            model=model,
        )

        # Yield result
        yield ResultMessage(
            subtype="result",
            result=self._script["text"],
            stop_reason="end_turn",
            total_cost_usd=0.01,
            usage={"input_tokens": 1000, "output_tokens": 500},
            session_id="fake-session-001",
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
        )

    async def _execute_tools(self) -> None:
        """Execute MCP tools on the blackboard server to create real artifacts.

        This is what makes the digital twin work: the fake agent actually
        writes artifacts and sets phase recommendations on the real blackboard,
        so downstream code (pool result handling, phase transitions) works
        against real filesystem state.
        """
        server = self._bb_server
        if not server:
            return

        # Call tool functions directly via the MCP server's tool registry
        # MCP servers expose tools as callables — find and invoke them
        tools = {}
        if hasattr(server, "_tool_handlers"):
            tools = server._tool_handlers
        elif hasattr(server, "list_tools") and hasattr(server, "call_tool"):
            # Standard MCP server interface
            tools = {"__mcp__": server}

        if not tools:
            return

        # Create artifacts
        for filename, content in self._script["artifacts"]:
            try:
                if "__mcp__" in tools:
                    await tools["__mcp__"].call_tool(
                        "write_blackboard",
                        {"filename": filename, "content": content},
                    )
                elif "write_blackboard" in tools:
                    await tools["write_blackboard"](filename=filename, content=content)
            except Exception:
                pass  # Best effort — some tests may not have full MCP setup

        # Set phase recommendation
        try:
            if "__mcp__" in tools:
                await tools["__mcp__"].call_tool(
                    "set_phase_recommendation",
                    {
                        "recommendation": self._script["recommendation"],
                        "reasoning": self._script["reasoning"],
                    },
                )
            elif "set_phase_recommendation" in tools:
                await tools["set_phase_recommendation"](
                    recommendation=self._script["recommendation"],
                    reasoning=self._script["reasoning"],
                )
        except Exception:
            pass

    @classmethod
    def reset(cls) -> None:
        """Reset invocation tracking between tests."""
        cls.invocations.clear()


def patch_sdk_with_twin():
    """Context manager that patches ClaudeSDKClient AND injects agent role info.

    The challenge: ClaudeSDKClient is created inside BaseAgent._run_inner(),
    and some agents (ImplementationAgent) use workspace dirs that don't
    contain the role name. We patch BaseAgent._run_inner to set the role
    on FakeClaudeSDKClient before the client is created.

    Usage:
        with patch_sdk_with_twin():
            pool = PoolManager(settings)
            await run_pool(pool)
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    from trellis.core.agent import BaseAgent

    original_run_inner = BaseAgent._run_inner

    async def _patched_run_inner(self, idea_id, **kwargs):
        FakeClaudeSDKClient._current_role = self.config.name
        return await original_run_inner(self, idea_id, **kwargs)

    @contextmanager
    def _patch():
        with patch("trellis.core.agent.ClaudeSDKClient", FakeClaudeSDKClient):
            with patch.object(BaseAgent, "_run_inner", _patched_run_inner):
                yield

    return _patch()


class FailingClaudeSDKClient(FakeClaudeSDKClient):
    """Variant that raises an exception during response, simulating SDK failures."""

    def __init__(self, *, options: ClaudeAgentOptions, error: str = "SDK connection failed"):
        super().__init__(options=options)
        self._error = error

    async def receive_response(self) -> AsyncIterator:
        raise RuntimeError(self._error)
        yield  # pragma: no cover — makes this an async generator


class SlowClaudeSDKClient(FakeClaudeSDKClient):
    """Variant that takes too long, testing timeout handling."""

    def __init__(self, *, options: ClaudeAgentOptions, delay: float = 999):
        super().__init__(options=options)
        self._delay = delay

    async def receive_response(self) -> AsyncIterator:
        import asyncio

        await asyncio.sleep(self._delay)
        yield  # pragma: no cover
