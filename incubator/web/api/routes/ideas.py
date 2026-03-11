"""Idea CRUD + action routes."""

from __future__ import annotations

import asyncio
import json

import re

import markdown
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.core.blackboard import Blackboard
from incubator.core.registry import load_registry

router = APIRouter()
_templates_dir = str(
    get_settings().project_root / "incubator" / "web" / "frontend" / "templates"
)
templates = Jinja2Templates(directory=_templates_dir)

# Register markdown filter for Jinja2
_md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br", "toc"])


def _render_md(text: str) -> str:
    _md.reset()
    return _md.convert(text)


templates.env.filters["markdown"] = _render_md
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)

_PHASE_LABELS = {
    "released": "ready",
    "killed": "shelved",
    "release": "releasing",
    "ideation_review": "reviewing ideation",
    "implementation_review": "reviewing build",
    "validation_review": "reviewing tests",
}


def _phase_label(phase: str) -> str:
    """Map internal phase names to display labels."""
    label = _PHASE_LABELS.get(phase, phase)
    return label.replace("_", " ")


templates.env.filters["phase_label"] = _phase_label


_CADENCE_PATTERNS = {
    "0 */6 * * *": "every 6h",
    "0 */4 * * *": "every 4h",
    "0 */12 * * *": "every 12h",
    "0 8 * * *": "daily at 8am",
    "0 0 * * *": "daily at midnight",
    "*/30 * * * *": "every 30min",
}


def _cadence_label(cron: str) -> str:
    """Turn common cron expressions into readable labels."""
    return _CADENCE_PATTERNS.get(cron, cron)


templates.env.filters["cadence_label"] = _cadence_label


def _get_blackboard() -> Blackboard:
    return Blackboard(get_settings().blackboard_dir)


def _load_presets() -> dict:
    presets_path = get_settings().project_root / "pool" / "presets.json"
    if presets_path.exists():
        return json.loads(presets_path.read_text())
    return {}


def _get_registered_roles() -> set[str]:
    registry = load_registry(get_settings().registry_path)
    return {a.name for a in registry.agents.values()}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    bb = _get_blackboard()
    ideas = []
    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        status["idea_id"] = idea_id
        ideas.append(status)
    ideas.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    return templates.TemplateResponse("home.html", {"request": request, "ideas": ideas})


@router.get("/ideas/new", response_class=HTMLResponse)
async def new_idea_form(request: Request):
    presets = _load_presets()
    return templates.TemplateResponse("new_idea.html", {"request": request, "presets": presets})


@router.get("/ideas")
async def ideas_redirect():
    return RedirectResponse(url="/", status_code=301)


@router.post("/ideas")
async def create_idea(
    title: str = Form(...),
    description: str = Form(...),
    preset: str = Form("full-pipeline"),
):
    bb = _get_blackboard()
    idea_id = bb.create_idea(title, description)
    presets = _load_presets()
    preset_data = presets.get(preset, presets.get("full-pipeline", {}))
    if preset_data:
        pipeline = {
            "stages": preset_data.get("stages", []),
            "post_ready": preset_data.get("post_ready", []),
            "gating": preset_data.get("gating", {"default": "auto", "overrides": {}}),
            "preset": preset,
        }
        bb.set_pipeline(idea_id, pipeline)
    return RedirectResponse(url=f"/ideas/{idea_id}", status_code=303)


@router.get("/ideas/{idea_id}", response_class=HTMLResponse)
async def idea_detail(request: Request, idea_id: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)

    # Organize files by category — idea.md first, then the rest
    artifacts = {}
    idea_dir = bb.idea_dir(idea_id)

    def _extract_title(content: str, filename: str) -> str:
        """Extract first H1 heading from markdown, fall back to filename."""
        if filename == "idea.md":
            return "Idea"
        m = re.match(r"^#\s+(.+)$", content.strip(), re.MULTILINE)
        if m:
            return m.group(1).strip()
        # Humanize filename: remove extension, replace hyphens
        return filename.rsplit(".", 1)[0].replace("-", " ").title()

    # Collect all files
    raw_artifacts = []
    for f in sorted(idea_dir.iterdir()):
        if f.is_dir():
            continue  # skip agent-logs/ directory
        if f.name == "status.json":
            continue
        content = f.read_text()
        is_empty = len(content.strip().split("\n")) <= 1
        raw_artifacts.append((f.name, {
            "content": content,
            "is_markdown": f.suffix == ".md",
            "is_empty": is_empty,
            "title": _extract_title(content, f.name),
        }))

    # Also pick up HTML artifacts from workspace
    workspace_dir = get_settings().project_root / "workspace" / idea_id
    if workspace_dir.is_dir():
        for f in sorted(workspace_dir.rglob("*.html")):
            rel = f.relative_to(workspace_dir)
            label = f"workspace/{rel}"
            raw_artifacts.append((label, {
                "content": f.read_text(),
                "is_markdown": False,
                "is_empty": False,
                "title": str(rel).rsplit(".", 1)[0].replace("-", " ").title(),
            }))

    # Sort: idea.md first, then alphabetically
    raw_artifacts.sort(key=lambda x: (0 if x[0] == "idea.md" else 1, x[0]))
    for name, info in raw_artifacts:
        artifacts[name] = info

    # Compute per-agent knowledge sizes (approx tokens = chars / 4)
    agent_knowledge = {}
    knowledge_dir = idea_dir / "agent-knowledge"
    if knowledge_dir.is_dir():
        for agent_dir in sorted(knowledge_dir.iterdir()):
            if agent_dir.is_dir():
                total_chars = 0
                file_count = 0
                for f in agent_dir.rglob("*"):
                    if f.is_file():
                        total_chars += f.stat().st_size
                        file_count += 1
                agent_knowledge[agent_dir.name] = {
                    "chars": total_chars,
                    "tokens": total_chars // 4,
                    "files": file_count,
                }

    # Count releases for refinement cycle display
    history = status.get("phase_history", [])
    release_count = sum(1 for entry in history if entry.get("to") == "released")

    is_running = status.get("running", False)
    stop_requested = status.get("stop_requested", False)

    return templates.TemplateResponse(
        "idea_detail.html",
        {
            "request": request,
            "status": status,
            "artifacts": artifacts,
            "idea_id": idea_id,
            "agent_knowledge": agent_knowledge,
            "release_count": release_count,
            "is_running": is_running,
            "stop_requested": stop_requested,
        },
    )


