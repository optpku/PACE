"""Deterministic assembly of current tasks, separate from role AGENTS.md."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pace.artifacts.workspace import work_ref
from pace.runtime.prompts import prompt_root, render_prompt_template

PROMPT_ASSEMBLY_SCHEMA_VERSION = "prompt_assembly/v3"


@dataclass(frozen=True)
class PromptAssembly:
    stage: str
    role: str
    mode: str
    target_contract_id: str
    candidate_refs: tuple[str, ...] = ()
    required_refs: tuple[str, ...] = ()
    contract_guide_refs: tuple[str, ...] = ()
    artifact_guide_refs: tuple[str, ...] = ()
    prompt_seed: str = ""
    output_artifact_ref: str = ""
    # Opaque per-call context budget. The Composer may use it to trim prompt
    # context, but it cannot reinterpret loop limits or mathematical state.
    budget: Any = None
    # Transport provenance only; this is the manifest written for this call.
    manifest_ref: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PROMPT_ASSEMBLY_SCHEMA_VERSION,
            "stage": self.stage,
            "role": self.role,
            "mode": self.mode,
            "target_contract_id": self.target_contract_id,
            "candidate_refs": list(self.candidate_refs),
            "required_refs": list(self.required_refs),
            "contract_guide_refs": list(self.contract_guide_refs),
            "artifact_guide_refs": list(self.artifact_guide_refs),
            "prompt_seed": self.prompt_seed,
            "output_artifact_ref": self.output_artifact_ref,
            "budget": self.budget,
            "manifest_ref": self.manifest_ref,
        }


def build_prompt_assembly(
    *,
    workdir: Path,
    stage: str,
    role: str,
    mode: str,
    target_contract_id: str,
    manifest_path: Path | None = None,
    candidate_file_refs: Iterable[str] = (),
    required_file_refs: Iterable[str] = (),
    contract_guide_refs: Iterable[str] = (),
    artifact_guide_refs: Iterable[str] = (),
    prompt_seed: str = "",
    output_artifact_ref: str = "",
    budget: Any = None,
) -> PromptAssembly:
    """Prepare Composer refs without inspecting mathematical files."""

    root = Path(workdir)
    resource_root = prompt_root(root)
    # Caller-owned references are reading hints. Omit stale optional paths
    # without inspecting their mathematical contents or building a registry.
    refs = _existing_refs(root, candidate_file_refs)
    required_refs = _existing_refs(root, required_file_refs)
    contract_refs = _existing_refs(root, contract_guide_refs)
    artifact_refs_value = _existing_refs(root, artifact_guide_refs)
    transport_ref = str(resource_root / "shared" / "transport.md")
    if not _is_file_ref(root, transport_ref):
        raise FileNotFoundError(f"run prompt resource is missing: {transport_ref}")
    refs = tuple(dict.fromkeys((*refs, transport_ref)))
    required_refs = _existing_refs(
        root,
        tuple(
            dict.fromkeys(
                (
                    *required_refs,
                    *contract_refs,
                    *artifact_refs_value,
                )
            )
        ),
    )
    # Short guidance files are ordinary reading suggestions, with no registry
    # or profile state to validate during execution or resume.
    guidance_refs = tuple(
        str(path) for path in sorted((resource_root / "shared/guides").glob("*.md"))
    )
    candidate_refs = _existing_refs(
        root,
        tuple(
            dict.fromkeys(
                (
                    *refs,
                    *required_refs,
                    *contract_refs,
                    *artifact_refs_value,
                    *guidance_refs,
                )
            )
        ),
    )
    assembly = PromptAssembly(
        stage=str(stage or "").upper(),
        role=str(role or "").upper(),
        mode=str(mode or "").upper(),
        target_contract_id=str(target_contract_id or ""),
        candidate_refs=candidate_refs,
        required_refs=required_refs,
        contract_guide_refs=contract_refs,
        artifact_guide_refs=artifact_refs_value,
        prompt_seed=str(prompt_seed or ""),
        output_artifact_ref=str(output_artifact_ref or ""),
        budget=budget,
        manifest_ref=_manifest_ref(manifest_path, root),
    )
    if manifest_path is not None:
        path = Path(manifest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, assembly.to_json())
    return assembly


def load_prompt_assembly(path: Path) -> PromptAssembly:
    """Restore the committed file collection alongside its saved role prompt."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return PromptAssembly(
        stage=raw["stage"],
        role=raw["role"],
        mode=raw["mode"],
        target_contract_id=raw["target_contract_id"],
        candidate_refs=tuple(raw["candidate_refs"]),
        required_refs=tuple(raw["required_refs"]),
        contract_guide_refs=tuple(raw["contract_guide_refs"]),
        artifact_guide_refs=tuple(raw["artifact_guide_refs"]),
        prompt_seed=raw["prompt_seed"],
        output_artifact_ref=raw["output_artifact_ref"],
        budget=raw["budget"],
        manifest_ref=raw["manifest_ref"],
    )


