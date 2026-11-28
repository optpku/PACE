"""Host-owned protocol for one dynamic Research Director decision.

The Director is a search coordinator.  This module contains only the small
machine-owned invariants needed to turn its structured response into a Host
dispatch or a search stop.  It deliberately does not inspect mathematical
claims, select a proof verdict, or execute a specialist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.runtime.schema import STRING, STRING_LIST, closed_object

DIRECTOR_PROTOCOL_SCHEMA_VERSION = "research_director_decision/v2"
MEMORY_DIRECTOR_SCHEMA_VERSION = "research_director_decision/v3"
MEMORY_DIRECTOR_FIELDS = {
    "obligation_ids",
    "branch_id",
    "archive_refs",
    "next_step_kind",
    "change_refs",
}
NEXT_STEP_KINDS = {"INITIAL_PLAN", "CONTINUE", "APPLY_EXISTING", "REPLAN", "REASSESS"}
DIRECTOR_ACTIONS = frozenset({"DISPATCH", "STOP"})
DIRECTOR_STOP_REASONS = frozenset({"BUDGET_EXHAUSTED", "NO_ACTIONABLE_TASK"})
MAX_SURVEY_CALLS = 4
DISPATCHABLE_AGENT_IDS = frozenset(
    {"framer", "planner", "survey", "rci", "research", "prover"}
)
DIRECTOR_REQUIRED_FIELDS = frozenset({"action"})
DIRECTOR_FIELDS = DIRECTOR_REQUIRED_FIELDS | {
    "agent_id",
    "prompt_brief",
    "input_refs",
    "rationale",
    "parallel_tasks",
    "research_operation",
    "research_problem_ref",
}


class DirectorAction(str, Enum):
    """The only scheduling choices owned by the Director."""

    DISPATCH = "DISPATCH"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class SpecialistSpec:
    """Static Host metadata for a dispatchable specialist.

    The registry is intentionally declarative.  The Host binds an entry to
    the concrete specialist implementation and owns execution of that
    implementation.
    """

    agent_id: str
    role: str

    def __post_init__(self) -> None:
        if self.agent_id not in DISPATCHABLE_AGENT_IDS:
            raise ValueError(f"unknown dispatchable specialist: {self.agent_id}")
        if not self.role.strip():
            raise ValueError("specialist metadata requires role")


# Auditor and Kernel are intentionally absent: they are fixed verification
# boundaries and cannot be scheduled as ordinary research specialists.
SPECIALIST_REGISTRY: Mapping[str, SpecialistSpec] = {
    "framer": SpecialistSpec("framer", "DIRECTOR_LED_FRAMER"),
    "planner": SpecialistSpec("planner", "PLANNER"),
    "survey": SpecialistSpec("survey", "SURVEY"),
    "rci": SpecialistSpec("rci", "RCI"),
    "research": SpecialistSpec("research", "RESEARCH"),
    "prover": SpecialistSpec("prover", "WHOLE_PROVER"),
}


@dataclass(frozen=True, slots=True)
class DirectorDecision:
    """A validated, Host-actionable Director response."""

    action: DirectorAction
    agent_id: str
    prompt_brief: str
    input_refs: tuple[str, ...]
    rationale: str
    parallel_tasks: tuple[DirectorDecision, ...] = ()
    research_operation: str = ""
    research_problem_ref: str = ""
    obligation_ids: tuple[str, ...] = ()
    branch_id: str = ""
    archive_refs: tuple[str, ...] = ()
    next_step_kind: str = ""
    change_refs: tuple[str, ...] = ()
    stop_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        value = {
            "action": self.action.value,
            "agent_id": self.agent_id,
            "prompt_brief": self.prompt_brief,
            "input_refs": list(self.input_refs),
            "rationale": self.rationale,
            "research_operation": self.research_operation,
            "research_problem_ref": self.research_problem_ref,
            "parallel_tasks": [
                {k: v for k, v in task.to_json().items() if k != "parallel_tasks"}
                for task in self.parallel_tasks
            ],
        }
        if self.next_step_kind:
            value.update(
                obligation_ids=list(self.obligation_ids),
                branch_id=self.branch_id,
                archive_refs=list(self.archive_refs),
                next_step_kind=self.next_step_kind,
                change_refs=list(self.change_refs),
            )
        if self.stop_reason:
            value["stop_reason"] = self.stop_reason
        return value


class DirectorProtocolError(ValueError):
    """Raised when a Director response violates the Host protocol."""

    def __init__(
        self, message: str, *, field: str = "", related_fields: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message)
        self.field = field
        self.related_fields = related_fields

    def for_companion(self, index: int) -> "DirectorProtocolError":
        prefix = f"parallel_tasks/{index}"
        return DirectorProtocolError(
            str(self),
            field=f"{prefix}/{self.field}".rstrip("/"),
            related_fields=tuple(f"{prefix}/{field}" for field in self.related_fields),
        )


def director_decision_schema(
    *, survey_calls_remaining: int = MAX_SURVEY_CALLS, memory_enabled: bool = False
) -> dict[str, Any]:
    """Describe scheduling fields; input paths are reading suggestions."""

    task_fields = {
        "action": {**STRING, "enum": ["DISPATCH"]},
        "agent_id": {
            **STRING,
            "enum": sorted(
                DISPATCHABLE_AGENT_IDS
                - ({"survey"} if survey_calls_remaining < 2 else set())
            ),
        },
        "prompt_brief": STRING,
        "input_refs": STRING_LIST,
        "rationale": STRING,
        "research_operation": {
            **STRING,
            "enum": ["", "NEW_PROBLEM", "CONTINUE_PROBLEM"],
        },
        "research_problem_ref": {
            **STRING,
            "description": "Exact existing run-relative question file from input_refs, without prose or a Markdown link; empty when unused.",
        },
    }
    schema = closed_object(
        {
            "action": {**STRING, "enum": sorted(DIRECTOR_ACTIONS)},
            "stop_reason": {**STRING, "enum": ["", *sorted(DIRECTOR_STOP_REASONS)]},
            "agent_id": {
                **STRING,
                "enum": [
                    "",
                    *sorted(
                        DISPATCHABLE_AGENT_IDS
                        - ({"survey"} if survey_calls_remaining == 0 else set())
                    ),
                ],
            },
            "prompt_brief": STRING,
            "input_refs": STRING_LIST,
            "rationale": STRING,
            "research_operation": {
                **STRING,
                "enum": ["", "NEW_PROBLEM", "CONTINUE_PROBLEM"],
            },
            "research_problem_ref": task_fields["research_problem_ref"],
            "parallel_tasks": {
                "type": "array",
                "maxItems": 1,
                "items": closed_object(
                    task_fields, required=("action", "agent_id", "prompt_brief")
                ),
            },
        },
        required=("action",),
    )

    if memory_enabled:
        fields = {
            "obligation_ids": STRING_LIST,
            "branch_id": STRING,
            "archive_refs": STRING_LIST,
            "next_step_kind": {**STRING, "enum": ["", *sorted(NEXT_STEP_KINDS)]},
            "change_refs": STRING_LIST,
        }
        schema["properties"].update(fields)
        companion_fields = schema["properties"]["parallel_tasks"]["items"]["properties"]
        companion_fields.update(fields)
        companion_fields["next_step_kind"] = {
            **fields["next_step_kind"],
            "description": "Set this companion's own purpose. Planner must choose INITIAL_PLAN or REPLAN "
            "explicitly. For other roles an omitted or empty purpose defaults to CONTINUE.",
        }
    return schema


def dispatchable_agent_ids() -> tuple[str, ...]:
    """Return deterministic registry order for Host dispatch validation."""

    return tuple(SPECIALIST_REGISTRY)


def specialist_spec(agent_id: str) -> SpecialistSpec:
    """Resolve one dispatchable specialist or raise a protocol error."""

    key = str(agent_id or "")
    try:
        return SPECIALIST_REGISTRY[key]
    except KeyError as exc:
        raise DirectorProtocolError(
            f"DISPATCH agent_id must be one of {sorted(DISPATCHABLE_AGENT_IDS)}",
            field="agent_id",
        ) from exc


def parse_director_decision(
    payload: Mapping[str, Any], *, errors: list[DirectorProtocolError] | None = None
) -> DirectorDecision:
    """Parse one scheduling response; agents choose mathematical guidance."""

    if not isinstance(payload, Mapping):
        raise DirectorProtocolError("director decision must be a JSON object")
    allowed_fields = DIRECTOR_FIELDS | MEMORY_DIRECTOR_FIELDS | {"stop_reason"}
    missing = sorted(DIRECTOR_REQUIRED_FIELDS.difference(payload))
    extra = sorted(set(payload).difference(allowed_fields))
    if missing or extra:
        invalid = (*missing, *extra)
        exc = DirectorProtocolError(
            f"director decision fields are closed (missing {missing}, unexpected {extra})",
            field=invalid[0],
            related_fields=invalid[1:],
        )
        if errors is None:
            raise exc
        errors.append(exc)

    action_value = payload.get("action")
    if not isinstance(action_value, str) or action_value not in DIRECTOR_ACTIONS:
        raise DirectorProtocolError("action must be DISPATCH or STOP", field="action")
    action = DirectorAction(action_value)

    def read(field, convert, default):
        try:
            return convert(payload.get(field, default), field)
        except DirectorProtocolError as exc:
            if errors is None:
                raise
            errors.append(exc)
            return convert(default, field)

    companions = payload.get("parallel_tasks", [])
    if not isinstance(companions, list) or len(companions) > 1:
        exc = DirectorProtocolError(
            "parallel_tasks permits at most one independent companion",
            field="parallel_tasks",
        )
        if errors is None:
            raise exc
        errors.append(exc)
        companions = []
    tasks = []
    for index, task in enumerate(companions):
        try:
            if errors is not None and isinstance(task, Mapping):
                # Diagnose an extra or missing field without losing unrelated
                # controls in this companion's otherwise readable receipt.
                unexpected = set(task) - (
                    (DIRECTOR_FIELDS | MEMORY_DIRECTOR_FIELDS) - {"parallel_tasks"}
                )
                for field in sorted(unexpected):
                    errors.append(
                        DirectorProtocolError(
                            "unexpected field in a parallel dispatch",
                            field=f"parallel_tasks/{index}/{field}",
                        )
                    )
                task = {
                    key: value for key, value in task.items() if key not in unexpected
                }
            elif (
                not isinstance(task, Mapping)
                or not DIRECTOR_REQUIRED_FIELDS <= set(task)
                or set(task) - (DIRECTOR_FIELDS | MEMORY_DIRECTOR_FIELDS)
                or "parallel_tasks" in task
            ):
                raise DirectorProtocolError(
                    "parallel task must be a single dispatch, without nested tasks"
                )
            task_errors = [] if errors is not None else None
            parsed = parse_director_decision(task, errors=task_errors)
            if parsed.action is not DirectorAction.DISPATCH:
                raise DirectorProtocolError(
                    "parallel task must DISPATCH", field="action"
                )
        except DirectorProtocolError as exc:
            if errors is None:
                raise exc.for_companion(index) from exc
            errors.append(exc.for_companion(index))
            continue
        if errors is not None:
            errors.extend(exc.for_companion(index) for exc in task_errors)
        tasks.append(parsed)

    decision = DirectorDecision(
        action=action,
        agent_id=read("agent_id", _text, ""),
        prompt_brief=read("prompt_brief", _text, ""),
        input_refs=read("input_refs", _string_tuple, []),
        rationale=read("rationale", _text, ""),
        parallel_tasks=tuple(tasks),
        obligation_ids=read("obligation_ids", _string_tuple, []),
        branch_id=read("branch_id", _text, ""),
        archive_refs=read("archive_refs", _string_tuple, []),
        next_step_kind=read("next_step_kind", _text, ""),
        change_refs=read("change_refs", _string_tuple, []),
        stop_reason=read("stop_reason", _text, ""),
        research_operation=read("research_operation", _text, ""),
        research_problem_ref=read("research_problem_ref", _text, ""),
    )
    issues = _decision_errors(decision)
    if errors is None:
        error = next(issues, None)
        if error is not None:
            raise error
    else:
        errors.extend(issues)
    return decision


def validate_director_decision(decision: DirectorDecision) -> None:
    """Check only Host-owned scheduling invariants."""
    if not isinstance(decision, DirectorDecision):
        raise DirectorProtocolError("decision must be a DirectorDecision")
    error = next(_decision_errors(decision), None)
    if error is not None:
        raise error
    for index, task in enumerate(decision.parallel_tasks):
        try:
            validate_director_decision(task)
        except DirectorProtocolError as exc:
            raise exc.for_companion(index) from exc


def _decision_errors(decision: DirectorDecision):
    if decision.action is DirectorAction.STOP:
        if decision.stop_reason and decision.stop_reason not in DIRECTOR_STOP_REASONS:
            yield DirectorProtocolError("unsupported stop_reason", field="stop_reason")
        for field in (
            "agent_id",
            "prompt_brief",
            "input_refs",
            "parallel_tasks",
            "obligation_ids",
            "branch_id",
            "archive_refs",
            "next_step_kind",
            "change_refs",
            "research_operation",
            "research_problem_ref",
        ):
            if getattr(decision, field):
                yield DirectorProtocolError(
                    "STOP must not dispatch an agent or select inputs", field=field
                )
        return

    if decision.action is not DirectorAction.DISPATCH:
        yield DirectorProtocolError("unsupported Director action", field="action")
        return

    if decision.next_step_kind:
        if decision.next_step_kind not in NEXT_STEP_KINDS:
            yield DirectorProtocolError(
                "unsupported next_step_kind", field="next_step_kind"
            )
        if not decision.obligation_ids:
            yield DirectorProtocolError(
                "memory dispatch requires obligation_ids", field="obligation_ids"
            )
        if not decision.branch_id.strip():
            yield DirectorProtocolError(
                "memory dispatch requires branch_id", field="branch_id"
            )
        if (
            decision.next_step_kind in {"INITIAL_PLAN", "REPLAN"}
            and decision.agent_id != "planner"
        ):
            yield DirectorProtocolError(
                "planning purpose requires Planner", field="next_step_kind"
            )
        if decision.agent_id == "planner" and decision.next_step_kind not in {
            "INITIAL_PLAN",
            "REPLAN",
        }:
            yield DirectorProtocolError(
                "Planner requires INITIAL_PLAN or REPLAN", field="next_step_kind"
            )
        if decision.next_step_kind == "REPLAN" and not decision.change_refs:
            yield DirectorProtocolError(
                "REPLAN requires concrete change_refs", field="change_refs"
            )

    if len(decision.parallel_tasks) > 1:
        yield DirectorProtocolError(
            "at most two tasks may run concurrently", field="parallel_tasks"
        )
    for index, companion in enumerate(decision.parallel_tasks):
        if companion.action is not DirectorAction.DISPATCH or companion.parallel_tasks:
            yield DirectorProtocolError(
                "parallel tasks must be non-nested dispatches",
                field=f"parallel_tasks/{index}",
            )
        if decision.agent_id == companion.agent_id == "planner":
            yield DirectorProtocolError(
                "a batch can have at most one Planner", field=f"parallel_tasks/{index}"
            )
    try:
        specialist_spec(decision.agent_id)
    except DirectorProtocolError as exc:
        yield exc
    if decision.agent_id == "rci":
        if decision.research_operation not in {"NEW_PROBLEM", "CONTINUE_PROBLEM"}:
            yield DirectorProtocolError(
                "RCI requires an explicit research operation",
                field="research_operation",
            )
        if (
            decision.research_operation == "CONTINUE_PROBLEM"
            and not decision.research_problem_ref
        ):
            yield DirectorProtocolError(
                "continued RCI requires research_problem_ref",
                field="research_problem_ref",
            )
    else:
        for field in ("research_operation", "research_problem_ref"):
            if getattr(decision, field):
                yield DirectorProtocolError(
                    "research operation fields are only for RCI", field=field
                )
    if decision.stop_reason:
        yield DirectorProtocolError(
            "DISPATCH must not carry a stop_reason", field="stop_reason"
        )
    if not decision.prompt_brief.strip():
        yield DirectorProtocolError(
            "DISPATCH requires a non-empty prompt_brief", field="prompt_brief"
        )


def validate_director_stop(
    decision: DirectorDecision,
    *,
    proof_states_remaining: int | None,
    research_rounds_remaining: int | None,
) -> None:
    if decision.action is not DirectorAction.STOP:
        return
    if decision.stop_reason == "BUDGET_EXHAUSTED" and not (
        proof_states_remaining == 0 or research_rounds_remaining == 0
    ):
        raise DirectorProtocolError(
            "Host budget is not exhausted; choose a bounded DISPATCH using the committed evidence.",
            field="action",
        )
    missing = tuple(
        field
        for field in ("stop_reason", "rationale")
        if not getattr(decision, field).strip()
    )
    if missing:
        raise DirectorProtocolError(
            "STOP requires a structured stop_reason and a concrete rationale",
            field=missing[0],
            related_fields=missing[1:],
        )


def validate_director_reference_bindings(
    decision: DirectorDecision, workdir: Path
) -> None:
    """Require continuation pointers to be declared, existing workspace files.

    Reading suggestions are advisory; the continuation pointer is executable
    input and must be checked before committing a dispatch.
    """
    error = next(director_reference_errors(decision, workdir), None)
    if error is not None:
        raise error


def director_reference_errors(
    decision: DirectorDecision, workdir: Path, *, require_bindings: bool = False
):
    from pace.artifacts.workspace import is_call_path, path_has_symlink_component
    from pace.contracts.research.memory import binding, check_source_ownership

    root = Path(workdir).resolve()
    checked = {}
    for index, task in enumerate((decision, *decision.parallel_tasks)):
        prefix = "" if index == 0 else f"parallel_tasks/{index - 1}/"
        if require_bindings or task.next_step_kind:
            for field in ("input_refs", "archive_refs", "change_refs"):
                for ref in getattr(task, field):
                    if ref not in checked:
                        try:
                            check_source_ownership(root, binding(root, ref))
                        except (OSError, ValueError) as exc:
                            checked[ref] = str(exc)
                        else:
                            checked[ref] = ""
                    if checked[ref]:
                        yield DirectorProtocolError(checked[ref], field=prefix + field)
        ref = task.research_problem_ref
        if not ref:
            continue
        path = Path(ref)
        field = prefix + "research_problem_ref"
        if (
            ref != ref.strip()
            or path.is_absolute()
            or PureWindowsPath(ref).drive
            or "\\" in ref
            or any(part in {".", "..", ""} for part in ref.split("/"))
            or is_call_path(path)
            or path.name.startswith("private_")
        ):
            yield DirectorProtocolError(
                "research_problem_ref must be an exact run-relative question file",
                field=field,
                related_fields=(prefix + "input_refs",),
            )
        elif (
            path_has_symlink_component(root, root / path)
            or not (root / path).resolve().is_relative_to(root)
            or not (root / path).is_file()
        ):
            yield DirectorProtocolError(
                f"research_problem_ref does not name an existing workspace file: {ref}",
                field=field,
                related_fields=(prefix + "input_refs",),
            )
        elif ref not in task.input_refs:
            yield DirectorProtocolError(
                "include the exact research_problem_ref in input_refs",
                field=prefix + "input_refs",
            )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DirectorProtocolError(f"{field} must be a string", field=field)
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DirectorProtocolError(f"{field} must be a list of strings", field=field)
    if any(not item.strip() for item in value):
        raise DirectorProtocolError(
            f"{field} entries must be non-empty strings", field=field
        )
    # Reading suggestions preserve literal paths and first occurrence.
    return tuple(dict.fromkeys(value))


__all__ = [
    "DIRECTOR_ACTIONS",
    "DIRECTOR_FIELDS",
    "DIRECTOR_PROTOCOL_SCHEMA_VERSION",
    "DISPATCHABLE_AGENT_IDS",
    "SPECIALIST_REGISTRY",
    "DirectorAction",
    "DirectorDecision",
    "DirectorProtocolError",
    "SpecialistSpec",
    "director_decision_schema",
    "dispatchable_agent_ids",
    "parse_director_decision",
    "specialist_spec",
    "validate_director_decision",
    "validate_director_reference_bindings",
]
