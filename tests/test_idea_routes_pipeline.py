"""Tests for pipeline-related idea routes: presets, pipeline editor."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# We test _load_presets as a unit — it reads from disk, no FastAPI needed.


def test_load_presets_returns_dict_from_file(tmp_path: Path):
    """_load_presets reads pool/presets.json and returns parsed dict."""
    from incubator.web.api.routes.ideas import _load_presets

    presets_data = {
        "full-pipeline": {
            "label": "Full Pipeline",
            "description": "All stages",
            "stages": ["ideation", "implementation", "validation", "release"],
            "post_ready": ["competitive", "research"],
            "gating": {"default": "auto", "overrides": {}},
        }
    }
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "presets.json").write_text(json.dumps(presets_data))

    mock_settings = MagicMock()
    mock_settings.project_root = tmp_path

    with patch("incubator.web.api.routes.ideas.get_settings", return_value=mock_settings):
        result = _load_presets()

    assert result == presets_data
    assert "full-pipeline" in result
    assert result["full-pipeline"]["stages"] == ["ideation", "implementation", "validation", "release"]


def test_load_presets_returns_empty_dict_when_missing(tmp_path: Path):
    """_load_presets returns {} when presets.json doesn't exist."""
    from incubator.web.api.routes.ideas import _load_presets

    mock_settings = MagicMock()
    mock_settings.project_root = tmp_path  # no pool/ dir

    with patch("incubator.web.api.routes.ideas.get_settings", return_value=mock_settings):
        result = _load_presets()

    assert result == {}


def test_load_presets_returns_empty_dict_when_pool_dir_exists_but_no_file(tmp_path: Path):
    """_load_presets returns {} when pool/ exists but presets.json doesn't."""
    from incubator.web.api.routes.ideas import _load_presets

    (tmp_path / "pool").mkdir()

    mock_settings = MagicMock()
    mock_settings.project_root = tmp_path

    with patch("incubator.web.api.routes.ideas.get_settings", return_value=mock_settings):
        result = _load_presets()

    assert result == {}


def test_get_registered_roles():
    """_get_registered_roles returns set of agent names from registry."""
    from incubator.web.api.routes.ideas import _get_registered_roles
    from incubator.core.registry import Registry, AgentConfig

    registry = Registry(agents={
        "ideation": AgentConfig(name="ideation", description="test"),
        "validation": AgentConfig(name="validation", description="test"),
    })

    mock_settings = MagicMock()
    mock_settings.registry_path = Path("/fake/registry.yaml")

    with patch("incubator.web.api.routes.ideas.get_settings", return_value=mock_settings), \
         patch("incubator.web.api.routes.ideas.load_registry", return_value=registry):
        result = _get_registered_roles()

    assert result == {"ideation", "validation"}