def _canonical_ref(value: str, root: Path) -> str:
    raw = str(value or "").strip()
    prefix = work_ref(root, "resources/prompts/")
    if raw.startswith(prefix):
        return str(prompt_root(root) / raw[len(prefix) :])
    return raw


def _is_file_ref(root: Path, value: str) -> bool:
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    return resolved.is_file()


def _existing_refs(root: Path, values: Iterable[str]) -> tuple[str, ...]:
    refs = (
        _canonical_ref(str(value).strip(), root)
        for value in values
        if str(value).strip()
    )
    return tuple(dict.fromkeys(ref for ref in refs if _is_file_ref(root, ref)))


def _manifest_ref(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    target = Path(path)
    try:
        return str(target.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(target)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class PromptComposerError(ValueError):
    """A Composer transport or output failure owned by the Host interface."""

    failure_owner = "HOST"
    reason_code = "prompt_composer_interface_failed"

    def __init__(
        self, message: str, *, diagnostics: Iterable[dict[str, Any]] = ()
    ) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclass(frozen=True)
class PromptComposerResult:
    final_prompt: str
    call_id: str


def compose_prompt(
    *,
    workdir: Path,
    assembly: PromptAssembly,
    input_refs: Iterable[str],
    resource_root: Path | None = None,
) -> PromptComposerResult:
    """Render a fixed role task without a model call."""
    from pace.runtime.prompts import is_prompt_resource

    root = Path(workdir).resolve()
    inputs = []
    for ref in dict.fromkeys(
        (*assembly.required_refs, *input_refs, *assembly.candidate_refs)
    ):
        path = (root / ref).resolve()
        if not path.is_file():
            if ref in assembly.required_refs:
                raise PromptComposerError(f"required task input is missing: {ref}")
            continue
        if not is_prompt_resource(path, workdir=root):
            inputs.append(ref)
    prompt = render_prompt_template(
        "shared/task.md",
        workdir=workdir,
        resource_root=resource_root,
        target_contract_id=assembly.target_contract_id,
        task=assembly.prompt_seed,
        input_refs="\n".join(f"- `{ref}`" for ref in inputs),
        output_ref=assembly.output_artifact_ref,
        budget=_format_budget(assembly.budget),
    )
    return PromptComposerResult(final_prompt=prompt, call_id="")


def _format_budget(value: Any) -> str:
    if value is None:
        return "(none)"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "PromptComposerError",
    "PromptComposerResult",
    "compose_prompt",
]


def render_role_task(name: str, *, workdir: Path, **values: object) -> str:
    """Keep current template bindings in the task; Codex loads fixed AGENTS.md."""
    render_prompt_template(name, workdir=workdir, **values)
    return "\n\n".join(
        f"## {name}\n{value}" for name, value in values.items() if name != "turn_dir"
    )


def delivery_instruction(
    root: Path, output_ref: str, *, with_delta: bool = False
) -> str:
    from pace.agents.director.context import task_context
    from pace.contracts.research.memory import reporter_owned

    context = task_context(root, output_ref)
    if not context:
        return ""
    folder = Path(output_ref).parent
    ids = context["obligation_ids"]
    delta = {
        "schema_version": "research_delta/v1",
        "obligation_ids": ids,
        "branch_id": context["branch_id"],
        "change_ref": output_ref,
        "applications": [],
        "attempt": {
            "outcome": "NOT_ESTABLISHED",
            "obstacle_ref": "",
            "reopen_condition_ref": output_ref,
        },
    }
    common = render_prompt_template("shared/delivery.md", workdir=root)
    if not with_delta:
        return common
    if reporter_owned(root):
        return render_prompt_template(
            "shared/reporter_delivery.md", workdir=root, common=common
        )
    return render_prompt_template(
        "shared/delta_delivery.md",
        workdir=root,
        common=common,
        folder=folder,
        envelope=json.dumps(delta),
    )


def compose_instructions(
    *,
    workdir: Path,
    role: str,
    resources: Path,
    refs: Iterable[str],
    project_skills: bool = False,
) -> str:
    """Join the fixed fragments explicitly selected by a role module."""
    import re

    fragments = []
    for ref in refs:
        body = (resources / ref).read_text(encoding="utf-8")
        body = re.sub(
            r"\{\{([a-z][a-z0-9_]*)\}\}",
            lambda match: (
                work_ref(workdir) if match[1] == "turn_dir" else f"TASK.{match[1]}"
            ),
            body,
        )
        fragments.append(f"<!-- Source: {ref} -->\n{body.strip()}")
    instructions = render_prompt_template(
        "shared/instructions.md",
        workdir=workdir,
        resource_root=resources,
        role=role,
        problem_directory=Path(workdir).resolve(),
        turn_directory=work_ref(workdir),
        body="\n\n".join(fragments),
    )
    if project_skills:
        from pace.runtime.prompts import project_skill_paths

        project, paths = project_skill_paths(Path(workdir).resolve())
        if paths:
            instructions += render_prompt_template(
                "shared/project_skills.md",
                workdir=workdir,
                resource_root=resources,
                project_directory=json.dumps(project.as_posix(), ensure_ascii=False),
                skill_paths=json.dumps(paths, ensure_ascii=False),
            )
    return instructions


def specialist_instructions(
    *,
    workdir: Path,
    role: str,
    resources: Path,
    role_ref: str,
    extra_refs: tuple[str, ...] = (),
    guides: bool = True,
    shared: bool = True,
    project_skills: bool = True,
    mode_ref: str = "shared/specialist.md",
) -> str:
    refs = [role_ref]
    if guides:
        refs.extend(
            "shared/guides/" + path.name
            for path in sorted((resources / "shared/guides").glob("*.md"))
        )
    if shared:
        refs.extend(
            (
                "shared/transport.md",
                mode_ref,
                "shared/output/files.md",
                "shared/output/findings.md",
                "shared/output/memory.md",
            )
        )
    return compose_instructions(
        workdir=workdir,
        role=role,
        resources=resources,
        refs=(*refs, *extra_refs),
        project_skills=project_skills,
    )


def build_specialist_assembly(
    *,
    workdir: Path,
    decision: Any,
    target_contract_id: str,
    output_ref: str,
    manifest_ref: str,
    role_ref: str,
    mode: str = "SPECIALIST",
    mode_ref: str = "shared/specialist.md",
    extra_refs: tuple[str, ...] = (),
    extra_task: str = "",
    extra_inputs: tuple[str, ...] = (),
    delivery_task: str = "",
    separate_reporter: bool = False,
) -> PromptAssembly:
    """Freeze common task structure using the policy supplied by its owning role."""
    from pace.agents.director.context import task_context
    from pace.artifacts.results import FINDINGS_INDEX_REF
    from pace.runtime.prompts import skill_index_ref

    frozen = task_context(workdir, output_ref)
    inputs = (*decision.input_refs, *extra_inputs)
    catalog = skill_index_ref(workdir)
    methods = (catalog,) if catalog else ()
    refs = tuple(
        work_ref(workdir, "resources/prompts/" + ref)
        for ref in (
            role_ref,
            mode_ref,
            "shared/transport.md",
            "shared/output/files.md",
            "shared/output/findings.md",
            *extra_refs,
        )
    )
    return build_prompt_assembly(
        workdir=workdir,
        stage="DIRECTOR_LED_SPECIALIST",
        role="DIRECTOR_LED_" + decision.agent_id.upper(),
        mode=mode,
        target_contract_id=target_contract_id,
        candidate_file_refs=(
            *inputs,
            *methods,
            *refs,
            *(
                (FINDINGS_INDEX_REF,)
                if not frozen and (workdir / FINDINGS_INDEX_REF).is_file()
                else ()
            ),
        ),
        required_file_refs=(*inputs, *methods, *refs),
        prompt_seed=render_prompt_template(
            "shared/specialist_task.md",
            workdir=workdir,
            objective=decision.prompt_brief,
            output_ref=output_ref,
            revision_task=extra_task,
            dependency_task="",
            delivery=delivery_task,
            archive=(
                render_prompt_template(
                    "shared/archive_task.md",
                    workdir=workdir,
                    archive_refs=json.dumps(frozen["archive_refs"]),
                )
                if frozen
                else ""
            ),
            findings_directory=Path(output_ref).parent / "findings",
            memory_task=(
                ""
                if separate_reporter
                else render_prompt_template(
                    "shared/memory_task.md",
                    workdir=workdir,
                    memory_ref=Path(output_ref).parent / "memory_note.md",
                )
            ),
        ),
        output_artifact_ref=output_ref,
        manifest_path=(workdir / manifest_ref).with_name("composer_request.json"),
    )


def append_skill_catalog(prompt: str, *, workdir: Path) -> str:
    """Expose method discovery without selecting methods or reading run evidence."""

    from pace.runtime.prompts import skill_index_ref

    ref = skill_index_ref(workdir)
    if ref is None:
        return prompt
    catalog = (Path(workdir) / ref).read_text(encoding="utf-8").strip()
    return render_prompt_template(
        "shared/skill_catalog.md",
        workdir=workdir,
        prompt=prompt.rstrip(),
        index_ref=ref,
        catalog=catalog,
    )
