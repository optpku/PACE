from __future__ import annotations

import os
import re
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from pace.runtime.output.workspace import work_ref, path_has_symlink_component

_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_PACKAGED_RESOURCES = Path(__file__).resolve().parents[1] / "resources"

# Source layout is independent of persisted run references. Keep the snapshot
# paths (and their Markdown content) unchanged so existing checkpoints resume.
_SNAPSHOT_PATHS = {
    "prompts/transport.md": "prompts/shared/transport.md",
    **{
        f"skills/rci-investigation/references/critic/{name}.md":
        f"skills/rci-investigation/references/critic/raw_seed/{name}.md"
        for name in (
            "attack_correctness", "attack_counterexamples", "attack_novelty_value",
            "attack_readiness_for_next_stage",
        )
    },
}


def prompt_root(workdir: Path) -> Path:
    """Return the prompt snapshot owned by this run."""

    return Path(workdir).resolve() / work_ref(workdir, 'resources') / "prompts"


def skill_index_ref(workdir: Path) -> str | None:
    """Locate the shared catalog; older saved snapshots have no catalog."""

    ref = work_ref(workdir, "resources/skills/README.md")
    return ref if (Path(workdir) / ref).is_file() else None


def append_skill_catalog(prompt: str, *, workdir: Path) -> str:
    """Expose method discovery without selecting methods or reading run evidence."""

    ref = skill_index_ref(workdir)
    if ref is None:
        return prompt
    catalog = (Path(workdir) / ref).read_text(encoding="utf-8").strip()
    return (
        prompt.rstrip() + f"\n\n## Available methods\n\nIndex: `{ref}`. "
        "Resolve its relative links from the index directory. Read the selected "
        "SKILL.md and relevant supporting files before applying a method.\n\n" + catalog
    )


def materialize_run_resources(
    workdir: Path, *, resource_variant: str, destination: Path | None = None,
) -> None:
    """Create one complete resource snapshot before any model call."""

    if resource_variant not in {"standard", "weak"}:
        raise ValueError(f"unknown resource variant: {resource_variant}")
    external = os.environ.get("PACE_REASATLAS_SKILL_DIR", "").strip()
    atlas = Path(external).expanduser().resolve() if external else None
    if atlas is not None:
        for relative in ("SKILL.md", "scripts/query.py"):
            if not (atlas / relative).is_file():
                raise ValueError(f"PACE_REASATLAS_SKILL_DIR is missing {relative}: {atlas}")
        if any(path.is_symlink() for path in atlas.rglob("*")):
            raise ValueError("PACE_REASATLAS_SKILL_DIR must contain regular files, not symlinks")
    destination = destination or (Path(workdir).resolve() / work_ref(workdir, 'resources'))
    if destination.exists():
        raise FileExistsError(f"resource snapshot already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    selected = _PACKAGED_RESOURCES / "variants" / resource_variant
    sources = [
        (_PACKAGED_RESOURCES / "prompts", destination / "prompts"),
        (selected, destination / "prompts" / "roles"),
        (_PACKAGED_RESOURCES / "skills", destination / "skills"),
        (_PACKAGED_RESOURCES / "templates", destination / "templates"),
    ]
    if atlas is not None:
        sources.append((atlas, destination / "skills" / "reasatlas-query"))
    try:
        for source, target in sources:
            for path in sorted(source.rglob("*")):
                if not path.is_file() or path.suffix not in {".md", ".json", ".py", ".yaml", ".sty"}:
                    continue
                relative = (target.relative_to(destination) / path.relative_to(source)).as_posix()
                output = temporary / _SNAPSHOT_PATHS.get(relative, relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(path.read_bytes())
        manifest = {"schema_version": "resource_snapshot/v1", "resource_variant": resource_variant,
                    "skill_layout": "shared/v1", "files": sorted(
            path.relative_to(temporary).as_posix() for path in temporary.rglob("*") if path.is_file()
        )}
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def validate_resource_snapshot(destination: Path, *, resource_variant: str) -> None:
    """A new initialization reuses only a complete, explicitly committed snapshot."""
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    required = {"prompts/roles/problem_extraction.md", "prompts/roles/research_decision.md",
                "prompts/roles/global_research_director.md", "templates/neurips2026/neurips_2026.sty",
                "skills/direct-proving-and-gap-closure/SKILL.md"}
    if manifest.get("skill_layout") == "shared/v1":
        required.update({"skills/README.md", "skills/rci-investigation/SKILL.md",
                         "skills/rci-investigation/references/rci_search_core.md"})
    if (manifest.get("schema_version") != "resource_snapshot/v1"
            or manifest.get("resource_variant") != resource_variant
            or not isinstance(manifest.get("files"), list)
            or not required.issubset(manifest["files"])):
        raise ValueError("resource snapshot manifest differs from the saved policy")
    for ref in manifest["files"]:
        path = Path(ref) if isinstance(ref, str) else Path("..")
        if (path.is_absolute() or ".." in path.parts
                or path_has_symlink_component(destination, destination / path)
                or not (destination / path).is_file()):
            raise ValueError(f"resource snapshot is incomplete: {ref}")


def load_prompt_template(name: str, *, workdir: Path) -> str:
    """Load one Markdown template from the committed run snapshot."""

    path = PurePosixPath(str(name or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("prompt template name must be a relative resource path")
    resource = prompt_root(workdir).joinpath(*path.parts)
    if not resource.is_file():
        raise FileNotFoundError(f"prompt template is missing: {path.as_posix()}")
    return resource.read_text(encoding="utf-8")


def render_prompt_template(name: str, *, workdir: Path, **values: object) -> str:
    """Render explicit ``{{token}}`` placeholders without format semantics."""

    template = load_prompt_template(name, workdir=workdir)
    required = set(_TOKEN.findall(template))
    missing = sorted(required - set(values))
    extra = sorted(set(values) - required)
    if missing or extra:
        raise ValueError(
            f"prompt template bindings differ for {name}: "
            f"missing={missing}, extra={extra}"
        )
    return _TOKEN.sub(lambda match: str(values[match.group(1)]), template)
