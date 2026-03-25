"""End-to-end: trellis init → trellis serve → hit every page.

Proves the full lifecycle works: scaffold a fresh project, start the ASGI app
against it, and verify every major route returns 200 with expected content.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from trellis.cli import app as cli_app
from trellis.config import Settings, _invalidate_settings_cache

runner = CliRunner()


def _settings_for_project(project: Path) -> Settings:
    """Build a Settings object with all paths pointing at the given project."""
    return Settings(
        project_root=project,
        blackboard_dir=project / "blackboard" / "ideas",
        workspace_dir=project / "workspace",
        registry_path=project / "registry.yaml",
    )


@pytest.fixture()
def fresh_project(tmp_path):
    """Scaffold a fresh Trellis project via the CLI and return its path."""
    result = runner.invoke(cli_app, ["init", str(tmp_path / "proj")])
    assert result.exit_code == 0, f"trellis init failed:\n{result.output}"
    project = tmp_path / "proj"
    assert (project / ".trellis").exists()
    assert (project / "registry.yaml").exists()
    assert (project / "blackboard" / "ideas" / "_template" / "status.json").exists()
    return project


@pytest.fixture()
def serve_app(fresh_project):
    """Create a fresh ASGI app bound to the scaffolded project.

    Patches get_settings globally so all route handlers see the tmp project.
    """
    settings = _settings_for_project(fresh_project)

    # Patch get_settings everywhere it's imported (config module + all routes)
    with (
        patch("trellis.config.get_settings", return_value=settings),
        patch("trellis.config._discover_project_root", return_value=fresh_project),
        patch("trellis.web.api.routes.settings.get_settings", return_value=settings),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=settings),
    ):
        _invalidate_settings_cache()
        from trellis.web.api.app import create_app

        yield create_app()

    _invalidate_settings_cache()


# ── Core pages ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_home_page(serve_app):
    """GET / renders the idea dashboard."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert "trellis" in resp.text.lower() or "ideas" in resp.text.lower()


@pytest.mark.asyncio
async def test_settings_page(serve_app):
    """GET /settings/ renders all settings sections."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/settings/")
    assert resp.status_code == 200
    body = resp.text
    assert "Pool" in body
    assert "Quality Gate" in body
    assert "Budget Limits" in body
    assert "Model Tiers" in body
    assert "Global System Prompt" in body


@pytest.mark.asyncio
async def test_settings_api(serve_app):
    """GET /settings/api/settings returns valid JSON with all fields."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/settings/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "pool_size" in data
    assert "min_quality_score" in data
    assert "max_iterate_per_stage" in data


@pytest.mark.asyncio
async def test_pool_page(serve_app):
    """GET /pool/ renders (even with no running pool)."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/pool/")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agents_page(serve_app):
    """GET /agents/ renders the agent registry."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/agents/")
    assert resp.status_code == 200
    # Should list at least one agent from the default registry
    assert "ideation" in resp.text.lower() or "agent" in resp.text.lower()


@pytest.mark.asyncio
async def test_new_idea_page(serve_app):
    """GET /ideas/new renders the new idea form."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/ideas/new")
    assert resp.status_code == 200


# ── Idea detail (template idea from init) ────────────────────────────


@pytest.mark.asyncio
async def test_template_idea_detail(serve_app):
    """GET /ideas/_template renders the scaffolded template idea."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.get("/ideas/_template")
    assert resp.status_code == 200
    body = resp.text
    # Feedback empty state should appear (template idea has no feedback)
    assert "No feedback yet" in body or "feedback-list" in body
    # Give Feedback button should be present
    assert "Give Feedback" in body
    # Agent chips fallback (no agents have serviced template)
    assert "queued for the first agent" in body


# ── Settings save round-trip against fresh project ───────────────────


@pytest.mark.asyncio
async def test_settings_save_round_trip(serve_app, fresh_project):
    """POST /settings/system saves to project_settings.json, API reflects change."""
    with patch("trellis.config._discover_project_root", return_value=fresh_project):
        _invalidate_settings_cache()
        async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
            resp = await c.post(
                "/settings/system",
                data={
                    "pool_size": "5",
                    "job_timeout_minutes": "30",
                    "producer_interval_seconds": "15",
                    "max_iterate_per_stage": "8",
                    "max_refinement_cycles": "4",
                    "min_quality_score": "6.0",
                    "max_budget_ideation": "1.00",
                    "max_budget_implementation": "20.00",
                    "max_budget_validation": "2.00",
                    "max_budget_release": "5.00",
                    "max_budget_watcher": "0.25",
                    "model_tier_high": "claude-opus-4-6",
                    "model_tier_low": "claude-haiku-4-5",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings?saved=system"

        # Verify file landed in the fresh project
        import json

        pf = fresh_project / "project_settings.json"
        assert pf.exists(), "project_settings.json should exist in the project"
        data = json.loads(pf.read_text())
        assert data["min_quality_score"] == 6.0
        assert data["max_iterate_per_stage"] == 8

        _invalidate_settings_cache()


# ── Feedback submit against fresh project ────────────────────────────


@pytest.mark.asyncio
async def test_feedback_submit_on_template_idea(serve_app):
    """POST feedback on the template idea returns ok + entry."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.post(
            "/api/ideas/_template/feedback",
            data={
                "artifact": "idea.md",
                "selected_text": "test selection",
                "comment": "Looks good",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["entry"]["comment"] == "Looks good"
    assert data["entry"]["artifact"] == "idea.md"


# ── Global prompt save against fresh project ─────────────────────────


@pytest.mark.asyncio
async def test_global_prompt_save(serve_app, fresh_project):
    """POST /settings/global-prompt writes to the fresh project."""
    async with AsyncClient(transport=ASGITransport(app=serve_app), base_url="http://test") as c:
        resp = await c.post(
            "/settings/global-prompt", data={"global_prompt": "Be concise."}, follow_redirects=False
        )
    assert resp.status_code == 303
    assert (fresh_project / "global-system-prompt.md").read_text() == "Be concise."
