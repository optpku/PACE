"""References for deterministic assembly of role instructions.

The Host selects stable references and records provenance metadata.  The
Host combines the assigned instructions and file references without inspecting
or summarizing mathematical JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.runtime.output.workspace import work_ref, _write_json_atomic
from pace.runtime.prompts import prompt_root

PROMPT_ASSEMBLY_SCHEMA_VERSION = "prompt_assembly/v3"

# These are the small, stable instructions that every production assembly
# exposes to the mathematical role. They are file references, not Python
# representations of a proof contract or artifact schema.
_ROLE_PROMPT_REFS = {
    "DIRECTOR_LED_FRAMER": "roles/research_framing.md",
    "DIRECTOR_LED_PLANNER": "roles/director_led_planner.md",
    "DIRECTOR_LED_SURVEY": "roles/director_led_survey.md",
    "DIRECTOR_LED_RESEARCH": "roles/director_led_research.md",
    "DIRECTOR_LED_PROVER": "roles/director_led_prover.md",
}


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
    normalized_role = str(role or "").upper()
    normalized_mode = str(mode or "").upper()
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
    essential_refs = tuple(
        _canonical_ref(str(resource_root / relative), root)
        for relative in (
            _ROLE_PROMPT_REFS.get(normalized_role, ""),
            f"modes/{normalized_mode}.md" if normalized_mode else "",
        )
        if relative
    )
    for ref in essential_refs:
        if not _is_file_ref(root, ref):
            raise FileNotFoundError(f"run prompt resource is missing: {ref}")
    required_refs = _existing_refs(
        root,
        tuple(
            dict.fromkeys(
                (
                    *required_refs,
                    *essential_refs,
                    *contract_refs,
                    *artifact_refs_value,
                )
            )
        ),
    )
    # Short guidance files are ordinary reading suggestions, with no registry
    # or profile state to validate during execution or resume.
    guidance_refs = tuple(
        str(path) for path in sorted((resource_root / "guides").glob("*.md"))
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
        _write_json_atomic(path, assembly.to_json())
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


__all__ = [
    "PromptAssembly",
    "build_prompt_assembly",
    "load_prompt_assembly",
]
