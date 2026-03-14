"""Pool status and configuration routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.web.api.paths import TEMPLATES_DIR

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STARVATION_THRESHOLD = 0.5
DEADLINE_WARNING_THRESHOLD = 2


def _read_pool_state() -> dict:
    """Read the latest pool state snapshot."""
    state_path = settings.project_root / "pool" / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {
        "pool_size": settings.pool_size,
        "cycle_time_minutes": settings.cycle_time_minutes,
        "current_window": {"started_at": None, "serviced": [], "remaining_seconds": 0},
        "workers": [],
        "role_health": {},
        "deadline_counts": {},
    }


@router.get("/", response_class=HTMLResponse)
async def pool_status(request: Request):
    state = _read_pool_state()

    # Compute warnings
    starved_roles = []
    for role, health in state.get("role_health", {}).items():
        expected = health.get("expected", 0)
        actual = health.get("actual", 0)
        if expected > 0 and actual / expected < STARVATION_THRESHOLD:
            starved_roles.append({"role": role, "expected": expected, "actual": actual})

    deadline_warnings = []
    for role, count in state.get("deadline_counts", {}).items():
        if count > DEADLINE_WARNING_THRESHOLD:
            deadline_warnings.append({"role": role, "count": count})

    return templates.TemplateResponse("pool.html", {
        "request": request,
        "state": state,
        "starved_roles": starved_roles,
        "deadline_warnings": deadline_warnings,
    })


@router.get("/api/state")
async def api_pool_state():
    return _read_pool_state()
