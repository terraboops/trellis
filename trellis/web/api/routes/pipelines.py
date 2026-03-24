"""Pipeline template management routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from trellis.config import get_settings
from trellis.core.blackboard import Blackboard, DEFAULT_PIPELINE
from trellis.core.pipeline_format import (
    detect_format,
    find_template,
    load_pipeline,
    save_pipeline,
)
from trellis.core.registry import load_registry
from trellis.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _templates_dir() -> Path:
    """Return the pipeline-templates directory, creating it if needed."""
    settings = get_settings()
    d = settings.project_root / "pipeline-templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_default_if_empty(d: Path) -> None:
    """Seed a default template from DEFAULT_PIPELINE if directory is empty."""
    if any(d.glob("*.yaml")) or any(d.glob("*.yml")) or any(d.glob("*.prose")):
        return
    default = {
        "name": "default",
        "description": "Standard 4-stage pipeline with watchers (from DEFAULT_PIPELINE)",
        "agents": list(DEFAULT_PIPELINE["agents"]),
        "post_ready": list(DEFAULT_PIPELINE.get("post_ready", [])),
        "parallel_groups": [list(g) for g in DEFAULT_PIPELINE.get("parallel_groups", [])],
        "gating": {
            "default": DEFAULT_PIPELINE.get("gating", {}).get("default", "auto"),
            "overrides": dict(DEFAULT_PIPELINE.get("gating", {}).get("overrides", {})),
        },
    }
    save_pipeline(d / "default.yaml", default, fmt="yaml")


def _load_template(path: Path) -> dict[str, Any]:
    """Load a single pipeline template file (``.prose``, ``.yaml``, or ``.yml``)."""
    data = load_pipeline(path)
    # Ensure name matches filename
    data.setdefault("name", path.stem)
    data.setdefault("description", "")
    data.setdefault("agents", [])
    data.setdefault("post_ready", [])
    data.setdefault("parallel_groups", [])
    data.setdefault("gating", {"default": "auto", "overrides": {}})
    data["format"] = detect_format(path)
    return data


def _list_templates() -> list[dict[str, Any]]:
    """Load all pipeline templates (Prose, YAML, and YML)."""
    d = _templates_dir()
    _seed_default_if_empty(d)
    result = []
    for pattern in ("*.prose", "*.yaml", "*.yml"):
        for f in sorted(d.glob(pattern)):
            try:
                result.append(_load_template(f))
            except Exception:
                continue
    return result


def _save_template(name: str, data: dict, fmt: str = "yaml") -> None:
    """Save a template in the specified format (``'yaml'`` or ``'prose'``)."""
    d = _templates_dir()
    data["name"] = name
    ext = ".prose" if fmt == "prose" else ".yaml"
    save_pipeline(d / f"{name}{ext}", data, fmt=fmt)


def _get_ideas() -> list[dict]:
    """Get list of active ideas for the 'apply to idea' dropdown."""
    settings = get_settings()
    bb = Blackboard(settings.blackboard_dir)
    ideas = []
    for idea_id in bb.list_ideas():
        try:
            status = bb.get_status(idea_id)
            if status.get("phase") not in ("released", "killed"):
                ideas.append({
                    "id": idea_id,
                    "title": status.get("title", idea_id),
                    "phase": status.get("phase", "unknown"),
                })
        except Exception:
            continue
    return ideas


def _get_available_agents() -> list[dict]:
    """Get all agents from the registry."""
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    return [
        {"name": a.name, "description": a.description, "phase": a.phase or ""}
        for a in registry.agents.values()
    ]


# --- Routes ---

@router.get("/", response_class=HTMLResponse)
async def pipelines_list(request: Request):
    tpls = _list_templates()
    return templates.TemplateResponse("pipelines.html", {
        "request": request,
        "templates": tpls,
    })


@router.get("/new", response_class=HTMLResponse)
async def pipeline_new_form(request: Request):
    blank = {
        "name": "",
        "description": "",
        "agents": [],
        "post_ready": [],
        "parallel_groups": [],
        "gating": {"default": "auto", "overrides": {}},
    }
    return templates.TemplateResponse("pipeline_detail.html", {
        "request": request,
        "pipeline": blank,
        "is_new": True,
        "ideas": _get_ideas(),
        "available_agents": _get_available_agents(),
    })


@router.post("/new", response_class=HTMLResponse)
async def pipeline_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    agents: str = Form(""),
    post_ready: str = Form(""),
    parallel_groups_json: str = Form("[]"),
    gating_default: str = Form("auto"),
    gating_overrides_json: str = Form("{}"),
    template_format: str = Form("yaml"),
):
    slug = name.strip().lower().replace(" ", "-")
    if not slug:
        return RedirectResponse(url="/pipelines/new", status_code=303)

    try:
        parallel_groups = json.loads(parallel_groups_json)
    except (json.JSONDecodeError, TypeError):
        parallel_groups = []

    try:
        gating_overrides = json.loads(gating_overrides_json)
    except (json.JSONDecodeError, TypeError):
        gating_overrides = {}

    agents_list = [a.strip() for a in agents.split(",") if a.strip()]
    post_ready_list = [p.strip() for p in post_ready.split(",") if p.strip()]

    # Auto-generate parallel groups if not provided
    if not parallel_groups:
        parallel_groups = []
        if agents_list:
            parallel_groups.append(agents_list)
        if post_ready_list:
            parallel_groups.append(post_ready_list)

    fmt = "prose" if template_format == "prose" else "yaml"
    data = {
        "name": slug,
        "description": description,
        "agents": agents_list,
        "post_ready": post_ready_list,
        "parallel_groups": parallel_groups,
        "gating": {
            "default": gating_default,
            "overrides": gating_overrides,
        },
    }
    _save_template(slug, data, fmt=fmt)
    return RedirectResponse(url=f"/pipelines/{slug}", status_code=303)


@router.get("/{name}", response_class=HTMLResponse)
async def pipeline_detail(request: Request, name: str):
    d = _templates_dir()
    _seed_default_if_empty(d)
    path = find_template(d, name)
    if path is None:
        return HTMLResponse("Pipeline template not found", status_code=404)

    pipeline = _load_template(path)
    return templates.TemplateResponse("pipeline_detail.html", {
        "request": request,
        "pipeline": pipeline,
        "is_new": False,
        "ideas": _get_ideas(),
        "available_agents": _get_available_agents(),
    })


@router.post("/{name}", response_class=HTMLResponse)
async def pipeline_update(
    request: Request,
    name: str,
    description: str = Form(""),
    agents: str = Form(""),
    post_ready: str = Form(""),
    parallel_groups_json: str = Form("[]"),
    gating_default: str = Form("auto"),
    gating_overrides_json: str = Form("{}"),
):
    d = _templates_dir()
    path = find_template(d, name)
    if path is None:
        return HTMLResponse("Pipeline template not found", status_code=404)

    # Preserve the existing file's format on update
    fmt = detect_format(path)

    try:
        parallel_groups = json.loads(parallel_groups_json)
    except (json.JSONDecodeError, TypeError):
        parallel_groups = []

    try:
        gating_overrides = json.loads(gating_overrides_json)
    except (json.JSONDecodeError, TypeError):
        gating_overrides = {}

    agents_list = [a.strip() for a in agents.split(",") if a.strip()]
    post_ready_list = [p.strip() for p in post_ready.split(",") if p.strip()]

    if not parallel_groups:
        parallel_groups = []
        if agents_list:
            parallel_groups.append(agents_list)
        if post_ready_list:
            parallel_groups.append(post_ready_list)

    data = {
        "name": name,
        "description": description,
        "agents": agents_list,
        "post_ready": post_ready_list,
        "parallel_groups": parallel_groups,
        "gating": {
            "default": gating_default,
            "overrides": gating_overrides,
        },
    }
    _save_template(name, data, fmt=fmt)
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)


@router.post("/{name}/delete")
async def pipeline_delete(name: str):
    d = _templates_dir()
    path = find_template(d, name)
    if path is not None:
        path.unlink()
    return RedirectResponse(url="/pipelines/", status_code=303)


@router.get("/{name}/apply/{idea_id}")
async def pipeline_apply(name: str, idea_id: str):
    d = _templates_dir()
    _seed_default_if_empty(d)
    path = find_template(d, name)
    if path is None:
        return HTMLResponse("Pipeline template not found", status_code=404)

    tpl = _load_template(path)
    settings = get_settings()
    bb = Blackboard(settings.blackboard_dir)

    pipeline = {
        "agents": tpl["agents"],
        "post_ready": tpl["post_ready"],
        "parallel_groups": tpl["parallel_groups"],
        "gating": tpl["gating"],
        "preset": tpl["name"],
    }
    bb.set_pipeline(idea_id, pipeline)
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)
