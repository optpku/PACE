"""Direct prompt and semantic-call boundary for one RCI phase."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import build_prompt_assembly
from pace.agents.roles import SemanticRoleSpec, run_semantic_role
from pace.artifacts.calls import update_agent_call_deliverable
from pace.artifacts.events import publish_progress, read_events
from pace.artifacts.workspace import (
    _write_json_atomic as _write_json,
)
from pace.artifacts.workspace import _write_text_atomic, stage_ref, work_ref
from pace.runtime.agent_environment import prepare_role_environment
from pace.runtime.prompts import render_prompt_template
from pace.runtime.schema import open_object
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    StructuredSemanticResult,
    semantic_call,
    validate_semantic_payload,
)

from .context import linked_review_refs, select_inputs
from .prompt_composer import build_instructions, build_prompt, instruction_refs
from .prompt_composer import skill_refs as phase_skill_refs
from .receipts import host_receipt_fields, materialize_seed_revision, validate_receipt
from .roles import (
    PHASE_ACTIONS,
    RCI_MODE,
    RciRoleBinding,
    role_binding,
)


class RciPhaseError(RuntimeError):
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


class RciBudgetExhausted(RuntimeError):
    """A bounded research dispatch stopped without a mathematical rejection."""


class BudgetedPhaseRunner:
    """Bound new RCI phases while letting committed phases replay on recovery."""

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
                raise RciBudgetExhausted(reason)
            if ref not in state["reserved_phase_refs"]:
                if len(state["reserved_phase_refs"]) >= state["max_new_phases"]:
                    raise RciBudgetExhausted("RCI_PHASE_BUDGET_EXHAUSTED")
                state["reserved_phase_refs"].append(ref)
                _write_json(state_path, state)
        return self.delegate.run(**kwargs)


@dataclass(frozen=True)
class RciPhaseResult:
    """Decoded prompt-owned artifact plus its committed reference."""

    phase: str
    role: str
    payload: dict[str, Any]
    output_ref: str
    prompt_assembly_ref: str


class RciPhaseRunner:
    """Execute each fixed RCI role without a redundant Composer call."""

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
    ) -> RciPhaseResult:
        """Run one append-only phase call with available predecessor context."""
        binding = role_binding(phase)
        routing = dict(routing_context or {})
        if binding.phase == "SEED_SCOUT" and routing.get("seed_operation") not in {
            "CREATE",
            "REVISE",
        }:
            raise ValueError(
                "Seed Scout requires a Host-assigned CREATE or REVISE operation"
            )
        if (
            binding.phase == "MECHANISM_CRITIC"
            and not str(routing.get("mechanism_id") or "").strip()
        ):
            raise ValueError("Mechanism Critic requires a Host-assigned mechanism_id")
        root = Path(workdir)
        target_dir = Path(call_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        progress_context = {
            "task_id": _relative_ref(target_dir, root),
            "stage": binding.phase.lower(),
            "round_index": round_index,
            "parent_task_id": f"round-{round_index:04d}",
        }
        publish_progress(root, **progress_context, status="started")
        try:
            result, artifact_refs = self._run_phase(
                phase=phase,
                binding=binding,
                root=root,
                target_dir=target_dir,
                target_contract_id=target_contract_id,
                input_refs=input_refs,
                prompt_seed=prompt_seed,
                skill_card_refs=skill_card_refs,
                allowed_actions=allowed_actions,
                routing=routing,
                progress_context=progress_context,
            )
        except Exception:
            publish_progress(root, **progress_context, status="failed")
            raise
        publish_progress(
            root, **progress_context, status="completed", artifact_refs=artifact_refs
        )
        return result

    def _run_phase(
        self,
        *,
        phase: str,
        binding: RciRoleBinding,
        root: Path,
        target_dir: Path,
        target_contract_id: str,
        input_refs: Mapping[str, str | Path | Iterable[str | Path]],
        prompt_seed: str,
        skill_card_refs: Iterable[str],
        allowed_actions: Iterable[str],
        routing: dict[str, Any],
        progress_context: dict[str, Any],
    ) -> tuple[RciPhaseResult, tuple[str, ...]]:
        round_index = progress_context["round_index"]
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
        finding_dir = root / stage_ref(round_index, workdir=root) / "findings"
        critic_phase = binding.phase in {
            "RAW_SEED_CRITIC_MATH",
            "RAW_SEED_CRITIC_RESEARCH",
            "MECHANISM_CRITIC",
        }
        previous_drafts = sorted(
            _relative_ref(path, root) for path in finding_dir.glob("*.md")
        )
        normalized: dict[str, tuple[str, ...]] = {}
        for kind, value in input_refs.items():
            refs = _refs(value)
            if refs:
                normalized[str(kind).strip().upper()] = refs
        from pace.agents.director.context import load_index, task_context
        from pace.agents.prompt_composer import delivery_instruction
        from pace.contracts.research.memory import binding as memory_binding
        from pace.contracts.research.memory import (
            check_binding,
            enabled,
            prepare_delta,
            reporter_owned,
        )

        outer_output = stage_ref(round_index, workdir=root) + "/research.md"
        frozen = task_context(root, outer_output) if enabled(root) else {}
        if frozen and binding.phase in {
            "SEED_SCOUT",
            "FRONTIER_EXPLORER",
            "RAW_SEED_CRITIC_RESEARCH",
        }:
            normalized["RESEARCH_CONTEXT"] = (
                stage_ref(round_index, workdir=root) + "/assignment/route_context.json",
            )
        selected = select_inputs(binding.phase, normalized, root)
        archive_inputs = (
            selected if binding.required_inputs == ("PROBE_TASK",) else normalized
        )
        normalized = selected
        if binding.required_inputs != ("PROBE_TASK",):
            if frozen:
                archive_inputs = {
                    **archive_inputs,
                    "TASK_ARCHIVE": (
                        *frozen["required_refs"],
                        *frozen["archive_refs"],
                        frozen["route_context"]["ref"],
                    ),
                }
            for kind, refs in tuple(normalized.items()):
                normalized[kind] = tuple(
                    dict.fromkeys((*refs, *linked_review_refs(root, refs)))
                )
            archive_inputs = {
                kind: tuple(dict.fromkeys((*refs, *linked_review_refs(root, refs))))
                for kind, refs in archive_inputs.items()
            }
        selection_path = target_dir / "input_selection.json"
        selection = {
            **(
                {"existing_finding_refs": previous_drafts}
                if binding.required_inputs != ("PROBE_TASK",)
                else {}
            ),
            "selected": normalized,
            "archive": archive_inputs,
            "policy": "declared role dependencies; no byte truncation",
        }
        if selection_path.is_file():
            selection = json.loads(selection_path.read_text())
            if frozen:
                for row in selection["bindings"]:
                    check_binding(root, row)
                normalized = {k: tuple(v) for k, v in selection["selected"].items()}
                archive_inputs = {k: tuple(v) for k, v in selection["archive"].items()}
        else:
            if frozen:
                selection["bindings"] = [
                    memory_binding(root, ref)
                    for ref in dict.fromkeys(
                        ref
                        for values in (
                            normalized
                            if binding.required_inputs == ("PROBE_TASK",)
                            else archive_inputs
                        ).values()
                        for ref in values
                    )
                    if (root / ref).is_file()
                ]
            _write_json(selection_path, selection)
        supplied = tuple((ref for refs in normalized.values() for ref in refs))

        def receipt_errors(payload):
            errors = validate_receipt(
                root,
                target_dir,
                binding.phase,
                payload,
                seed_base=next(iter(normalized.get("SEED", ())), ""),
                inputs=supplied,
                routing_context=routing,
            )

            if (
                frozen
                and not reporter_owned(root)
                and binding.phase == "FRONTIER_EXPLORER"
            ):
                try:
                    from pace.contracts.research.memory import read_json

                    obligations = dict(load_index(root)["obligations"])
                    obligations.update(
                        {
                            row["id"]: row
                            for row in read_json(root, frozen["route_context"]["ref"])[
                                "obligations"
                            ]
                        }
                    )
                    delta = prepare_delta(
                        root,
                        _relative_ref(target_dir / "research_delta.json", root),
                        obligations,
                    )
                    if delta["branch_id"] != frozen["branch_id"] or not set(
                        delta["obligation_ids"]
                    ) <= set(frozen["obligation_ids"]):
                        raise ValueError("Explorer delta changed assigned identities")
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    errors.append(
                        {
                            "reason_code": "research_delta_invalid",
                            "json_pointer": "/research_delta_ref",
                            "message": str(exc),
                        }
                    )
            return errors

        accepted_result = None

        def accept_receipt(payload):
            nonlocal accepted_result
            if accepted_result is not None:
                return accepted_result
            committed_payload = dict(payload)
            if (
                frozen
                and not reporter_owned(root)
                and binding.phase == "FRONTIER_EXPLORER"
            ):
                committed_payload["research_delta_ref"] = _relative_ref(
                    target_dir / "research_delta.json", root
                )
            committed_payload.update(
                host_receipt_fields(
                    root,
                    target_dir,
                    binding.phase,
                    payload.get("action"),
                    routing_context=routing,
                )
            )
            if binding.phase == "SEED_SCOUT":
                if routing["seed_operation"] == "REVISE":
                    materialize_seed_revision(
                        root, target_dir, seed_base=normalized["SEED"][0]
                    )
                    committed_payload["base_ref"] = normalized["SEED"][0]
                seed = target_dir / "seed.md"
                if not seed.is_file() or not seed.read_bytes().strip():
                    raise ValueError("seed.md is missing or empty after seed creation")
            previous = set(selection.get("existing_finding_refs", previous_drafts))
            new_drafts = sorted(
                ref
                for path in finding_dir.glob("*.md")
                if (ref := _relative_ref(path, root)) not in previous
            )
            committed_payload["manuscript_refs"] = (
                [committed_payload["resolution_ref"]]
                if binding.phase == "FRONTIER_EXPLORER"
                and payload.get("action") == "CLAIM_RESOLVED"
                else ([] if critic_phase else new_drafts)
            )
            _write_json(output_path, committed_payload)
            committed = RciPhaseResult(
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
            for ref in new_drafts:
                output_files[ref] = (root / ref).read_bytes().hex()
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

        instructions = build_instructions(phase=binding.phase, workdir=root)
        prepare_role_environment(root, binding.role, instructions=instructions)
        skill_refs = phase_skill_refs(
            binding.phase, workdir=root, extra_refs=tuple(skill_card_refs)
        )
        checkpoint = target_dir / "completed_phase.json"
        inputs_snapshot = {
            "instructions": instructions,
            "skills": {ref: (root / ref).read_bytes().hex() for ref in skill_refs},
            "archive_refs": {kind: list(refs) for kind, refs in archive_inputs.items()}
            if binding.required_inputs != ("PROBE_TASK",)
            else {},
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
        assembly_path = target_dir / "prompt_assembly.json"
        if assembly_path.is_file():
            saved_assembly = json.loads(assembly_path.read_text())
            if saved_assembly["phase_inputs"] != inputs_snapshot:
                raise RciPhaseError(
                    "saved RCI phase task changed; create a new phase version",
                    phase=phase,
                )
        if checkpoint.is_file():
            cached = json.loads(checkpoint.read_text(encoding="utf-8"))
            if cached["inputs"] != inputs_snapshot:
                raise RciPhaseError(
                    "saved RCI phase inputs changed; preserve the old version",
                    phase=phase,
                )
            for ref, content in cached["outputs"].items():
                if (
                    not (root / ref).is_file()
                    or (root / ref).read_bytes().hex() != content
                ):
                    raise RciPhaseError("committed RCI artifact changed", phase=phase)
            from pace.artifacts.events import append_event

            append_event(
                root,
                "RCI_PHASE_REUSED",
                phase=phase,
                phase_ref=_relative_ref(target_dir, root),
                reason="exact committed inputs and outputs unchanged",
            )
            return RciPhaseResult(**cached["result"]), tuple(cached["outputs"])
        # Keep unfinished manuscripts at their original paths for this exact task.
        # Mathematical revisions already receive a new phase directory in the graph.
        calls = []
        for event in read_events(root):
            if (
                event.get("event_kind") == "SEMANTIC_CALL_STARTED"
                and event.get("task_id") == progress_context["task_id"]
            ):
                ref = event["call_ref"]
                calls.append((ref, json.loads((root / ref).read_text())))
        author_ref = next(
            (
                ref
                for ref, record in reversed(calls)
                if record["source_prompt_ref"]
                == _relative_ref(target_dir / "final_prompt.md", root)
            ),
            "",
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
                            *(() if reporter_owned(root) else ("research_delta.json",)),
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
            resource_refs = tuple(
                work_ref(root, "resources/prompts/" + ref)
                for ref in instruction_refs(binding.phase, workdir=root)
            )
            resource_refs = (*resource_refs, *skill_refs)
            assembly = build_prompt_assembly(
                workdir=root,
                stage=binding.phase,
                role=binding.role,
                mode=RCI_MODE,
                target_contract_id=str(target_contract_id),
                candidate_file_refs=(*predecessor_refs, *resource_refs),
                required_file_refs=(*required_predecessors, *resource_refs),
                contract_guide_refs=(
                    work_ref(root, "resources/prompts/shared/contracts/research.md"),
                ),
                artifact_guide_refs=(
                    work_ref(root, "resources/prompts/shared/artifact_writing.md"),
                ),
                prompt_seed=str(prompt_seed),
                output_artifact_ref=_relative_ref(output_path, root),
            )
            _write_json(
                assembly_path,
                {
                    **assembly.to_json(),
                    "manifest_ref": _relative_ref(assembly_path, root),
                    "phase_inputs": inputs_snapshot,
                },
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
            archives = (
                tuple(
                    dict.fromkeys(
                        ref
                        for values in archive_inputs.values()
                        for ref in values
                        if ref not in allowed and _is_file_ref(root, ref)
                    )
                )
                if archive_access
                else ()
            )
            final_prompt = build_prompt(
                workdir=root,
                task=str(prompt_seed),
                actions=actions,
                routing=routing,
                input_refs=allowed,
                archive_ref=archive_ref if archive_access else "",
                output_directory=_relative_ref(target_dir, root),
                findings_directory=f"{stage_ref(round_index, workdir=root)}/findings",
            )
            if (
                frozen
                and not reporter_owned(root)
                and binding.phase == "FRONTIER_EXPLORER"
            ):
                instruction = delivery_instruction(root, outer_output, with_delta=True)
                instruction = instruction.replace(
                    str(Path(outer_output).parent / "research_delta.json"),
                    _relative_ref(target_dir / "research_delta.json", root),
                )
                instruction = instruction.replace(
                    outer_output, _relative_ref(target_dir / "change.md", root)
                )
                final_prompt += instruction
            prompt_path = target_dir / "final_prompt.md"
            if author_ref:
                if prompt_path.read_text(encoding="utf-8") != final_prompt:
                    raise ValueError(
                        "saved RCI phase prompt changed; create a new phase version"
                    )
            else:
                _write_text_atomic(prompt_path, final_prompt)
        except Exception as exc:
            raise RciPhaseError(
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
            host_schema_version="rci_phase_call/v1",
            authority="SEARCH_CONTEXT_ONLY",
            fatal_scope="CALL",
        )
        try:
            result = _saved_author_result(root, author_ref) if author_ref else None
            if result is None:
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
                        "resume_from_call_ref": author_ref,
                        "archive_refs": archives,
                        "target_contract_id": str(target_contract_id),
                        "round_index": round_index,
                        "task_id": progress_context["task_id"],
                        "parent_task_id": progress_context["parent_task_id"],
                        "output_ref": _relative_ref(output_path, root),
                        "prompt_ref": _relative_ref(
                            target_dir / "final_prompt.md", root
                        ),
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
                                *(
                                    ()
                                    if reporter_owned(root)
                                    else ("research_delta.json",)
                                ),
                            )
                        ],
                        "phase": binding.phase,
                        "output_dirs": []
                        if critic_phase
                        else [_relative_ref(finding_dir, root)],
                        "receipt_correction_owner": "RCI_PHASE",
                        "allowed_actions": list(actions),
                        "prompt_assembly_ref": _relative_ref(assembly_path, root),
                    },
                )
        except Exception as exc:
            raise RciPhaseError(
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
                row.get("stage") == "acceptance"
                or row.get("reason_code")
                in {"phase_artifact_missing", "phase_artifact_invalid"}
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
                )
            )
            correction = render_prompt_template(
                "rci/shared/receipt_correction.md",
                workdir=root,
                actions=list(actions),
                receipt_ref=raw_ref,
                diagnostics=json.dumps(list(result.diagnostics), ensure_ascii=False),
                value_4="\n".join(drafts),
            )
            before = dict(result.payload)
            for key in host_receipt_fields(
                root,
                target_dir,
                binding.phase,
                before.get("action"),
                routing_context=routing,
            ):
                before.pop(key, None)
            invalid_fields = {
                row.get("json_pointer", "").strip("/").split("/")[0]
                for row in result.diagnostics
            }

            def correction_errors(payload):
                errors = receipt_errors(payload)
                for key, value in before.items():
                    if key not in {
                        "action",
                        "decisions",
                        "support_disposition",
                        "representative_mechanism_id",
                    } and not key.endswith(("_ref", "_refs", "_ids")):
                        continue
                    if key == "action":
                        if value not in actions:
                            continue
                    elif key in invalid_fields:
                        continue
                    if (
                        value in (None, "")
                        or key == "selected_mechanism_ids"
                        and not value
                    ):
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
                    "archive_refs": archives,
                    "task_id": progress_context["task_id"],
                    "parent_task_id": progress_context["parent_task_id"],
                    "stage_ref": stage_ref(round_index, workdir=root),
                    "phase": binding.phase,
                    "round_index": round_index,
                    "target_contract_id": str(target_contract_id),
                    "output_ref": _relative_ref(output_path, root),
                    "receipt_correction_owner": "RCI_PHASE",
                    "receipt_correction": True,
                    "resume_from_call_ref": next(
                        (
                            ref
                            for ref, record in reversed(calls)
                            if record["parent_call_id"] == result.host["call_id"]
                        ),
                        result.host["call_ref"],
                    ),
                    "output_refs": [],
                    "output_dirs": [],
                    "parent_call_id": result.host["call_id"],
                    "representation_correction_of": result.host["call_id"],
                    "receipt_corrections": 1,
                    "allowed_actions": list(actions),
                },
            )
        if isinstance(
            result, (SemanticInterfaceFailure, SemanticRepresentationFailure)
        ):
            raise RciPhaseError(
                f"{binding.phase} did not return a usable artifact",
                phase=binding.phase,
                diagnostics=result.diagnostics,
                receipt_corrections=int(correction_validator is not None),
            )
        payload = result.payload
        if not isinstance(payload, dict):
            raise RciPhaseError(
                f"{binding.phase} JSON artifact must be an object",
                phase=binding.phase,
                diagnostics=({"reason_code": "payload_not_object"},),
            )
        routing_errors = validate_semantic_payload(payload, receipt_schema) or (
            correction_validator or receipt_errors
        )(payload)
        if routing_errors:
            raise RciPhaseError(
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
        return committed, tuple(json.loads(checkpoint.read_text())["outputs"])


def _saved_author_result(root: Path, call_ref: str):
    trace_path = (root / call_ref).with_name("interface_result.json")
    if not trace_path.is_file():
        return None
    trace = json.loads(trace_path.read_text())
    host = trace["host"]
    if host["semantic_status"] == "FAILED":
        return SemanticRepresentationFailure(
            payload=trace["payload"], host=host, diagnostics=tuple(trace["diagnostics"])
        )
    if host["host_metadata"]["transport"]["status"] == "SUCCESS":
        return StructuredSemanticResult(payload=trace["payload"], host=host)
    return None


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
    return "rci_" + "_".join((*parts, phase.lower(), suffix))


__all__ = ["RciPhaseError", "RciPhaseResult", "RciPhaseRunner"]
