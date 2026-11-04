"""Default Host adapters for one Director-led specialist call.

The Global Research Director decides what to ask, and the Prompt Composer
assembles the specialist prompt. This module supplies the narrow execution
boundary used by the Director-led engine: run the selected specialist once,
require its explicitly requested file, and return a machine-readable handoff.
A transport response is retained only as private trace data and is never
promoted to a deliverable.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.contracts.research.director_protocol import (
    DISPATCHABLE_AGENT_IDS,
    specialist_spec,
)

from pace.runtime.output.results import FINDINGS_INDEX_REF
from pace.runtime.output.workspace import is_call_path, work_ref
from pace.runtime.prompt_composer import compose_prompt
from pace.runtime.prompt_policy import PromptAssembly, build_prompt_assembly
from pace.runtime.prompts import skill_index_ref
from pace.runtime.semantic import (
    ArtifactCallHandle,
    begin_artifact_call,
    commit_artifact_call_failure,
    commit_artifact_call_success,
)


SPECIALIST_STAGE = "DIRECTOR_LED_SPECIALIST"


@dataclass(frozen=True, slots=True)
class SpecialistAdapterResult:
    """Terminal Host handoff for one specialist call."""

    agent_id: str
    status: str
    call_id: str = ""
    deliverable_ref: str = ""
    failure_class: str = ""
    reason_code: str = ""
    diagnostics_ref: str = ""
    response_ref: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED" and bool(self.deliverable_ref)

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "call_id": self.call_id,
            "deliverable_ref": self.deliverable_ref,
            "failure_class": self.failure_class,
            "reason_code": self.reason_code,
            "diagnostics_ref": self.diagnostics_ref,
            "response_ref": self.response_ref,
        }


def build_default_specialist_handlers(
    *,
    llm: Any,
    workdir: Path,
    output_ref_factory: Callable[[str, int], str] | None = None,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Build Host-owned handlers from the single dispatchable agent registry.

    ``output_ref_factory`` may be supplied by the engine to bind its round
    artifact layout.  The default layout is deterministic and remains below
    ``work/<current-turn>/stages``.
    """

    root = Path(workdir)
    handlers: dict[str, Callable[..., dict[str, Any]]] = {}
    for agent_id in sorted(DISPATCHABLE_AGENT_IDS):
        if agent_id == "smi":
            from pace.orchestrator.smi_adapter import build_smi_handler
            handlers[agent_id] = build_smi_handler(llm=llm, workdir=root)
            continue
        handlers[agent_id] = _handler_for(
            llm=llm,
            workdir=root,
            agent_id=agent_id,
            output_ref_factory=output_ref_factory,
        )
    return handlers


