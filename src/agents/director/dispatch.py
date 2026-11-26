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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.agents.prompt_composer import compose_prompt
from pace.artifacts.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    is_call_path,
    path_has_symlink_component,
    work_ref,
)
from pace.contracts.research.memory import binding as memory_binding
from pace.contracts.research.memory import (
    check_binding,
    enabled,
    repair_route_references,
)
from pace.contracts.research.director_protocol import (
    DISPATCHABLE_AGENT_IDS,
    specialist_spec,
)
from pace.runtime.prompts import render_prompt_template
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
    ``work/<current-turn>/roundN``.
    """

    root = Path(workdir)
    handlers: dict[str, Callable[..., dict[str, Any]]] = {}
    for agent_id in sorted(DISPATCHABLE_AGENT_IDS):
        if agent_id == "rci":
            from pace.agents.rci.assignment import build_rci_handler

            handlers[agent_id] = build_rci_handler(llm=llm, workdir=root)
            continue
        handlers[agent_id] = _handler_for(
            llm=llm,
            workdir=root,
            agent_id=agent_id,
            output_ref_factory=output_ref_factory,
        )
    return handlers


def specialist_module(agent_id: str):
    from pace.agents import framer, planner, prover, research, survey

    return {
        "framer": framer,
        "planner": planner,
        "survey": survey,
        "research": research,
        "prover": prover,
    }[agent_id]


def build_specialist_prompt_assembly(
    *, workdir, decision, target_contract_id, output_ref, manifest_ref
):
    return specialist_module(decision.agent_id).build_prompt(
        workdir=workdir,
        decision=decision,
        target_contract_id=target_contract_id,
        output_ref=output_ref,
        manifest_ref=manifest_ref,
    )


def build_default_composer() -> Callable[..., Any]:
    """Return the production Prompt Composer adapter for Director assignments."""

    def compose(**kwargs: Any) -> Any:
        return compose_prompt(
            workdir=Path(kwargs["workdir"]),
            assembly=kwargs["assembly"],
            input_refs=kwargs["input_refs"],
        )

    return compose


def run_specialist(
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
        "task_id": str(
            (context or {}).get("task_id") or f"round-{int(round_index or 1):04d}"
        ),
        "prompt_ref": prompt_ref,
        "target_contract_id": str((context or {}).get("target_contract_id") or ""),
        "input_refs": list(_unique_strings(input_refs)),
    }
    if decision is not None:
        metadata["decision"] = _jsonable(decision)
    reading_refs = _unique_strings(input_refs)
    if normalized_agent == "survey":
        metadata["external_retrieval_enabled"] = bool(getattr(llm, "web_search", False))
        from pace.agents.survey import retrieval_instruction

        prompt += retrieval_instruction(
            root, enabled=metadata["external_retrieval_enabled"]
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
    from pace.agents.director.context import (
        route_base,
        task_context,
        validate_research_delivery,
    )

    frozen = task_context(root, requested_ref)
    metadata["archive_refs"] = frozen.get("archive_refs", [])
    sidecars = {
        "planner": ("route_relations.json", "revision.json", "memory_note.md"),
        "survey": ("source_manifest.json", "memory_note.md"),
        "framer": ("memory_note.md",),
    }
    metadata["output_refs"] = [
        requested_ref,
        *(str(Path(requested_ref).with_name(name)) for name in sidecars.get(normalized_agent, ())),
    ]
    metadata["output_dirs"] = [str((destination.parent / "findings").relative_to(root))]
    if normalized_agent == "survey":
        metadata["output_dirs"].append(
            str((destination.parent / "sources").relative_to(root))
        )
    author_delivery = destination.with_name("author_delivery.json")
    if destination.exists() and not author_delivery.is_file():
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
    memory_enabled = enabled(root)
    base_ref = route_base(root, requested_ref) if normalized_agent == "planner" else ""
    patch = destination.with_name("revision.json")
    saved_revision = None
    rendered_revision = None

    def checkpoint_author(handle: ArtifactCallHandle) -> None:
        nonlocal saved_revision, rendered_revision
        if not validate_specialist_output(
            destination, allow_json=normalized_agent == "framer"
        ):
            delivered = {"manuscript": memory_binding(root, requested_ref)}
        elif base_ref and patch.is_file() and not destination.exists():
            from pace.runtime.text_revisions import render_revision

            patch_ref = patch.relative_to(root).as_posix()
            try:
                version = memory_binding(root, patch_ref)
                rendered_revision = render_revision(root, patch_ref, base_ref=base_ref)
            except (OSError, ValueError, KeyError, TypeError):
                return  # The ordinary Planner validation reports the invalid patch.
            saved_revision = version
            delivered = {"revision": version}
        else:
            return
        _write_json_atomic(author_delivery, {"handle": asdict(handle), **delivered})

    try:
        if author_delivery.is_file():
            saved = json.loads(author_delivery.read_text())
            saved_revision = saved.get("revision")
            delivery = saved_revision or saved["manuscript"]
            expected = (
                patch.relative_to(root).as_posix() if saved_revision else requested_ref
            )
            if delivery["ref"] != expected:
                raise ValueError("saved author delivery belongs to another task")
            check_binding(root, delivery)
            handle = ArtifactCallHandle(**saved["handle"])
        else:
            handle = begin_artifact_call(
                llm,
                str(prompt or ""),
                workdir=root,
                call_name=call_name,
                role=spec.role,
                stage=SPECIALIST_STAGE,
                input_refs=reading_refs,
                metadata=metadata,
                on_transport_success=checkpoint_author if memory_enabled else None,
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
        if saved_revision or (
            patch.is_file() and base_ref and not destination.is_file()
        ):
            from pace.runtime.text_revisions import render_revision

            try:
                patch_ref = patch.relative_to(root).as_posix()
                if not base_ref or (root / base_ref).resolve() == destination.resolve():
                    raise ValueError("revision requires a separate frozen base")
                rendered = (
                    rendered_revision
                    if rendered_revision is not None
                    else render_revision(root, patch_ref, base_ref=base_ref)
                )
                if not destination.exists():
                    _write_text_atomic(destination, rendered)
                elif destination.read_text(encoding="utf-8") != rendered:
                    raise ValueError(
                        "materialized Planner route differs from its saved revision"
                    )
                relations = destination.with_name("route_relations.json")
                if (
                    frozen
                    and not relations.exists()
                    and json.loads(patch.read_text()).get("relations_unchanged") is True
                ):
                    _write_json_atomic(
                        relations,
                        {
                            "schema_version": "route_relations/v1",
                            "change_ref": requested_ref,
                            "obligations": [],
                        },
                    )
                metadata["revision_base_ref"] = base_ref
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _commit_failure_result(
                    root,
                    handle,
                    agent_id=normalized_agent,
                    round_index=round_index,
                    reason_code="route_revision_invalid",
                    message=str(exc),
                    failure_class="REPRESENTATION",
                    metadata=metadata,
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

    if memory_enabled and (saved_revision is not None or not author_delivery.is_file()):
        _write_json_atomic(
            author_delivery,
            {"handle": asdict(handle), "manuscript": memory_binding(root, requested_ref)},
        )
    if normalized_agent == "planner" and frozen:
        result_stub = {"agent_id": normalized_agent, "deliverable_ref": requested_ref}
        assignment_ref = str(
            destination.parent.relative_to(root) / "assignment/task.json"
        )
        relations_ref = str(Path(requested_ref).with_name("route_relations.json"))
        try:
            repair_route_references(root, relations_ref)
            validate_research_delivery(
                root, result_stub, assignment_ref
            )
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # One bounded correction of sidecars; never repeat the mathematical author.
            repair_refs = [relations_ref]
            repair_metadata = {k: v for k, v in metadata.items() if k != "prompt_ref"}
            repair_archive = [
                ref
                for ref in _unique_strings((*frozen["required_refs"], frozen["route_context"]["ref"]))
                if ref not in (requested_ref, assignment_ref, relations_ref)
            ]
            repair = begin_artifact_call(
                llm,
                render_prompt_template(
                    "shared/handoff_correction.md",
                    workdir=root,
                    repair_refs=repair_refs,
                    manuscript_ref=requested_ref,
                    diagnostics=str(exc),
                    delivery=specialist_module(
                        normalized_agent
                    ).build_delivery_instructions(root, requested_ref),
                ),
                workdir=root,
                call_name=call_name + "_handoff_correction",
                role="research_handoff_correction",
                stage=SPECIALIST_STAGE,
                input_refs=(requested_ref, assignment_ref),
                metadata={
                    **repair_metadata,
                    "parent_call_id": handle.call_id,
                    "read_scope": "declared",
                    "archive_refs": repair_archive,
                    "protected_input_refs": reading_refs,
                    "output_refs": repair_refs,
                    "output_dirs": [],
                },
            )
            try:
                if repair.transport_status != "SUCCESS":
                    raise ValueError(repair.error_code)
                check_binding(
                    root, json.loads(author_delivery.read_text())["manuscript"]
                )
                repair_route_references(root, relations_ref)
                validate_research_delivery(
                    root, result_stub, assignment_ref
                )
            except (ValueError, OSError, KeyError, TypeError) as repair_error:
                return _commit_failure_result(
                    root,
                    repair,
                    agent_id=normalized_agent,
                    round_index=round_index,
                    reason_code="research_handoff_invalid",
                    message=str(repair_error),
                    failure_class="REPRESENTATION",
                    metadata=metadata,
                )
            commit_artifact_call_success(
                repair, workdir=root, artifact_ref=requested_ref, metadata=metadata
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
        metadata=metadata,
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
        return (
            f"cannot read specialist output as UTF-8 text: {type(exc).__name__}: {exc}"
        )
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
        return specialist_module(agent_id).run(
            llm=llm,
            workdir=workdir,
            prompt=str(kwargs.get("prompt") or ""),
            input_refs=kwargs["input_refs"],
            round_index=round_index,
            decision=kwargs.get("decision"),
            context=context_map,
            output_ref=output_ref,
            prompt_ref=str(kwargs.get("prompt_ref") or ""),
            on_accept=kwargs.get("on_accept"),
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
        failure_class="HOST_INTERFACE"
        if failure_class == "INTERFACE"
        else failure_class,
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
    # Keep adapter artifacts in the same roundN layout as the
    # Director-led engine.  A single canonical path is required for resume
    # and for promoting failure diagnostics into the next public snapshot.
    ref = (
        f"{work_ref(root)}/round{max(1, int(round_index or 1))}/specialist_failure.json"
    )
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
    if path_has_symlink_component(root, path):
        raise ValueError("specialist failure path must not contain a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_has_symlink_component(root, path):
        raise ValueError("specialist failure path must not contain a symlink")
    _write_json_atomic(path, payload)
    return SpecialistAdapterResult(
        agent_id=agent_id,
        status="INTERFACE_FAILED",
        call_id=call_id,
        failure_class=failure_class,
        reason_code=reason_code,
        diagnostics_ref=ref,
    ).to_json()


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
        raise ValueError(
            "specialist output ref must be a relative path under the run workspace"
        )
    root_path = Path(root)
    lexical = root_path / raw
    if path_has_symlink_component(root_path, lexical):
        raise ValueError("specialist output ref must not contain a symlink")
    destination = lexical.resolve()
    try:
        destination.relative_to(root_path.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("specialist output ref escapes the run workspace") from exc
    return destination


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
    "run_specialist",
    "validate_specialist_output",
]
