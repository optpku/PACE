"""Direct prompt and semantic-call boundary for one SMI phase."""

from __future__ import annotations
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pace.agents.roles import run_semantic_role
from pace.runtime.output.calls import update_agent_call_deliverable
from pace.runtime.output.events import publish_progress
from pace.runtime.output.workspace import (
    _write_json_atomic as _write_json,
)
from pace.runtime.output.workspace import stage_ref, work_ref
from pace.runtime.prompt_policy import build_prompt_assembly
from pace.runtime.prompts import prompt_root
from pace.runtime.roles import SemanticRoleSpec
from pace.runtime.schema import open_object
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    semantic_call,
    validate_semantic_payload,
)
from .receipts import validate_receipt, materialize_seed_revision, host_receipt_fields
from .context import select_inputs, linked_review_refs
from .roles import (
    SMI_MODE,
    PHASE_ACTIONS,
    SMI_SKILL_CORE_REF,
    SMI_SKILL_ENTRY_REF,
    resolve_skill_refs,
    role_binding,
)


class SmiPhaseError(RuntimeError):
    """A phase failed at the model interface rather than mathematically."""

    failure_owner = "HOST"

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        diagnostics: Iterable[dict[str, Any]] = (),
        reason_code: str = "",
        receipt_corrections: int = 0,
    ) -> None:
        super().__init__(message)
        self.phase = str(phase)
        self.diagnostics = tuple((dict(item) for item in diagnostics))
        self.reason_code = str(reason_code or "")
        self.receipt_corrections = receipt_corrections
        if not self.reason_code:
            self.reason_code = next(
                (
                    str(item.get("reason_code") or "")
                    for item in self.diagnostics
                    if str(item.get("reason_code") or "")
                ),
                "",
            )


class SmiBudgetExhausted(RuntimeError):
    """A bounded research dispatch stopped without a mathematical rejection."""


class BudgetedPhaseRunner:
    """Bound new SMI phases while letting committed phases replay on recovery."""

    def __init__(self, delegate, *, root: Path, directory: Path, limit: int = 64):
        self.delegate, self.root, self.directory, self.limit = (
            delegate,
            root,
            directory,
            limit,
        )

    def run(self, **kwargs):
        call_dir = Path(kwargs["call_dir"])
        if not (call_dir / "completed_phase.json").is_file():
            from pace.runtime.sessions import budget_exhausted

            reason = budget_exhausted(self.root)
            state_path = self.directory / "phase_budget.json"
            state = (
                json.loads(state_path.read_text())
                if state_path.is_file()
                else {"max_new_phases": self.limit, "reserved_phase_refs": []}
            )
            ref = call_dir.relative_to(self.root).as_posix()
            if reason:
                raise SmiBudgetExhausted(reason)
            if ref not in state["reserved_phase_refs"]:
                if len(state["reserved_phase_refs"]) >= state["max_new_phases"]:
                    raise SmiBudgetExhausted("SMI_PHASE_BUDGET_EXHAUSTED")
                state["reserved_phase_refs"].append(ref)
                _write_json(state_path, state)
        return self.delegate.run(**kwargs)


@dataclass(frozen=True)
class SmiPhaseResult:
    """Decoded prompt-owned artifact plus its committed reference."""

    phase: str
    role: str
    payload: dict[str, Any]
    output_ref: str
    prompt_assembly_ref: str


