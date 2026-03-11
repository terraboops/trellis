"""Tests for Worker deadline handling and agent execution."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from incubator.orchestrator.worker import Worker, RunResult, RunStatus


@pytest.fixture
def mock_factory():
    factory = MagicMock()
    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=MagicMock(
        success=True, cost_usd=0.25, error=None, output="Done"
    ))
    factory.create_agent.return_value = mock_agent
    return factory


@pytest.fixture
def worker(mock_factory):
    return Worker(
        worker_id=1,
        factory=mock_factory,
        blackboard=MagicMock(),
        lock_manager=MagicMock(),
    )


def test_run_result_ok():
    """RunResult captures successful completion."""
    r = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=120,
        cost_usd=0.50,
    )
    assert r.status == RunStatus.OK
    assert r.is_deadline is False


def test_run_result_deadline():
    """RunResult captures deadline termination."""
    r = RunResult(
        status=RunStatus.DEADLINE,
        role="implementation",
        idea_id="test-idea",
        duration_seconds=1800,
        cost_usd=1.20,
    )
    assert r.status == RunStatus.DEADLINE
    assert r.is_deadline is True


def test_max_turns_calculation():
    """max_turns is calculated from remaining minutes."""
    w = Worker(worker_id=1, factory=MagicMock(), blackboard=MagicMock(), lock_manager=MagicMock())
    assert w._calculate_max_turns(30) == 60  # 30 min * 2 turns/min
    assert w._calculate_max_turns(5) == 10
    assert w._calculate_max_turns(0) == 4  # minimum 4 turns


@pytest.mark.asyncio
async def test_execute_acquires_and_releases_lock(worker, mock_factory):
    """Worker acquires lock before running and releases after."""
    worker.lock_manager.acquire.return_value = True
    deadline = datetime.now(timezone.utc) + timedelta(minutes=30)

    await worker.execute("ideation", "test-idea", deadline)

    worker.lock_manager.acquire.assert_called_once_with("pool", "test-idea", executor="worker-1")
    worker.lock_manager.release.assert_called_once_with("pool", "test-idea")


@pytest.mark.asyncio
async def test_execute_skips_when_lock_unavailable(worker):
    """Worker returns None when lock can't be acquired."""
    worker.lock_manager.acquire.return_value = False
    deadline = datetime.now(timezone.utc) + timedelta(minutes=30)

    result = await worker.execute("ideation", "test-idea", deadline)

    assert result is None