@router.post("/ideas/{idea_id}/action")
async def idea_action(
    idea_id: str,
    action: str = Form(...),
    kill_reason: str = Form(""),
    resurrect_context: str = Form(""),
    refine_feedback: str = Form(""),
):
    settings = get_settings()
    bb = _get_blackboard()

    if action == "incubate":
        async def _run():
            from incubator.orchestrator.orchestrator import Orchestrator
            orch = Orchestrator(settings)
            await orch.run_continuous_for_idea(idea_id)

        asyncio.create_task(_run())
    elif action == "request_stop":
        from incubator.orchestrator.orchestrator import Orchestrator
        orch = Orchestrator(settings)
        orch.request_stop(idea_id)
    elif action == "kill":
        from incubator.orchestrator.orchestrator import Orchestrator
        orch = Orchestrator(settings)
        await orch.kill(idea_id)
        if kill_reason.strip():
            bb.update_status(idea_id, kill_reason=kill_reason.strip())
    elif action == "resume":
        async def _run():
            from incubator.orchestrator.orchestrator import Orchestrator
            orch = Orchestrator(settings)
            await orch.resume(idea_id)

        asyncio.create_task(_run())
    elif action == "refine":
        if refine_feedback.strip():
            bb.append_file(
                idea_id, "idea.md",
                f"\n\n---\n\n## Refinement Feedback\n\n{refine_feedback.strip()}\n",
            )

        async def _run():
            from incubator.orchestrator.orchestrator import Orchestrator
            orch = Orchestrator(settings)
            from incubator.core.phase import Phase
            # Loop back to ideation — agents detect refinement mode automatically
            await orch._transition(idea_id, Phase.IDEATION)
            await orch.run_continuous_for_idea(idea_id)

        asyncio.create_task(_run())
    elif action == "resurrect":
        from incubator.core.phase import Phase
        bb.set_phase(idea_id, Phase.SUBMITTED)
        bb.update_status(idea_id, kill_reason=None)
        if resurrect_context.strip():
            # Append context to idea.md so agents see it on next run
            bb.append_file(
                idea_id, "idea.md",
                f"\n\n---\n\n## Additional Context (Resurrected)\n\n{resurrect_context.strip()}\n",
            )

    return RedirectResponse(url=f"/ideas/{idea_id}", status_code=303)


@router.get("/ideas/{idea_id}/logs", response_class=HTMLResponse)
async def idea_agent_logs(request: Request, idea_id: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)
    log_dir = bb.idea_dir(idea_id) / "agent-logs"
    logs = []
    if log_dir.is_dir():
        for f in sorted(log_dir.iterdir(), reverse=True):
            if f.suffix == ".json":
                data = json.loads(f.read_text())
                logs.append({
                    "filename": f.name,
                    "agent": data.get("agent", "unknown"),
                    "timestamp": data.get("timestamp", ""),
                    "model": data.get("model", ""),
                    "transcript_len": len(data.get("transcript", [])),
                })
    return templates.TemplateResponse(
        "idea_logs.html",
        {"request": request, "status": status, "logs": logs, "idea_id": idea_id},
    )


@router.get("/ideas/{idea_id}/logs/{log_filename}", response_class=HTMLResponse)
async def idea_agent_log_detail(request: Request, idea_id: str, log_filename: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)
    log_file = bb.idea_dir(idea_id) / "agent-logs" / log_filename
    if not log_file.exists():
        return HTMLResponse("Log not found", status_code=404)
    log_data = json.loads(log_file.read_text())
    return templates.TemplateResponse(
        "idea_log_detail.html",
        {"request": request, "status": status, "log": log_data, "idea_id": idea_id, "log_filename": log_filename},
    )


@router.get("/api/ideas")
async def api_list_ideas():
    bb = _get_blackboard()
    return [bb.get_status(idea_id) for idea_id in bb.list_ideas()]


@router.get("/api/ideas/{idea_id}")
async def api_get_idea(idea_id: str):
    bb = _get_blackboard()
    return bb.get_status(idea_id)