class SmiPhaseRunner:
    """Execute each fixed SMI role without a redundant Composer call."""

    def __init__(self, *, llm: Any, semantic_call_fn: Any = semantic_call) -> None:
        self.llm = llm
        self.semantic_call_fn = semantic_call_fn

    def run(
        self,
        *,
        phase: str,
        workdir: Path,
        call_dir: Path,
        target_contract_id: str,
        input_refs: Mapping[str, str | Path | Iterable[str | Path]],
        prompt_seed: str,
        skill_card_refs: Iterable[str] = (),
        round_index: int = 0,
        allowed_actions: Iterable[str] = (),
        routing_context: Mapping[str, Any] | None = None,
    ) -> SmiPhaseResult:
        """Run one append-only phase call with available predecessor context."""
        binding = role_binding(phase)
        routing = dict(routing_context or {})
        if binding.phase == "SEED_SCOUT" and routing.get("seed_operation") not in {"CREATE", "REVISE"}:
            raise ValueError("Seed Scout requires a Host-assigned CREATE or REVISE operation")
        requested_actions = tuple(allowed_actions)
        actions = requested_actions or PHASE_ACTIONS.get(binding.phase, ())
        receipt_schema = open_object()
        if actions:
            receipt_schema["properties"]["action"] = {
                "type": "string",
                "enum": list(actions),
            }
            receipt_schema["required"] = ["action"]
        elif binding.phase == "SEMANTIC_DEDUP":
            receipt_schema["properties"]["decisions"] = {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["task_id", "action"],
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "action": {"type": "string", "enum": ["KEEP", "DUPLICATE"]},
                    },
                },
            }
            receipt_schema["required"] = ["decisions"]
        root = Path(workdir)
        target_dir = Path(call_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        progress_context = {
            "task_id": _relative_ref(target_dir, root),
            "stage": binding.phase.lower(),
            "round_index": round_index,
            "parent_task_id": f"round-{round_index:04d}",
        }
        finding_dir = root / stage_ref(round_index, workdir=root) / "findings"
        previous_drafts = set(finding_dir.glob("*.md"))
        publish_progress(root, **progress_context, status="started")
        normalized: dict[str, tuple[str, ...]] = {}
        for kind, value in input_refs.items():
            refs = _refs(value)
            if refs:
                normalized[str(kind).strip().upper()] = refs
        selected = select_inputs(binding.phase, normalized, root)
        archive_inputs = normalized
        normalized = selected
        if binding.required_inputs != ("PROBE_TASK",):
            for kind, refs in tuple(normalized.items()):
                normalized[kind] = tuple(
                    dict.fromkeys((*refs, *linked_review_refs(root, refs)))
                )
            archive_inputs = {
                kind: tuple(dict.fromkeys((*refs, *linked_review_refs(root, refs))))
                for kind, refs in archive_inputs.items()
            }
        _write_json(
            target_dir / "input_selection.json",
            {
                "selected": normalized,
                "archive": archive_inputs,
                "policy": "declared role dependencies; no byte truncation",
            },
        )
        supplied = tuple((ref for refs in normalized.values() for ref in refs))

        def receipt_errors(payload):
            return validate_receipt(
                root,
                target_dir,
                binding.phase,
                payload,
                seed_base=next(iter(normalized.get("SEED", ())), ""),
                inputs=supplied,
                routing_context=routing,
            )

        accepted_result = None

        def accept_receipt(payload):
            nonlocal accepted_result
            if accepted_result is not None:
                return accepted_result
            committed_payload = dict(payload)
            committed_payload.update(host_receipt_fields(
                root, target_dir, binding.phase, payload.get("action"), routing_context=routing
            ))
            if binding.phase == "SEED_SCOUT":
                if routing["seed_operation"] == "REVISE":
                    materialize_seed_revision(root, target_dir, seed_base=normalized["SEED"][0])
                    committed_payload["base_ref"] = normalized["SEED"][0]
                seed = target_dir / "seed.md"
                if not seed.is_file() or not seed.read_bytes().strip():
                    raise ValueError("seed.md is missing or empty after seed creation")
            _write_json(output_path, committed_payload)
            committed = SmiPhaseResult(
                phase=binding.phase,
                role=binding.role,
                payload=committed_payload,
                output_ref=_relative_ref(output_path, root),
                prompt_assembly_ref=_relative_ref(assembly_path, root),
            )
            output_files = {
                _relative_ref(output_path, root): output_path.read_bytes().hex()
            }
            for key, value in committed_payload.items():
                if key.endswith(("_ref", "_refs")):
                    for ref in value if isinstance(value, list) else [value]:
                        if isinstance(ref, str) and ref and (root / ref).is_file():
                            output_files[ref] = (root / ref).read_bytes().hex()
            for draft in sorted(set(finding_dir.glob("*.md")) - previous_drafts):
                output_files[_relative_ref(draft, root)] = draft.read_bytes().hex()
            _write_json(
                checkpoint,
                {
                    "inputs": inputs_snapshot,
                    "outputs": output_files,
                    "result": {
                        "phase": committed.phase,
                        "role": committed.role,
                        "payload": committed.payload,
                        "output_ref": committed.output_ref,
                        "prompt_assembly_ref": committed.prompt_assembly_ref,
                    },
                },
            )
            accepted_result = committed
            return committed

        checkpoint = target_dir / "completed_phase.json"
        inputs_snapshot = {
            "phase": phase,
            "target_contract_id": target_contract_id,
            "input_refs": {kind: list(refs) for kind, refs in normalized.items()},
            "allowed_actions": list(requested_actions),
            "prompt_seed": prompt_seed,
            "contents": {
                ref: (root / ref).read_bytes().hex()
                for refs in normalized.values()
                for ref in refs
                if (root / ref).is_file()
            },
        }
        if routing:
            inputs_snapshot["routing_context"] = routing
        if checkpoint.is_file():
            cached = json.loads(checkpoint.read_text(encoding="utf-8"))
            if cached["inputs"] != inputs_snapshot:
                raise SmiPhaseError(
                    "saved SMI phase inputs changed; preserve the old version",
                    phase=phase,
                )
            for ref, content in cached["outputs"].items():
                if (
                    not (root / ref).is_file()
                    or (root / ref).read_bytes().hex() != content
                ):
                    raise SmiPhaseError("committed SMI artifact changed", phase=phase)
            from pace.runtime.output.events import append_event

            append_event(
                root,
                "SMI_PHASE_REUSED",
                phase=phase,
                phase_ref=_relative_ref(target_dir, root),
                reason="exact committed inputs and outputs unchanged",
            )
            publish_progress(root, **progress_context, status="completed",
                             artifact_refs=tuple(cached["outputs"]))
            return SmiPhaseResult(**cached["result"])
        partials = [
            path
            for path in target_dir.iterdir()
            if path.name
            not in {
                "prompt_assembly.json",
                "input_selection.json",
                "final_prompt.md",
                "input_diagnostics.json",
                "interrupted_attempts",
            }
        ]
        if partials:
            archive = target_dir / "interrupted_attempts"
            archive.mkdir(exist_ok=True)
            previous = archive / f"attempt-{len(list(archive.iterdir())) + 1:04d}"
            previous.mkdir()
            moved_refs = {}
            for path in partials:
                for file in path.rglob("*") if path.is_dir() else (path,):
                    if file.is_file():
                        destination = (
                            previous / path.name / file.relative_to(path)
                            if path.is_dir()
                            else previous / path.name
                        )
                        moved_refs[_relative_ref(file, root)] = _relative_ref(
                            destination, root
                        )
                path.replace(previous / path.name)
            for record_path in (root / stage_ref(round_index, workdir=root)).glob(
                "calls/*/record.json"
            ):
                record = json.loads(record_path.read_text(encoding="utf-8"))
                archived_ref = moved_refs.get(record.get("deliverable_ref"))
                if archived_ref:
                    update_agent_call_deliverable(
                        root, call_id=record["call_id"], deliverable_ref=archived_ref
                    )
        missing = tuple(
            (kind for kind in binding.required_inputs if not normalized.get(kind))
        )
        missing_files = tuple(
            (
                ref
                for kind in binding.required_inputs
                for ref in normalized.get(kind, ())
                if not _is_file_ref(root, ref)
            )
        )
        input_diagnostics: list[dict[str, Any]] = []
        if missing:
            input_diagnostics.append(
                {
                    "reason_code": "required_input_missing",
                    "input_classes": list(missing),
                }
            )
        if missing_files:
            input_diagnostics.append(
                {
                    "reason_code": "required_input_file_missing",
                    "paths": list(missing_files),
                }
            )
        if input_diagnostics:
            _write_json(
                target_dir / "input_diagnostics.json",
                {
                    "prompt_ref": _relative_ref(target_dir / "final_prompt.md", root),
                    "output_refs": [
                        _relative_ref(target_dir / name, root)
                        for name in (
                            "seed.md",
                            "candidate.md",
                            "mechanism.md",
                            "probe_task.md",
                            "closure_brief.md",
                            "review.md",
                            "change.md",
                            "patch.json",
                        )
                    ],
                    "phase": binding.phase,
                    "diagnostics": input_diagnostics,
                },
            )
        predecessor_refs = tuple(
            (
                ref
                for refs in normalized.values()
                for ref in refs
                if _is_file_ref(root, ref)
            )
        )
        required_predecessors = tuple(
            (
                ref
                for kind in binding.required_inputs
                for ref in normalized.get(kind, ())
                if _is_file_ref(root, ref)
            )
        )
        assembly_path = target_dir / "prompt_assembly.json"
        output_path = target_dir / "output.json"
        try:
            skill_refs = resolve_skill_refs(
                root,
                (
                    *binding.skill_refs,
                    *skill_card_refs,
                    SMI_SKILL_CORE_REF,
                    *(
                        (SMI_SKILL_ENTRY_REF,)
                        if binding.required_inputs != ("PROBE_TASK",)
                        else ()
                    ),
                ),
            )
            resource_refs = (
                work_ref(root, binding.prompt_ref),
                *skill_refs,
                *(),
                work_ref(workdir, "resources/prompts/output/findings.md"),
                work_ref(workdir, "resources/prompts/output/structured.md"),
            )
            assembly = build_prompt_assembly(
                workdir=root,
                stage=binding.phase,
                role=binding.role,
                mode=SMI_MODE,
                target_contract_id=str(target_contract_id),
                manifest_path=assembly_path,
                candidate_file_refs=(*predecessor_refs, *resource_refs),
                required_file_refs=(*required_predecessors, *resource_refs),
                contract_guide_refs=(
                    work_ref(workdir, "resources/prompts/contracts/research.md"),
                ),
                artifact_guide_refs=(
                    work_ref(workdir, "resources/prompts/kernel/artifact_writing.md"),
                ),
                prompt_seed=str(prompt_seed),
                output_artifact_ref=_relative_ref(output_path, root),
            )
            allowed = tuple(
                dict.fromkeys(
                    (
                        *assembly.candidate_refs,
                        *assembly.required_refs,
                        *assembly.contract_guide_refs,
                        *assembly.artifact_guide_refs,
                    )
                )
            )
            archive_ref = _relative_ref(target_dir / "input_selection.json", root)
            archive_access = binding.required_inputs != ("PROBE_TASK",)
            if archive_access:
                allowed = (*allowed, archive_ref)
            guidance = []
            inputs = []
            for ref in allowed:
                if archive_access and ref == archive_ref:
                    continue
                path = Path(ref)
                path = (path if path.is_absolute() else root / path).resolve()
                if path.is_relative_to(prompt_root(root)) and (
                    not path.is_relative_to(prompt_root(root) / "smi/skills")
                ):
                    guidance.append(path.read_text(encoding="utf-8"))
                else:
                    inputs.append(ref)
            final_prompt = "\n\n".join(
                (
                    *guidance,
                    "## Task\n" + str(prompt_seed),
                    *(
                        (
                            f"## Receipt routing contract\nFor this invocation, return an `action` equal to one of {list(actions)!r}. Earlier source artifacts do not change this controller-owned routing choice.",
                        )
                        if actions
                        else ()
                    ),
                    *(
                        ("## Host-assigned routing identities\n" + json.dumps(routing),)
                        if routing
                        else ()
                    ),
                    "The original target contract is authoritative. Candidate assumptions are proposals, not new permissions; explicitly distinguish any restricted task from the full original target.",
                    "## Files to read\n" + "\n".join((f"- `{ref}`" for ref in inputs)),
                    *(
                        (
                            f"Optional dependency index: `{archive_ref}`. Read it only when a specific missing definition, source or objection requires an archived dependency. Follow relevant source references as needed; Record any additional dependency used in dependency_refs so it is bound to this review. If it cannot be supplied, report the missing dependency to the Director.",
                        )
                        if archive_access
                        else ()
                    ),
                    f"Mathematical artifact directory: `{_relative_ref(target_dir, root)}`. Write the original files requested by your role here during this invocation. "
                    + (
                        "The Host records the assigned seed operation and artifact paths. Return one JSON object with any mathematical handoff rationale; do not return an action or repeat those Host-owned fields. "
                        if binding.phase == "SEED_SCOUT"
                        else "Return the routing fields and any caller-owned file references requested by your role. Reuse the supplied candidate_ref for an unchanged task; write a new file for a revision. Keep candidate task manuscripts free of mechanism derivations, source leads and review judgments. "
                    )
                    + "Keep proposed assumptions distinct from the full original target. Keep the complete mathematics in its original files.",
                    f"Standalone finding draft directory: `{stage_ref(round_index, workdir=workdir)}/findings`. Only write a draft when this phase already establishes its complete argument; use a phase-specific filename and leave earlier drafts unchanged.",
                )
            )
            (target_dir / "final_prompt.md").write_text(final_prompt, encoding="utf-8")
        except Exception as exc:
            publish_progress(root, **progress_context, status="failed")
            raise SmiPhaseError(
                f"{binding.phase} prompt assembly failed",
                phase=binding.phase,
                diagnostics=(
                    {"reason_code": "prompt_assembly_failed", "message": str(exc)},
                ),
            ) from exc
        role = SemanticRoleSpec(
            role=binding.role.lower(),
            schema=lambda: receipt_schema,
            validator=receipt_errors,
            on_accept=accept_receipt,
            host_schema_version="smi_phase_call/v1",
            authority="SEARCH_CONTEXT_ONLY",
            fatal_scope="CALL",
        )
        try:
            result = run_semantic_role(
                self.llm,
                final_prompt,
                workdir=root,
                call_name=_call_name(binding.phase, target_dir, suffix="semantic"),
                input_refs=allowed,
                semantic_call_fn=self.semantic_call_fn,
                role=role,
                host_metadata={
                    "agent_id": binding.role.lower(),
                    "target_contract_id": str(target_contract_id),
                    "round_index": round_index,
                    "task_id": progress_context["task_id"],
                    "parent_task_id": progress_context["parent_task_id"],
                    "output_ref": _relative_ref(output_path, root),
                    "prompt_ref": _relative_ref(target_dir / "final_prompt.md", root),
                    "output_refs": [
                        _relative_ref(target_dir / name, root)
                        for name in (
                            "seed.md",
                            "candidate.md",
                            "mechanism.md",
                            "probe_task.md",
                            "closure_brief.md",
                            "review.md",
                            "change.md",
                            "patch.json",
                        )
                    ],
                    "phase": binding.phase,
                    "output_dirs": [_relative_ref(finding_dir, root)],
                    "receipt_correction_owner": "SMI_PHASE",
                    "allowed_actions": list(actions),
                    "prompt_assembly_ref": _relative_ref(assembly_path, root),
                },
            )
        except Exception as exc:
            publish_progress(root, **progress_context, status="failed")
            raise SmiPhaseError(
                f"{binding.phase} semantic call raised an interface error",
                phase=binding.phase,
                diagnostics=(
                    {
                        "reason_code": "semantic_call_exception",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
            ) from exc
        from pace.runtime.sessions import budget_exhausted

        correction_validator = None
        if (
            isinstance(result, SemanticRepresentationFailure)
            and not budget_exhausted(root)
        ) and (
            not any(
                row.get("stage") == "acceptance" or row.get("reason_code") in {
                    "phase_artifact_missing", "phase_artifact_invalid"
                }
                for row in result.diagnostics
            )
        ):
            raw_ref = str(result.host.get("response_ref") or "")
            drafts = tuple(
                (
                    p.relative_to(root).as_posix()
                    for p in target_dir.rglob("*")
                    if p.is_file()
                    and p.suffix in {".md", ".json"}
                    and (
                        p.name
                        not in {
                            "final_prompt.md",
                            "prompt_assembly.json",
                            "output.json",
                            "completed_phase.json",
                        }
                    )
                    and ("interrupted_attempts" not in p.parts)
                )
            )
            correction = (
                f"Repair only the JSON receipt/interface for the previous SMI call. Do not run research, rewrite any existing mathematical file, change the question, or create missing mathematical evidence. Reuse the files that already exist. Allowed actions: {list(actions)}. Prior receipt: `{raw_ref}`. A valid prior action must remain unchanged. Correct only invalid control fields or file references. If the mathematics needs revision, leave this call failed for its owning mathematical phase.\n"
                + json.dumps(list(result.diagnostics), ensure_ascii=False)
                + "\nExisting files:\n"
                + "\n".join(drafts)
            )
            before = dict(result.payload)
            for key in host_receipt_fields(
                root, target_dir, binding.phase, before.get("action"), routing_context=routing
            ):
                before.pop(key, None)
            invalid_fields = {
                row.get("json_pointer", "").strip("/").split("/")[0]
                for row in result.diagnostics
            }

            def correction_errors(payload):
                errors = receipt_errors(payload)
                for key, value in before.items():
                    if key not in {"action", "decisions", "support_disposition", "representative_mechanism_id"} and not key.endswith(("_ref", "_refs", "_ids")):
                        continue
                    if key == "action":
                        if value not in actions:
                            continue
                    elif key in invalid_fields:
                        continue
                    if value in (None, "") or key == "selected_mechanism_ids" and not value:
                        continue
                    if key.endswith(("_ref", "_refs")) and (not value):
                        continue
                    if payload.get(key) != value:
                        errors.append(
                            {
                                "stage": "receipt",
                                "message": f"receipt correction changed {key}",
                            }
                        )
                return errors

            correction_validator = correction_errors
            result = run_semantic_role(
                self.llm,
                correction,
                workdir=root,
                call_name=_call_name(
                    binding.phase, target_dir, suffix="receipt_correction"
                ),
                input_refs=tuple(
                    dict.fromkeys((*allowed, *drafts, *((raw_ref,) if raw_ref else ())))
                ),
                semantic_call_fn=self.semantic_call_fn,
                role=role,
                validator=correction_errors,
                host_metadata={
                    "agent_id": binding.role.lower(),
                    "task_id": progress_context["task_id"],
                    "parent_task_id": progress_context["parent_task_id"],
                    "stage_ref": stage_ref(round_index, workdir=root),
                    "phase": binding.phase,
                    "round_index": round_index,
                    "target_contract_id": str(target_contract_id),
                    "output_ref": _relative_ref(output_path, root),
                    "receipt_correction_owner": "SMI_PHASE",
                    "receipt_correction": True,
                    "parent_call_id": result.host["call_id"],
                    "representation_correction_of": result.host["call_id"],
                    "receipt_corrections": 1,
                    "allowed_actions": list(actions),
                },
            )
        if isinstance(
            result, (SemanticInterfaceFailure, SemanticRepresentationFailure)
        ):
            publish_progress(root, **progress_context, status="failed")
            raise SmiPhaseError(
                f"{binding.phase} did not return a usable artifact",
                phase=binding.phase,
                diagnostics=result.diagnostics,
                receipt_corrections=int(correction_validator is not None),
            )
        payload = result.payload
        if not isinstance(payload, dict):
            publish_progress(root, **progress_context, status="failed")
            raise SmiPhaseError(
                f"{binding.phase} JSON artifact must be an object",
                phase=binding.phase,
                diagnostics=({"reason_code": "payload_not_object"},),
            )
        routing_errors = validate_semantic_payload(payload, receipt_schema) or (
            correction_validator or receipt_errors
        )(payload)
        if routing_errors:
            raise SmiPhaseError(
                f"{binding.phase} did not return a valid routing receipt",
                phase=phase,
                diagnostics=routing_errors,
                reason_code="phase_receipt_invalid",
            )
        committed = accept_receipt(payload)
        update_agent_call_deliverable(
            root,
            call_id=str(result.host.get("call_id") or ""),
            deliverable_ref=_relative_ref(output_path, root),
        )
        publish_progress(root, **progress_context, status="completed",
                         artifact_refs=tuple(json.loads(checkpoint.read_text())["outputs"]))
        return committed

def _refs(value: str | Path | Iterable[str | Path]) -> tuple[str, ...]:
    if isinstance(value, (str, Path)):
        values: Iterable[str | Path] = (value,)
    else:
        values = value
    return tuple(
        dict.fromkeys((str(item).strip() for item in values if str(item).strip()))
    )


def _is_file_ref(root: Path, ref: str) -> bool:
    path = Path(str(ref).strip())
    resolved = (path if path.is_absolute() else root / path).resolve()
    return resolved.is_file()


def _relative_ref(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _call_name(phase: str, call_dir: Path, *, suffix: str = "composer") -> str:
    parts = [part for part in call_dir.parts[-5:] if part]
    return "smi_" + "_".join((*parts, phase.lower(), suffix))


__all__ = ["SmiPhaseError", "SmiPhaseResult", "SmiPhaseRunner"]
