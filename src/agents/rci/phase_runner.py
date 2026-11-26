"""Direct prompt and semantic-call boundary for one RCI phase."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.agents.roles import SemanticRoleSpec, run_semantic_role
from pace.artifacts.calls import update_agent_call_deliverable
from pace.artifacts.events import publish_progress, read_events
from pace.artifacts.workspace import (
    _write_json_atomic as _write_json,
)
from pace.artifacts.workspace import _write_text_atomic, stage_ref
from pace.contracts.research.memory import digest
from pace.runtime.agent_environment import prepare_role_environment
from pace.runtime.prompts import render_prompt_template
from pace.runtime.schema import STRING, STRING_LIST, closed_object
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    StructuredSemanticResult,
    semantic_call,
    validate_semantic_payload,
)

from .prompt_composer import build_instructions, build_prompt, skill_refs
from .receipts import host_receipt_fields, output_names, validate_receipt
from .roles import (
    AUTHORS,
    CRITICS,
    PHASE_ACTIONS,
    RciRoleBinding,
    role_binding,
)


class RciPhaseError(RuntimeError):
    """A phase failed at the model interface rather than mathematically."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        diagnostics: Iterable[dict[str, Any]] = (),
        reason_code: str = "",
    ) -> None:
        super().__init__(message)
        self.phase = str(phase)
        self.diagnostics = tuple(dict(item) for item in diagnostics)
        self.reason_code = str(reason_code or "")
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

    payload: dict[str, Any]
    artifact_refs: tuple[str, ...]


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
        round_index: int = 0,
        allowed_actions: Iterable[str] = (),
        routing_context: Mapping[str, Any] | None = None,
    ) -> RciPhaseResult:
        """Run one append-only phase call with available predecessor context."""
        binding = role_binding(phase)
        routing = dict(routing_context or {})
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
            result = self._run_phase(
                binding=binding,
                root=root,
                target_dir=target_dir,
                target_contract_id=target_contract_id,
                input_refs=input_refs,
                prompt_seed=prompt_seed,
                allowed_actions=allowed_actions,
                routing=routing,
                progress_context=progress_context,
            )
        except Exception:
            publish_progress(root, **progress_context, status="failed")
            raise
        publish_progress(
            root,
            **progress_context,
            status="completed",
            artifact_refs=result.artifact_refs,
        )
        return result

    def _run_phase(
        self,
        *,
        binding: RciRoleBinding,
        root: Path,
        target_dir: Path,
        target_contract_id: str,
        input_refs: Mapping[str, str | Path | Iterable[str | Path]],
        prompt_seed: str,
        allowed_actions: Iterable[str],
        routing: dict[str, Any],
        progress_context: dict[str, Any],
    ) -> RciPhaseResult:
        phase = binding.phase
        round_index = progress_context["round_index"]
        actions = tuple(allowed_actions) or PHASE_ACTIONS.get(phase, ())
        fields = {}
        if actions:
            fields["action"] = {"type": "string", "enum": list(actions)}
        fields.update(
            {
                field: STRING
                for field in {
                    "CONJECTURE_EXPLORER": ("mechanism_ref",),
                    "MECHANISM_EXPLORER": ("base_mechanism_ref",),
                    "CANDIDATE_CRITIC": ("existing_candidate_ref",),
                }.get(phase, ())
            }
        )
        if "LOCAL_REPAIR" in actions:
            fields["gap_id"] = STRING
        if phase in AUTHORS:
            fields["dependency_refs"] = STRING_LIST
        receipt_schema = closed_object(fields, required=("action",) if actions else ())
        normalized = {
            kind: tuple(
                ref
                for ref in _refs(input_refs.get(kind, ()))
                if _is_file_ref(root, ref)
            )
            for kind in (*binding.required_inputs, *binding.extra_inputs)
        }
        missing = [kind for kind in binding.required_inputs if not normalized[kind]]
        if missing:
            code = (
                "required_input_file_missing"
                if any(input_refs.get(kind) for kind in missing)
                else "required_input_missing"
            )
            raise RciPhaseError(
                f"{phase} is missing inputs: {', '.join(missing)}",
                phase=phase,
                diagnostics=({"reason_code": code, "input_classes": missing},),
            )

        instructions = build_instructions(phase=phase, workdir=root)
        methods = skill_refs(phase, workdir=root)
        allowed = tuple(
            dict.fromkeys(
                (*[ref for refs in normalized.values() for ref in refs], *methods)
            )
        )
        allowed_outputs = tuple(
            _relative_ref(target_dir / name, root) for name in output_names(phase)
        )
        finding_dir = root / stage_ref(round_index, workdir=root) / "findings"
        output_path = target_dir / "output.json"
        assembly_path = target_dir / "prompt_assembly.json"
        prompt_path = target_dir / "final_prompt.md"
        checkpoint = target_dir / "completed_phase.json"

        final_prompt = build_prompt(
            workdir=root,
            task=prompt_seed,
            actions=actions,
            routing={
                key: routing[key]
                for key in ("base_mechanism_ref", "previous_local_gaps")
                if routing.get(key)
            },
            input_refs=normalized,
            output_directory=_relative_ref(target_dir, root),
            findings_directory=_relative_ref(finding_dir, root)
            if phase not in CRITICS
            else "",
        )
        inputs_snapshot = {
            "phase": phase,
            "target_contract_id": target_contract_id,
            "prompt": final_prompt,
            "instructions": instructions,
            "input_refs": {kind: list(refs) for kind, refs in normalized.items()},
            "contents": {ref: digest((root / ref).read_bytes()) for ref in allowed},
            "routing": routing,
        }
        if assembly_path.is_file():
            assembly = json.loads(assembly_path.read_text())
            if assembly["phase_inputs"] != inputs_snapshot:
                raise RciPhaseError(
                    "saved RCI phase task changed; create a new phase version",
                    phase=phase,
                )
        else:
            assembly = {
                "phase_inputs": inputs_snapshot,
                "finding_refs_before": sorted(
                    _relative_ref(path, root) for path in finding_dir.glob("*.md")
                ),
            }
            _write_json(assembly_path, assembly)

        if checkpoint.is_file():
            cached = json.loads(checkpoint.read_text())
            outputs = cached["outputs"]
            receipt_ref = _relative_ref(output_path, root)
            if receipt_ref not in outputs:
                raise RciPhaseError("committed RCI receipt is missing", phase=phase)
            for ref, expected in outputs.items():
                path = root / ref
                content = path.read_bytes() if path.is_file() else None
                if content is None or digest(content) != expected:
                    raise RciPhaseError("committed RCI artifact changed", phase=phase)
                if ref == receipt_ref:
                    receipt = json.loads(content)
            from pace.artifacts.events import append_event

            append_event(
                root,
                "RCI_PHASE_REUSED",
                phase=phase,
                phase_ref=_relative_ref(target_dir, root),
                reason="exact committed inputs and outputs unchanged",
            )
            return RciPhaseResult(receipt, tuple(outputs))

        previous = set(assembly["finding_refs_before"])
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
                if record["source_prompt_ref"] == _relative_ref(prompt_path, root)
            ),
            "",
        )
        if author_ref:
            if prompt_path.read_text() != final_prompt:
                raise RciPhaseError("saved RCI phase task changed", phase=phase)
        else:
            _write_text_atomic(prompt_path, final_prompt)
        prepare_role_environment(root, binding.role, instructions=instructions)

        def receipt_errors(payload):
            return validate_receipt(
                root, target_dir, phase, payload, inputs=normalized, routing=routing
            )

        accepted_result = None

        def accept_receipt(payload):
            nonlocal accepted_result
            if accepted_result is not None:
                return accepted_result
            host_fields = host_receipt_fields(root, target_dir, phase, payload, routing)
            committed = {**payload, **host_fields}
            drafts = []
            if payload.get("action") == "CLAIM_RESOLVED":
                committed["manuscript_refs"] = [host_fields["resolution_ref"]]
            elif phase not in CRITICS:
                drafts = sorted(
                    _relative_ref(path, root)
                    for path in finding_dir.glob("*.md")
                    if _relative_ref(path, root) not in previous
                )
                if drafts:
                    committed["manuscript_refs"] = drafts
            _write_json(output_path, committed)
            refs = tuple(
                dict.fromkeys(
                    (
                        _relative_ref(output_path, root),
                        *(
                            ref
                            for ref in allowed_outputs
                            if ref in host_fields.values()
                        ),
                        *drafts,
                    )
                )
            )
            _write_json(
                checkpoint,
                {
                    "outputs": {ref: digest((root / ref).read_bytes()) for ref in refs},
                },
            )
            accepted_result = RciPhaseResult(committed, refs)
            return accepted_result

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
                    call_name=_call_name(target_dir, suffix="semantic"),
                    input_refs=allowed,
                    semantic_call_fn=self.semantic_call_fn,
                    role=role,
                    host_metadata={
                        "agent_id": binding.role.lower(),
                        "resume_from_call_ref": author_ref,
                        "read_scope": "declared",
                        "target_contract_id": str(target_contract_id),
                        "round_index": round_index,
                        "task_id": progress_context["task_id"],
                        "parent_task_id": progress_context["parent_task_id"],
                        "output_ref": _relative_ref(output_path, root),
                        "prompt_ref": _relative_ref(
                            target_dir / "final_prompt.md", root
                        ),
                        "output_refs": list(allowed_outputs),
                        "phase": binding.phase,
                        "output_dirs": []
                        if phase in CRITICS
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
                or row.get("reason_code") == "phase_artifact_missing"
                for row in result.diagnostics
            )
        ):
            raw_ref = str(result.host.get("response_ref") or "")
            drafts = tuple(ref for ref in allowed_outputs if (root / ref).is_file())
            correction = render_prompt_template(
                "rci/shared/receipt_correction.md",
                workdir=root,
                actions=list(actions),
                receipt_ref=raw_ref,
                diagnostics=json.dumps(list(result.diagnostics), ensure_ascii=False),
                value_4="\n".join(drafts),
            )
            before = dict(result.payload)
            invalid_fields = {
                row.get("json_pointer", "").strip("/").split("/")[0]
                for row in result.diagnostics
            }

            def correction_errors(payload):
                errors = receipt_errors(payload)
                for key, value in before.items():
                    if key not in fields or key in invalid_fields:
                        continue
                    if key == "action" and value not in actions:
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
                call_name=_call_name(target_dir, suffix="receipt_correction"),
                input_refs=tuple(
                    dict.fromkeys((*allowed, *drafts, *((raw_ref,) if raw_ref else ())))
                ),
                semantic_call_fn=self.semantic_call_fn,
                role=role,
                validator=correction_errors,
                host_metadata={
                    "agent_id": binding.role.lower(),
                    "read_scope": "declared",
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
            )
        if accepted_result is None:
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
            accept_receipt(payload)
        update_agent_call_deliverable(
            root,
            call_id=str(result.host.get("call_id") or ""),
            deliverable_ref=_relative_ref(output_path, root),
        )
        return accepted_result


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
    if host["semantic_status"] == "PASS":
        raise RciPhaseError(
            "successful RCI phase is missing its committed output checkpoint",
            phase=host["host_metadata"]["phase"],
        )
    if host["retry_owner"] == "HOST":
        return StructuredSemanticResult(payload=trace["payload"], host=host)
    return None


def _refs(value: str | Path | Iterable[str | Path]) -> tuple[str, ...]:
    if isinstance(value, (str, Path)):
        values: Iterable[str | Path] = (value,)
    else:
        values = value
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _is_file_ref(root: Path, ref: str) -> bool:
    path = Path(str(ref).strip())
    resolved = (path if path.is_absolute() else root / path).resolve()
    return resolved.is_file()


def _relative_ref(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _call_name(call_dir: Path, *, suffix: str) -> str:
    return "rci_" + "_".join((*call_dir.parts[-5:], suffix))


__all__ = ["RciPhaseError", "RciPhaseResult", "RciPhaseRunner"]
