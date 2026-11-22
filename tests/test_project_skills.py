import json
from pathlib import Path

import pytest

from pace.runtime.codex_config import isolated_codex_args
from pace.runtime.prompts import materialize_run_resources, prompt_root, project_skill_paths
from pace.artifacts.workspace import RunWorkspace
from pace.agents import prover, research, report_writer, paper_writer, framer, kernel


def write_skill(root: Path, name: str) -> Path:
    entry = root / name / "SKILL.md"
    entry.parent.mkdir(parents=True)
    entry.write_text("Read [supporting notes](references/notes.md).", encoding="utf-8")
    return entry


def test_project_catalog_exposes_both_skill_roots_to_prover_and_writer(tmp_path, monkeypatch):
    project = tmp_path / "current project"
    run = RunWorkspace.create(project / "runtime", "run").root
    materialize_run_resources(run, resource_variant="standard")
    local = write_skill(project / ".agents/skills", "proof guide")
    report = write_skill(project / "skills", "report guide")
    notes = local.parent / "references/notes.md"
    notes.parent.mkdir()
    notes.write_text("supporting notes", encoding="utf-8")
    write_skill(tmp_path / ".agents/skills", "ancestor")
    write_skill(tmp_path / "another project/skills", "other project")
    personal = tmp_path / "personal"
    write_skill(personal / ".agents/skills", "personal")
    monkeypatch.setattr(Path, "home", lambda: personal)

    assert not project_skill_paths(run)[1]
    monkeypatch.setenv("PACE_PROJECT_SKILLS_ROOT", str(project))
    for module, kwargs in ((prover, {}), (research, {}), (report_writer, {"outline": False}), (paper_writer, {"outline": False})):
        instruction = module.build_instructions(workdir=run, resources=prompt_root(run), **kwargs)
        paths = json.loads(instruction.split("Project Skill paths: ")[1])
        assert [Path(path) for path in paths] == [local, report]
        assert "from its own directory" in instruction
    for module in (framer, kernel):
        assert "Project Skill paths:" not in module.build_instructions(workdir=run, resources=prompt_root(run))
    assert not project_skill_paths(tmp_path / "another project")[1]
    settings = next(arg for arg in isolated_codex_args(run) if arg.startswith("skills.config="))
    assert "{path=" + json.dumps(str(local)) + ",enabled=false}" in settings


def test_project_catalog_ignores_symlinked_roots_and_entries(tmp_path, monkeypatch):
    project = tmp_path / "project"
    run = project / "run"
    run.mkdir(parents=True)
    outside = write_skill(tmp_path / "outside", "foreign")
    entry = write_skill(project / ".agents/skills", "owned")
    symlinked_file = entry.parent.parent / "symlinked_file"
    symlinked_file.mkdir()
    try:
        (project / "skills").symlink_to(outside.parent.parent, target_is_directory=True)
        (entry.parent.parent / "symlinked_dir").symlink_to(outside.parent, target_is_directory=True)
        (symlinked_file / "SKILL.md").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    monkeypatch.setenv("PACE_PROJECT_SKILLS_ROOT", str(project))
    assert project_skill_paths(run)[1] == [
        entry.as_posix(),
    ]
