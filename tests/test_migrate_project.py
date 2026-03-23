import json
from pathlib import Path
from typer.testing import CliRunner
from trellis.cli import app

runner = CliRunner()


def _scaffold_incubator_project(path: Path) -> None:
    """Create a minimal incubator-era project structure."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".incubator").write_text(json.dumps({"version": "0.2.0"}))
    (path / "pool").mkdir(exist_ok=True)
    (path / "pool" / "incubator.log").write_text("old log\n")
    (path / "registry.yaml").write_text(
        "agents:\n- name: ideation\n  claude_home: incubator/agents/ideation/.claude\n"
    )


def test_migrate_renames_marker(tmp_path):
    proj = tmp_path / "proj"
    _scaffold_incubator_project(proj)
    result = runner.invoke(app, ["migrate-project", str(proj)])
    assert result.exit_code == 0
    assert (proj / ".trellis").exists()
    assert not (proj / ".incubator").exists()


def test_migrate_renames_pool_files(tmp_path):
    proj = tmp_path / "proj"
    _scaffold_incubator_project(proj)
    (proj / "pool" / "incubator.pid").write_text("12345")
    runner.invoke(app, ["migrate-project", str(proj)])
    assert (proj / "pool" / "trellis.log").exists()
    assert (proj / "pool" / "trellis.pid").exists()
    assert not (proj / "pool" / "incubator.log").exists()
    assert not (proj / "pool" / "incubator.pid").exists()


def test_migrate_updates_registry(tmp_path):
    proj = tmp_path / "proj"
    _scaffold_incubator_project(proj)
    runner.invoke(app, ["migrate-project", str(proj)])
    text = (proj / "registry.yaml").read_text()
    assert "trellis/agents" in text
    assert "incubator/agents" not in text


def test_migrate_dry_run(tmp_path):
    proj = tmp_path / "proj"
    _scaffold_incubator_project(proj)
    result = runner.invoke(app, ["migrate-project", str(proj), "--dry-run"])
    assert result.exit_code == 0
    assert (proj / ".incubator").exists()
    assert not (proj / ".trellis").exists()


def test_migrate_already_trellis(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".trellis").write_text(json.dumps({"version": "0.2.0"}))
    result = runner.invoke(app, ["migrate-project", str(proj)])
    assert result.exit_code == 0
    assert "Already a Trellis project" in result.output


def test_migrate_no_marker(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    result = runner.invoke(app, ["migrate-project", str(proj)])
    assert result.exit_code == 1
    assert "No .incubator marker" in result.output