def build_specialist_prompt_assembly(
    *,
    workdir: Path,
    decision: Any,
    target_contract_id: str,
    output_ref: str,
    manifest_ref: str,
) -> PromptAssembly:
    """Prepare the Host-owned context shared by Composer and specialist."""

    inputs = tuple(decision.input_refs)
    dependency_task = ""
    if decision.agent_id == "prover":
        from pace.runtime.prompt_composer import write_dependency_manifest

        dependency_ref, dependency_task = write_dependency_manifest(
            workdir=workdir, output_ref=output_ref, target_contract_id=target_contract_id
        )
        inputs = (*inputs, dependency_ref)
    revision_task = ""
    route_state = workdir / Path(output_ref).parent / "route_context.json"
    if (
        decision.agent_id == "planner"
        and decision.prompt_brief.startswith("REVISE_ROUTE")
        and route_state.is_file()
    ):
        from pace.runtime.text_revisions import revision_instruction

        base = json.loads(route_state.read_text())["route_ref"]
        if not base:
            raise ValueError("REVISE_ROUTE requires a frozen route base")
        inputs = (*inputs, base)
        revision_task = revision_instruction(
            base, str(Path(output_ref).parent / "revision.json")
        )
        revision_task += " Write only revision.json; the Host materializes the assigned complete route. "

    method_refs = (skill_index_ref(workdir),) if decision.agent_id != "smi" else ()

    role_prompt_ref = (
        work_ref(workdir, "resources/prompts/roles/research_framing.md")
        if decision.agent_id == "framer"
        else f"{work_ref(workdir)}/resources/prompts/roles/director_led_{decision.agent_id}.md"
    )
    mode = "RESEARCH_FRAMING" if decision.agent_id == "framer" else "SPECIALIST"
    return build_prompt_assembly(
        workdir=workdir,
        stage="DIRECTOR_LED_SPECIALIST",
        role=f"DIRECTOR_LED_{decision.agent_id.upper()}",
        mode=mode,
        target_contract_id=target_contract_id,
        candidate_file_refs=(
            *inputs,
            *method_refs,
            *((FINDINGS_INDEX_REF,) if (workdir / FINDINGS_INDEX_REF).is_file() else ()),
            role_prompt_ref,
            f"{work_ref(workdir)}/resources/prompts/modes/{mode}.md",
            work_ref(workdir, "resources/prompts/shared/transport.md"),
            work_ref(workdir, "resources/prompts/output/files.md"),
            work_ref(workdir, "resources/prompts/output/findings.md"),
            work_ref(workdir, "resources/prompts/output/memory.md"),
            *(
                (work_ref(workdir, "resources/prompts/kernel/literal_target.md"),)
                if decision.agent_id == "framer"
                else ()
            ),
            *(
                (
                    work_ref(workdir, "resources/prompts/contracts/research.md"),
                    work_ref(workdir, "resources/prompts/kernel/artifact_writing.md"),
                )
                if decision.agent_id == "framer"
                else ()
            ),
        ),
        required_file_refs=(*inputs, *method_refs),
        prompt_seed=(
            f"Bounded objective from Research Director: {decision.prompt_brief}\n"
            "Other independent tasks may run concurrently. Treat all supplied inputs as read-only. "
            "Write only your assigned round's output and scratch files; never read or modify "
            "another pending task's files or the shared run state.\n"
            f"Write the requested deliverable to `{output_ref}`.\n"
            + revision_task
            + dependency_task + "\n"
            "Follow the standalone findings guide when this task establishes a result. "
            f"Finding draft directory: `{Path(output_ref).parent / 'findings'}`.\n"
            "Follow the research memory guide when this task produces a durable lesson. "
            f"Memory draft path: `{Path(output_ref).parent / 'memory_note.md'}`.\n"

        ),
        output_artifact_ref=output_ref,
        # Survey already states its evidence-reporting boundary; proof-use
        # obligations belong to downstream mathematical roles.
        contract_guide_refs=(
            (work_ref(workdir, "resources/prompts/contracts/research.md"),)
            if decision.agent_id == "framer"
            else ()
        ),
        artifact_guide_refs=(
            (work_ref(workdir, "resources/prompts/kernel/artifact_writing.md"),)
            if decision.agent_id == "framer"
            else ()
        ),
        manifest_path=(workdir / manifest_ref).with_name("composer_request.json"),
    )


def build_default_composer(*, llm: Any) -> Callable[..., Any]:
    """Return the production Prompt Composer adapter for Director assignments."""

    def compose(**kwargs: Any) -> Any:
        decision = kwargs["decision"]
        return compose_prompt(
            llm=llm,
            workdir=Path(kwargs["workdir"]),
            assembly=kwargs["assembly"],
            input_refs=kwargs["input_refs"],
            round_index=int(kwargs["round_index"]),
            call_name=(
                f"prompt_composer_director_led_{decision.agent_id}_"
                f"{int(kwargs['round_index']):04d}"
            ),
        )

    return compose


