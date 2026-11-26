from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from pace.artifacts.workspace import path_has_symlink_component, work_ref

_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_PACKAGED_RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def prompt_root(workdir: Path) -> Path:
    """Return the prompt snapshot owned by this run."""

    return Path(workdir).resolve() / work_ref(workdir, "resources") / "prompts"


def standard_workflow(workdir: Path, *, turn_id: str | None = None) -> bool:
    """Select the complete protocol frozen by the owning turn."""
    path = Path(workdir) / work_ref(workdir, "resources/manifest.json", turn_id=turn_id)
    if not path.is_file():
        return False
    return _standard_manifest(json.loads(path.read_text(encoding="utf-8")))


def _standard_manifest(manifest: dict) -> bool:
    if not isinstance(manifest, dict):
        raise ValueError("resource snapshot manifest must be a JSON object")
    variant = manifest.get("resource_variant")
    standard = variant == "standard"
    if (
        variant not in {"standard", "weak"}
        or manifest.get("verification_policy")
        != ("on_demand/v1" if standard else "detailed/v1")
        or manifest.get("workflow_profile") != ("compact/v1" if standard else None)
        or manifest.get("kernel_receipt") != ("concise/v1" if standard else None)
    ):
        raise ValueError(
            "unsupported resource snapshot protocol; start a new turn with current resources"
        )
    return standard


def skill_index_ref(workdir: Path) -> str | None:
    """Locate the current turn's shared method catalog."""

    ref = work_ref(workdir, "resources/skills/README.md")
    return ref if (Path(workdir) / ref).is_file() else None


def project_skill_paths(workdir: Path) -> tuple[Path | None, list[str]]:
    """List project-owned methods without changing Codex's Skill discovery."""
    configured_root = os.environ.get("PACE_PROJECT_SKILLS_ROOT")
    if not configured_root:
        return None, []
    project = Path(configured_root)
    if not project.is_absolute():
        return None, []
    project = project.resolve()
    if not workdir.is_relative_to(project):
        return None, []

    entries = sorted(
        entry.as_posix()
        for directory in (project / ".agents/skills", project / "skills")
        if directory.is_dir() and directory.resolve() == directory
        for entry in directory.rglob("SKILL.md")
        if entry.is_file() and entry.resolve() == entry
    )
    if not entries:
        return None, []
    return project, entries


