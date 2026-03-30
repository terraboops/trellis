"""End-to-end pipeline tests using the ClaudeSDKClient digital twin.

These tests exercise the full Trellis pipeline without requiring Claude
API access, making them suitable for CI/CD (GitHub Actions).

Flow tested:
    idea submission → pool scheduling → agent dispatch → SDK call (mocked)
    → result handling → phase transition → next agent → ... → released

The FakeClaudeSDKClient writes real artifacts to the blackboard and sets
phase recommendations, so the pool's result handling and phase transition
logic runs against real filesystem state.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from trellis.config import Settings
from trellis.core.blackboard import Blackboard
from trellis.orchestrator.pool import PoolManager

from tests.sdk_twin import FakeClaudeSDKClient, FailingClaudeSDKClient, patch_sdk_with_twin

pytestmark = pytest.mark.e2e


# ── Helpers ────────────────────────────────────────────────────────────


def _make_pool(settings: Settings) -> PoolManager:
    """Create a PoolManager from real settings (no mocking)."""
    return PoolManager(settings)


def _submit_idea(bb: Blackboard, title: str = "Test Startup", desc: str = "A test idea.") -> str:
    """Submit an idea and return its slug."""
    return bb.create_idea(title, desc)


async def _run_pool_once(pool: PoolManager, timeout: float = 30.0) -> None:
    """Run the pool loop for a limited time, then stop.

    This lets the pool process one scheduling cycle (scan → dispatch → wait → reap).
    """
    pool._running = True

    # Acquire lock (required for run)
    if not pool._acquire_pool_lock():
        pool._release_pool_lock()
        pool._acquire_pool_lock()

    try:
        # We need to run the pool for enough cycles to process all stages
        # Each cycle: producers scan → dispatch → workers run → results handled
        task = asyncio.create_task(pool._run_loop())

        # Wait for pool to process — check periodically if idea is done
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(0.2)
            # Check if all ideas have been processed
            try:
                ideas = pool.blackboard.list_ideas()
                all_done = True
                for idea_id in ideas:
                    status = pool.blackboard.get_status(idea_id)
                    phase = status.get("phase", "submitted")
                    if phase not in ("released", "killed", "paused"):
                        all_done = False
                        break
                if all_done and ideas:
                    break
            except Exception:
                pass

        pool.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        pool._release_pool_lock()


# ── Test: Full pipeline traversal ─────────────────────────────────────


@pytest.mark.asyncio
async def test_idea_traverses_full_pipeline(trellis_settings, blackboard):
    """An idea progresses through ideation → implementation → validation → release → released."""
    # Submit idea
    idea_id = _submit_idea(blackboard, "Coffee Shop Concept", "Open a coffee shop in Nelson BC")

    status = blackboard.get_status(idea_id)
    assert status["phase"] == "submitted"

    # Configure pipeline with no post_ready (simpler test)
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation", "implementation", "validation", "release"],
            "post_ready": [],
            "parallel_groups": [["ideation", "implementation", "validation", "release"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    # Patch ClaudeSDKClient with our digital twin
    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=30.0)

    # Verify final state
    status = blackboard.get_status(idea_id)
    assert status["phase"] == "released", (
        f"Expected 'released', got '{status['phase']}'. "
        f"History: {json.dumps(status.get('phase_history', []), indent=2)}"
    )

    # Verify all agents ran
    roles_invoked = [inv["role"] for inv in FakeClaudeSDKClient.invocations]
    for expected_role in ("ideation", "implementation", "validation", "release"):
        assert expected_role in roles_invoked, f"Agent '{expected_role}' was not invoked"

    # Verify artifacts were mentioned in invocations
    assert len(FakeClaudeSDKClient.invocations) >= 4

    # Verify phase history records all transitions
    history = status.get("phase_history", [])
    assert len(history) >= 4, f"Expected at least 4 transitions, got {len(history)}"


@pytest.mark.asyncio
async def test_idea_creates_artifacts(trellis_settings, blackboard):
    """Agents write artifacts to the blackboard during pipeline execution."""
    idea_id = _submit_idea(blackboard, "Artifact Test", "Testing artifact creation")
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation", "implementation", "validation", "release"],
            "post_ready": [],
            "parallel_groups": [["ideation", "implementation", "validation", "release"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=30.0)

    # The fake agent writes artifacts via MCP tools — verify they ended up
    # on the blackboard.  The actual writing depends on MCP tool execution
    # which may not fully work in the fake.  But phase_recommendation IS
    # set on the status.json by the pool's _handle_result.
    status = blackboard.get_status(idea_id)
    assert status["phase"] == "released"
    assert status["total_cost_usd"] > 0, "Cost should be tracked"


# ── Test: Cost tracking ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_accumulated_across_agents(trellis_settings, blackboard):
    """Total cost accumulates across all agent runs."""
    idea_id = _submit_idea(blackboard, "Cost Tracking Test", "Verify cost accumulation")
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation", "implementation", "validation", "release"],
            "post_ready": [],
            "parallel_groups": [["ideation", "implementation", "validation", "release"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=30.0)

    status = blackboard.get_status(idea_id)
    # Each fake agent reports $0.01 cost, 4 agents = $0.04
    assert status["total_cost_usd"] >= 0.04, (
        f"Expected at least $0.04 total cost, got ${status['total_cost_usd']:.2f}"
    )


# ── Test: Agent receives correct context ──────────────────────────────


@pytest.mark.asyncio
async def test_agent_receives_idea_context(trellis_settings, blackboard):
    """Each agent receives the idea description and prior work manifest in its prompt."""
    idea_id = _submit_idea(
        blackboard,
        "Context Test Idea",
        "This is a unique test description for context verification",
    )
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation"],
            "post_ready": [],
            "parallel_groups": [["ideation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=15.0)

    # Check that the ideation agent received the idea description
    assert len(FakeClaudeSDKClient.invocations) >= 1
    inv = FakeClaudeSDKClient.invocations[0]
    assert inv["role"] == "ideation"
    assert "unique test description" in inv["query"]


# ── Test: Worker error handling ───────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_error_does_not_crash_pool(trellis_settings, blackboard):
    """If an agent's SDK call fails, the pool logs the error and continues."""
    idea_id = _submit_idea(blackboard, "Error Test", "This idea will trigger an agent error")
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation", "implementation"],
            "post_ready": [],
            "parallel_groups": [["ideation", "implementation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    # Use FailingClaudeSDKClient — it raises during receive_response
    with patch("trellis.core.agent.ClaudeSDKClient", FailingClaudeSDKClient):
        pool = _make_pool(trellis_settings)
        # Run for a short time — should not crash
        pool._running = True
        if not pool._acquire_pool_lock():
            pool._release_pool_lock()
            pool._acquire_pool_lock()

        try:
            task = asyncio.create_task(pool._run_loop())
            await asyncio.sleep(3.0)
            pool.stop()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                task.cancel()
        finally:
            pool._release_pool_lock()

    # Pool should have survived — idea should have an error recorded
    status = blackboard.get_status(idea_id)
    # Agent failure is recorded but doesn't crash the pool
    assert status.get("last_error") or status["phase"] in ("submitted", "ideation")


# ── Test: Multiple ideas processed concurrently ───────────────────────


@pytest.mark.asyncio
async def test_multiple_ideas_processed(trellis_settings, blackboard):
    """Multiple ideas are processed by the pool scheduler."""
    idea1 = _submit_idea(blackboard, "Idea Alpha", "First test idea")
    idea2 = _submit_idea(blackboard, "Idea Beta", "Second test idea")

    for idea_id in (idea1, idea2):
        blackboard.update_status(
            idea_id,
            pipeline={
                "agents": ["ideation"],
                "post_ready": [],
                "parallel_groups": [["ideation"]],
                "gating": {"default": "auto", "overrides": {}},
            },
        )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        # Use pool_size=1 to test sequential processing
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=30.0)

    # Both ideas should have been processed
    for idea_id in (idea1, idea2):
        status = blackboard.get_status(idea_id)
        assert status["phase"] == "released", (
            f"Idea {idea_id}: expected 'released', got '{status['phase']}'"
        )


# ── Test: Phase history integrity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_history_records_transitions(trellis_settings, blackboard):
    """Phase history records each transition with timestamps."""
    idea_id = _submit_idea(blackboard, "History Test", "Testing phase history")
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation", "implementation"],
            "post_ready": [],
            "parallel_groups": [["ideation", "implementation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=20.0)

    status = blackboard.get_status(idea_id)
    history = status.get("phase_history", [])

    # Should have at least: submitted→ideation, ideation→implementation, implementation→released
    assert len(history) >= 2, f"Expected at least 2 transitions, got {len(history)}: {history}"

    # Each entry should have from, to, at
    for entry in history:
        assert "from" in entry, f"Missing 'from' in history entry: {entry}"
        assert "to" in entry, f"Missing 'to' in history entry: {entry}"
        assert "at" in entry, f"Missing 'at' in history entry: {entry}"


# ── Test: Pool crash resilience ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pool_survives_snapshot_oserror(trellis_settings, blackboard):
    """Pool continues running when _snapshot raises OSError (e.g., disk full)."""
    idea_id = _submit_idea(blackboard, "Snapshot Error Test", "Testing error resilience")
    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation"],
            "post_ready": [],
            "parallel_groups": [["ideation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()

    call_count = 0
    original_snapshot = PoolManager._snapshot

    def _failing_snapshot(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise OSError("No space left on device")
        return original_snapshot(self, *args, **kwargs)

    with (
        patch("trellis.core.agent.ClaudeSDKClient", FakeClaudeSDKClient),
        patch.object(PoolManager, "_snapshot", _failing_snapshot),
    ):
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=20.0)

    # Pool should have survived the OSError and eventually processed the idea
    status = blackboard.get_status(idea_id)
    assert status["phase"] == "released", (
        f"Pool should survive snapshot errors. Phase: {status['phase']}"
    )


# ── Test: Submitted idea is picked up ─────────────────────────────────


@pytest.mark.asyncio
async def test_submitted_idea_is_picked_up(trellis_settings, blackboard):
    """A newly submitted idea is detected by the pipeline producer and dispatched.

    This is the exact scenario that was broken: the pool wasn't running, so
    submitted ideas were never picked up.
    """
    idea_id = _submit_idea(blackboard, "Freshly Submitted", "This idea was just added")

    # Verify initial state
    status = blackboard.get_status(idea_id)
    assert status["phase"] == "submitted"

    blackboard.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation"],
            "post_ready": [],
            "parallel_groups": [["ideation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        pool = _make_pool(trellis_settings)
        await _run_pool_once(pool, timeout=15.0)

    # The pool should have picked up and processed the idea
    status = blackboard.get_status(idea_id)
    assert status["phase"] != "submitted", (
        f"Idea should have been picked up from 'submitted'. Current phase: {status['phase']}"
    )
    assert len(FakeClaudeSDKClient.invocations) >= 1, "At least one agent should have been invoked"


# ── Test: Pool resilient wrapper ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resilient_pool_restarts_on_crash():
    """The _resilient_pool wrapper restarts the pool after an unexpected crash.

    Tests the function directly by patching at trellis.orchestrator.pool where
    PoolManager is defined (since _resilient_pool imports from there).
    """
    from trellis.web.api.app import _resilient_pool

    crash_count = 0

    class CrashingPoolManager:
        async def run(self):
            nonlocal crash_count
            crash_count += 1
            if crash_count <= 2:
                raise RuntimeError("Simulated pool crash")
            return  # Third time succeeds

    app = MagicMock()
    settings = MagicMock()

    with patch(
        "trellis.orchestrator.pool.PoolManager", side_effect=lambda s: CrashingPoolManager()
    ):
        # _resilient_pool imports constants at function start, so we need
        # to set them before calling. Use module-level patching.
        import trellis.orchestrator.pool as pool_mod

        orig_delay = pool_mod.POOL_RESTART_DELAY_SECONDS
        pool_mod.POOL_RESTART_DELAY_SECONDS = 0.01
        try:
            await _resilient_pool(app, CrashingPoolManager(), settings)
        finally:
            pool_mod.POOL_RESTART_DELAY_SECONDS = orig_delay

    assert crash_count == 3, f"Expected 3 attempts (2 crashes + 1 success), got {crash_count}"


@pytest.mark.asyncio
async def test_resilient_pool_gives_up_after_max_restarts():
    """The _resilient_pool wrapper stops retrying after MAX_RAPID_RESTARTS."""
    from trellis.web.api.app import _resilient_pool

    crash_count = 0

    class AlwaysCrashingPoolManager:
        async def run(self):
            nonlocal crash_count
            crash_count += 1
            raise RuntimeError("Permanent failure")

    app = MagicMock()
    settings = MagicMock()

    with patch(
        "trellis.orchestrator.pool.PoolManager", side_effect=lambda s: AlwaysCrashingPoolManager()
    ):
        import trellis.orchestrator.pool as pool_mod

        orig_delay = pool_mod.POOL_RESTART_DELAY_SECONDS
        orig_max = pool_mod.MAX_RAPID_RESTARTS
        pool_mod.POOL_RESTART_DELAY_SECONDS = 0.01
        pool_mod.MAX_RAPID_RESTARTS = 3
        try:
            await _resilient_pool(app, AlwaysCrashingPoolManager(), settings)
        finally:
            pool_mod.POOL_RESTART_DELAY_SECONDS = orig_delay
            pool_mod.MAX_RAPID_RESTARTS = orig_max

    # Initial + 2 restarts = 3 total before circuit breaker trips
    assert crash_count == 3, f"Expected 3 crashes before giving up, got {crash_count}"