def run_default_specialist(
    *,
    llm: Any,
    workdir: Path,
    prompt: str,
    agent_id: str,
    input_refs: Iterable[str] = (),
    round_index: int = 1,
    decision: Any = None,
    context: Mapping[str, Any] | None = None,
    output_ref: str = "",
    prompt_ref: str = "",
    on_accept: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute one default specialist and return a JSON-ready result.

    ``decision`` is recorded as metadata only; dispatch
    validity is established by the Director protocol before this boundary.
    ``context['output_ref']`` (or the explicit argument) is the sole authority
    for the required deliverable path. ``input_refs`` provides suggested reading paths. The specialist may
    discover additional relevant files in the problem directory.
    """

    normalized_agent = str(agent_id or "").strip()
    try:
        spec = specialist_spec(normalized_agent)
    except ValueError as exc:
        return SpecialistAdapterResult(
            agent_id=normalized_agent,
            status="INTERFACE_FAILED",
            failure_class="HOST_INTERFACE",
            reason_code="specialist_id_invalid",
            diagnostics_ref="",
        ).to_json() | {"message": str(exc)}

    root = Path(workdir)
    metadata = {
        "agent_id": normalized_agent,
        "round_index": int(round_index or 1),
        "task_id": str((context or {}).get("task_id") or f"round-{int(round_index or 1):04d}"),
        "prompt_ref": prompt_ref,
        "target_contract_id": str((context or {}).get("target_contract_id") or ""),
        "input_refs": list(_unique_strings(input_refs)),
    }
    if decision is not None:
        metadata["decision"] = _jsonable(decision)
    reading_refs = _unique_strings(input_refs)
    if normalized_agent == "survey":
        metadata["external_retrieval_enabled"] = bool(getattr(llm, "web_search", False))
        prompt += (
            "\n\nHost URL retrieval: "
            + ("enabled" if metadata["external_retrieval_enabled"] else "disabled")
            + ". Use local sources when disabled.\n"
        )

    requested_ref = str(output_ref or (context or {}).get("output_ref") or "").strip()
    if not requested_ref:
        return _write_failure(
            root,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_output_ref_missing",
            message="Host assignment did not provide an explicit output_ref",
            failure_class="HOST_INTERFACE",
            call_id="",
        )
    try:
        destination = _safe_output_path(root, requested_ref)
    except (OSError, ValueError) as exc:
        return _write_failure(
            root,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_output_path_invalid",
            message=str(exc),
            failure_class="HOST_INTERFACE",
            call_id="",
        )
    metadata["output_ref"] = requested_ref
    metadata["output_refs"] = [
        requested_ref, *(str(destination.with_name(name).relative_to(root)) for name in
                        ("revision.json", "memory_note.md",
                         "source_manifest.json", "catalogue_request.json"))
    ]
    metadata["output_dirs"] = [str((destination.parent / "findings").relative_to(root))]
    if normalized_agent == "survey":
        metadata["output_dirs"].append(str((destination.parent / "sources").relative_to(root)))
    if destination.exists():
        return _write_failure(
            root,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_output_already_exists",
            message=f"required output path already exists: {requested_ref}",
            failure_class="HOST_INTERFACE",
            call_id="",
        )

    call_name = f"director_led_{normalized_agent}_{max(1, int(round_index or 1)):02d}"
    try:
        handle = begin_artifact_call(
            llm,
            str(prompt or ""),
            workdir=root,
            call_name=call_name,
            role=spec.role,
            stage=SPECIALIST_STAGE,
            input_refs=reading_refs,
            metadata=metadata,
        )
    except (
        Exception
    ) as exc:  # transport boundary: turn provider exceptions into a result
        return _write_failure(
            root,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_transport_exception",
            message=f"{type(exc).__name__}: {exc}",
            failure_class="HOST_INTERFACE",
            call_id="",
        )

    if handle.transport_status != "SUCCESS":
        return _commit_failure_result(
            root,
            handle,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_transport_failed",
            message=(handle.error_code or "specialist transport failed"),
            failure_class="INTERFACE",
            metadata=metadata,
        )

    if normalized_agent == "planner":
        route_state = destination.parent / "route_context.json"
        patch = destination.parent / "revision.json"
        if patch.is_file() and route_state.is_file():
            from pace.runtime.text_revisions import apply_revision

            try:
                base_ref = json.loads(route_state.read_text())["route_ref"]
                apply_revision(
                    root,
                    patch.relative_to(root).as_posix(),
                    base_ref=base_ref,
                    output_ref=requested_ref,
                )
                metadata["revision_base_ref"] = base_ref
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _commit_failure_result(
                    root, handle, agent_id=normalized_agent, round_index=round_index,
                    reason_code="route_revision_invalid", message=str(exc),
                    failure_class="REPRESENTATION", metadata=metadata,
                )
    problem = validate_specialist_output(
        destination,
        allow_json=normalized_agent == "framer",
    )
    if problem:
        return _commit_failure_result(
            root,
            handle,
            agent_id=normalized_agent,
            round_index=round_index,
            reason_code="specialist_output_missing_or_invalid",
            message=problem,
            failure_class="INTERFACE",
            metadata=metadata,
        )

    if normalized_agent == "planner":
        from pace.runtime.output.workspace import _write_json_atomic

        _write_json_atomic(
            root / work_ref(root, "state/current_route.json"), {"ref": requested_ref}
        )
    response_ref = handle.response_ref
    result = SpecialistAdapterResult(
        agent_id=normalized_agent,
        status="SUCCEEDED",
        call_id=handle.call_id,
        deliverable_ref=requested_ref,
        response_ref=response_ref,
    ).to_json()
    published = [
        path.relative_to(root).as_posix()
        for path in sorted((destination.parent / "findings").glob("*.md"))
        if path.is_file()
    ]
    memory_draft = destination.with_name("memory_note.md")
    if memory_draft.is_file():
        published.append(memory_draft.relative_to(root).as_posix())
    if normalized_agent == "survey":
        result["external_retrieval_enabled"] = metadata["external_retrieval_enabled"]
        published.extend(
            path.relative_to(root).as_posix()
            for path in sorted((destination.parent / "sources").rglob("*"))
            if path.is_file()
        )
    request = destination.parent / "catalogue_request.json"
    if request.is_file():
        result["catalogue_request_ref"] = request.relative_to(root).as_posix()
    if published:
        result["published_refs"] = published
    # Persist the caller's validated handoff before publishing call success.
    # A later call-record/display failure must not cause a completed proof or
    # Survey to run again merely because the adapter did not return normally.
    if on_accept is not None:
        on_accept(result)
    commit_artifact_call_success(
        handle,
        workdir=root,
        artifact_ref=requested_ref,
        metadata={**metadata, "response_ref": response_ref},
    )
    return result


def validate_specialist_output(path: Path, *, allow_json: bool = False) -> str:
    """Return an interface diagnostic unless ``path`` is a real text artifact."""

    candidate = Path(path)
    if not candidate.is_file():
        return "model did not create the required specialist output file"
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        return f"cannot read specialist output as UTF-8 text: {type(exc).__name__}: {exc}"
    if not text:
        return "required specialist output file is empty"
    # A structured control response is not a mathematical deliverable.  Do
    # not accept it merely because the transport also returned response text.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, (dict, list)) and not allow_json:
        return "required specialist output cannot be a control JSON response"
    return ""


def _handler_for(
    *,
    llm: Any,
    workdir: Path,
    agent_id: str,
    output_ref_factory: Callable[[str, int], str] | None,
) -> Callable[..., dict[str, Any]]:
    def handler(**kwargs: Any) -> dict[str, Any]:
        round_index = max(1, int(kwargs.get("round_index") or 1))
        context = kwargs.get("context")
        context_map = context if isinstance(context, Mapping) else {}
        output_ref = (
            output_ref_factory(agent_id, round_index)
            if output_ref_factory is not None
            else str(context_map.get("output_ref") or "")
        )
        return run_default_specialist(
            llm=llm,
            workdir=workdir,
            prompt=str(kwargs.get("prompt") or ""),
            agent_id=str(kwargs.get("agent_id") or agent_id),
            input_refs=kwargs["input_refs"],
            round_index=round_index,
            decision=kwargs.get("decision"),
            context=context_map,
            output_ref=output_ref,
            prompt_ref=str(kwargs.get("prompt_ref") or ""),
        )

    return handler


def _commit_failure_result(
    root: Path,
    handle: ArtifactCallHandle,
    *,
    agent_id: str,
    round_index: int,
    reason_code: str,
    message: str,
    failure_class: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _write_failure(
        root,
        agent_id=agent_id,
        round_index=round_index,
        reason_code=reason_code,
        message=message,
        failure_class="HOST_INTERFACE" if failure_class == "INTERFACE" else failure_class,
        call_id=handle.call_id,
    )
    diagnostics_ref = str(failure.get("diagnostics_ref") or "")
    commit_artifact_call_failure(
        handle,
        workdir=root,
        failure_class=failure_class,
        retry_class=handle.retry_class or "NONE",
        diagnostics_ref=diagnostics_ref,
        metadata=dict(metadata),
    )
    return failure


def _write_failure(
    root: Path,
    *,
    agent_id: str,
    round_index: int,
    reason_code: str,
    message: str,
    failure_class: str,
    call_id: str,
) -> dict[str, Any]:
    # Keep adapter artifacts in the same four-digit round layout as the
    # Director-led engine.  A single canonical path is required for resume
    # and for promoting failure diagnostics into the next public snapshot.
    ref = f"{work_ref(root)}/stages/{max(1, int(round_index or 1)):04d}/specialist_failure.json"
    payload = {
        "schema_version": "director_led_specialist_failure/v1",
        "agent_id": agent_id,
        "status": "INTERFACE_FAILED" if failure_class == "HOST_INTERFACE" else "FAILED",
        "failure_class": failure_class,
        "reason_code": reason_code,
        "message": str(message),
        "call_id": call_id,
        "authority": "SEARCH_CONTEXT_ONLY",
    }
    path = root / ref
    _atomic_write_text(
        root,
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        label="specialist failure path",
    )
    return SpecialistAdapterResult(
        agent_id=agent_id,
        status="INTERFACE_FAILED",
        call_id=call_id,
        failure_class=failure_class,
        reason_code=reason_code,
        diagnostics_ref=ref,
    ).to_json()


def _atomic_write_text(
    root: Path,
    path: Path,
    content: str,
    *,
    label: str,
) -> None:
    """Atomically write one Host artifact without following lexical aliases."""

    root_path = Path(root)
    target = Path(path)
    if _path_has_symlink_component(root_path, target):
        raise ValueError(f"{label} must not contain a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _path_has_symlink_component(root_path, target):
        raise ValueError(f"{label} must not contain a symlink")
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=target.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_output_path(root: Path, output_ref: str) -> Path:
    raw = str(output_ref or "").strip().replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or ".." in path.parts
        or is_call_path(path)
        or path.name.startswith("private_")
    ):
        raise ValueError("specialist output ref must be a relative path under the run workspace")
    root_path = Path(root)
    lexical = root_path / raw
    if _path_has_symlink_component(root_path, lexical):
        raise ValueError("specialist output ref must not contain a symlink")
    destination = lexical.resolve()
    try:
        destination.relative_to(root_path.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("specialist output ref escapes the run workspace") from exc
    return destination


def _path_has_symlink_component(root: Path, path: Path) -> bool:
    """Return whether an output path uses a lexical symlink component."""

    root_path = Path(root)
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root_path)
    except ValueError:
        return True
    current = root_path
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_json"):
        try:
            return value.to_json()
        except Exception:
            pass
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "SPECIALIST_STAGE",
    "SpecialistAdapterResult",
    "build_default_specialist_handlers",
    "run_default_specialist",
    "validate_specialist_output",
]