def materialize_run_resources(
    workdir: Path,
    *,
    resource_variant: str,
    destination: Path | None = None,
) -> None:
    """Create one complete resource snapshot before any model call."""

    if resource_variant not in {"standard", "weak"}:
        raise ValueError(f"unknown resource variant: {resource_variant}")
    external = os.environ.get("PACE_REASATLAS_SKILL_DIR", "").strip()
    atlas = Path(external).expanduser().resolve() if external else None
    if atlas is not None:
        for relative in ("SKILL.md", "scripts/query.py"):
            if not (atlas / relative).is_file():
                raise ValueError(
                    f"PACE_REASATLAS_SKILL_DIR is missing {relative}: {atlas}"
                )
        if any(path.is_symlink() for path in atlas.rglob("*")):
            raise ValueError(
                "PACE_REASATLAS_SKILL_DIR must contain regular files, not symlinks"
            )
    destination = destination or (
        Path(workdir).resolve() / work_ref(workdir, "resources")
    )
    if destination.exists():
        raise FileExistsError(f"resource snapshot already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    sources = [
        (_PACKAGED_RESOURCES / "prompts", destination / "prompts"),
        (_PACKAGED_RESOURCES / "skills", destination / "skills"),
        (_PACKAGED_RESOURCES / "templates", destination / "templates"),
    ]
    if atlas is not None:
        sources.append((atlas, destination / "skills" / "reasatlas-query"))
    try:
        for source, target in sources:
            for path in sorted(source.rglob("*")):
                if not path.is_file() or path.suffix not in {
                    ".md",
                    ".json",
                    ".py",
                    ".yaml",
                    ".sty",
                }:
                    continue
                relative = path.relative_to(source)
                if source == _PACKAGED_RESOURCES / "prompts" and path.name.endswith(
                    (".standard.md", ".weak.md")
                ):
                    if not path.name.endswith(f".{resource_variant}.md"):
                        continue
                    relative = relative.with_name(
                        path.name.removesuffix(f".{resource_variant}.md") + ".md"
                    )
                output = temporary / target.relative_to(destination) / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(path.read_bytes())
        manifest = {
            "schema_version": "resource_snapshot/v1",
            "resource_variant": resource_variant,
            "files": sorted(
                path.relative_to(temporary).as_posix()
                for path in temporary.rglob("*")
                if path.is_file()
            ),
        }
        manifest["verification_policy"] = (
            "on_demand/v1" if resource_variant == "standard" else "detailed/v1"
        )
        if resource_variant == "standard":
            manifest["workflow_profile"] = "compact/v1"
            manifest["kernel_receipt"] = "concise/v1"
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def validate_resource_snapshot(destination: Path, *, resource_variant: str) -> None:
    """A new initialization reuses only a complete, explicitly committed snapshot."""
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    required = {
        "prompts/orientation/extraction.md",
        "prompts/orientation/assessment.md",
        "prompts/director/role.md",
        "prompts/director/calling/planner.md",
        "prompts/kernel/role.md",
        "prompts/kernel/receipt_correction.md",
        "prompts/kernel/publication.md",
        "templates/neurips2026/neurips_2026.sty",
        "skills/direct-proving-and-gap-closure/SKILL.md",
    }
    _standard_manifest(manifest)
    required.update(
        {
            "skills/README.md",
            "skills/rci-investigation/SKILL.md",
            "skills/rci-investigation/references/rci_search_core.md",
        }
    )
    if (
        manifest.get("schema_version") != "resource_snapshot/v1"
        or manifest.get("resource_variant") != resource_variant
        or not isinstance(manifest.get("files"), list)
        or not required.issubset(manifest["files"])
    ):
        raise ValueError("resource snapshot manifest differs from the saved policy")
    for ref in manifest["files"]:
        path = Path(ref) if isinstance(ref, str) else Path("..")
        if (
            path.is_absolute()
            or ".." in path.parts
            or path_has_symlink_component(destination, destination / path)
            or not (destination / path).is_file()
        ):
            raise ValueError(f"resource snapshot is incomplete: {ref}")


def load_prompt_template(
    name: str, *, workdir: Path, resource_root: Path | None = None
) -> str:
    """Load one Markdown template from the committed run snapshot."""

    path = PurePosixPath(str(name or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("prompt template name must be a relative resource path")
    resource = (resource_root or prompt_root(workdir)).joinpath(*path.parts)
    if not resource.is_file():
        raise FileNotFoundError(f"prompt template is missing: {path.as_posix()}")
    return resource.read_text(encoding="utf-8")


def render_prompt_template(
    name: str, *, workdir: Path, resource_root: Path | None = None, **values: object
) -> str:
    """Render explicit ``{{token}}`` placeholders without format semantics."""

    template = load_prompt_template(name, workdir=workdir, resource_root=resource_root)
    required = set(_TOKEN.findall(template))
    missing = sorted(required - set(values))
    extra = sorted(set(values) - required)
    if missing or extra:
        raise ValueError(
            f"prompt template bindings differ for {name}: "
            f"missing={missing}, extra={extra}"
        )
    return _TOKEN.sub(lambda match: str(values[match.group(1)]), template)


def is_prompt_resource(path: Path, *, workdir: Path) -> bool:
    """Include writing-task resource snapshots as well as the turn snapshot."""
    root = Path(workdir).resolve()
    path = path.resolve()
    if not path.is_relative_to(root):
        return False
    parts = path.relative_to(root).parts
    return any(
        parts[index : index + 2] == ("resources", "prompts")
        for index in range(len(parts) - 1)
    )
