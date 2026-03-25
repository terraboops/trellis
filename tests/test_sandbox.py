"""Tests for trellis/core/sandbox.py — nono CLI flag building."""

from __future__ import annotations

from pathlib import Path


from trellis.core.registry import AgentConfig
from trellis.core.sandbox import build_nono_flags


def _flags(config: AgentConfig, tmp_path: Path) -> str:
    """Helper: build flags with a minimal valid directory structure."""
    bb = tmp_path / "blackboard" / "ideas"
    bb.mkdir(parents=True, exist_ok=True)
    return build_nono_flags(config, "test-idea", tmp_path, bb)


# ── build_nono_flags tests ──────────────────────────────────────────────


def test_build_nono_flags_defaults(tmp_path):
    config = AgentConfig(name="test", description="t")
    flags = _flags(config, tmp_path)
    assert "--allow-cwd" in flags
    assert f"--read {tmp_path}" in flags


def test_build_nono_flags_blackboard_path(tmp_path):
    config = AgentConfig(name="test", description="t")
    flags = _flags(config, tmp_path)
    assert f"--allow {tmp_path / 'blackboard' / 'ideas' / 'test-idea'}" in flags


def test_build_nono_flags_workspace(tmp_path):
    bb = tmp_path / "blackboard" / "ideas"
    bb.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    config = AgentConfig(name="test", description="t")
    flags = build_nono_flags(config, "test-idea", tmp_path, bb, workspace_dir=ws)
    assert f"--allow {ws / 'test-idea'}" in flags


def test_build_nono_flags_global_idea(tmp_path):
    bb = tmp_path / "blackboard" / "ideas"
    bb.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(name="test", description="t")
    flags = build_nono_flags(config, "__all__", tmp_path, bb)
    assert f"--read {bb}" in flags


def test_build_nono_flags_proxy_credentials(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_proxy_credentials=["anthropic", "github"],
    )
    flags = _flags(config, tmp_path)
    assert "--proxy-credential anthropic" in flags
    assert "--proxy-credential github" in flags


def test_build_nono_flags_allowed_hosts(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_allowed_hosts=["api.anthropic.com", "github.com"],
    )
    flags = _flags(config, tmp_path)
    assert "--proxy-allow api.anthropic.com" in flags
    assert "--proxy-allow github.com" in flags


def test_build_nono_flags_allowed_ports(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_allowed_ports=[3000, 8080],
    )
    flags = _flags(config, tmp_path)
    assert "--allow-port 3000" in flags
    assert "--allow-port 8080" in flags


def test_build_nono_flags_allowed_commands(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_allowed_commands=["rm", "docker"],
    )
    flags = _flags(config, tmp_path)
    assert "--allow-command rm" in flags
    assert "--allow-command docker" in flags


def test_build_nono_flags_credential_maps(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_credential_maps=["op://Dev/OpenAI/key -> OPENAI_API_KEY"],
    )
    flags = _flags(config, tmp_path)
    assert "--env-credential-map" in flags


def test_build_nono_flags_extra_paths(tmp_path):
    extra_read = tmp_path / "extra-read"
    extra_write = tmp_path / "extra-write"
    extra_read.mkdir()
    extra_write.mkdir()
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_extra_read_paths=[str(extra_read)],
        sandbox_extra_write_paths=[str(extra_write)],
    )
    flags = _flags(config, tmp_path)
    assert f"--read {extra_read}" in flags
    assert f"--allow {extra_write}" in flags


def test_build_nono_flags_extra_paths_skips_missing(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_extra_read_paths=["/nonexistent/path"],
    )
    flags = _flags(config, tmp_path)
    assert "/nonexistent/path" not in flags


def test_build_nono_flags_empty_config(tmp_path):
    config = AgentConfig(
        name="test",
        description="t",
        sandbox_proxy_credentials=[],
        sandbox_allowed_hosts=[],
        sandbox_allowed_ports=[],
        sandbox_allowed_commands=[],
    )
    flags = _flags(config, tmp_path)
    assert "--allow-cwd" in flags
    assert "--proxy-credential" not in flags
