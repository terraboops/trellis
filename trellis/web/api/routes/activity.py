"""Activity tracking routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trellis.config import get_settings
from trellis.core.activity import ActivityTracker
from trellis.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _get_tracker() -> ActivityTracker:
    settings = get_settings()
    return ActivityTracker(settings.blackboard_dir.parent / ".activity.json")


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    tracker = _get_tracker()
    tracker.clear_stale()
    running = tracker.get_running()
    return templates.TemplateResponse("activity.html", {"request": request, "running": running})


@router.get("/api/activity")
async def api_activity():
    tracker = _get_tracker()
    tracker.clear_stale()
    return {"running": tracker.get_running()}


@router.get("/api/activity/live")
async def api_activity_live(agent: str, idea_id: str):
    """Return the partial in-progress transcript for a running agent."""
    from trellis.core.blackboard import Blackboard

    settings = get_settings()
    bb = Blackboard(settings.blackboard_dir)
    live_path = bb.idea_dir(idea_id) / "agent-logs" / f".live-{agent}.json"
    if not live_path.exists():
        return {
            "agent": agent,
            "idea_id": idea_id,
            "entries": [],
            "process_stderr": [],
            "sandbox_failure": False,
            "last_updated": None,
        }
    try:
        import json

        data = json.loads(live_path.read_text())
        return data
    except Exception:
        return {"agent": agent, "idea_id": idea_id, "entries": [], "last_updated": None}
