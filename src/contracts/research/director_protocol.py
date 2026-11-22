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

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def director_decision_schema(
    *, survey_calls_remaining: int = 4, memory_enabled: bool = False
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


def parse_director_decision(payload: Mapping[str, Any]) -> DirectorDecision:
    """Parse one scheduling response; agents choose mathematical guidance."""

    if not isinstance(payload, Mapping):
        raise DirectorProtocolError("director decision must be a JSON object")
    allowed_fields = DIRECTOR_FIELDS | MEMORY_DIRECTOR_FIELDS | {"stop_reason"}
    if not DIRECTOR_REQUIRED_FIELDS.issubset(payload) or set(payload) - allowed_fields:
        missing = sorted(DIRECTOR_REQUIRED_FIELDS.difference(payload))
        extra = sorted(set(payload).difference(allowed_fields))
        details = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorProtocolError(
            "director decision fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )

    action_value = payload.get("action")
    if not isinstance(action_value, str) or action_value not in DIRECTOR_ACTIONS:
        raise DirectorProtocolError("action must be DISPATCH or STOP", field="action")
    action = DirectorAction(action_value)

    agent_id = _text(payload.get("agent_id", ""), "agent_id")
    prompt_brief = _text(payload.get("prompt_brief", ""), "prompt_brief")
    rationale = _text(payload.get("rationale", ""), "rationale")
    input_refs = _string_tuple(payload.get("input_refs", []), "input_refs")
    companions = payload.get("parallel_tasks", [])
    if not isinstance(companions, list) or len(companions) > 1:
        raise DirectorProtocolError(
            "parallel_tasks permits at most one independent companion"
        )
    tasks = []
    for index, task in enumerate(companions):
        if (
            not isinstance(task, Mapping)
            or not DIRECTOR_REQUIRED_FIELDS <= set(task)
            or set(task) - (DIRECTOR_FIELDS | MEMORY_DIRECTOR_FIELDS)
            or "parallel_tasks" in task
        ):
            raise DirectorProtocolError(
                "parallel task must be a single dispatch, without nested tasks"
            )
        try:
            parsed = parse_director_decision(task)
        except DirectorProtocolError as exc:
            raise DirectorProtocolError(
                str(exc), field=f"parallel_tasks/{index}/{exc.field}"
            ) from exc
        if parsed.action is not DirectorAction.DISPATCH:
            raise DirectorProtocolError("parallel task must DISPATCH")
        tasks.append(parsed)

    decision = DirectorDecision(
        action=action,
        agent_id=agent_id,
        prompt_brief=prompt_brief,
        input_refs=input_refs,
        rationale=rationale,
        parallel_tasks=tuple(tasks),
        obligation_ids=_string_tuple(
            payload.get("obligation_ids", []), "obligation_ids"
        ),
        branch_id=_text(payload.get("branch_id", ""), "branch_id"),
        archive_refs=_string_tuple(payload.get("archive_refs", []), "archive_refs"),
        next_step_kind=_text(payload.get("next_step_kind", ""), "next_step_kind"),
        change_refs=_string_tuple(payload.get("change_refs", []), "change_refs"),
        stop_reason=_text(payload.get("stop_reason", ""), "stop_reason"),
        research_operation=_text(
            payload.get("research_operation", ""), "research_operation"
        ),
        research_problem_ref=_text(
            payload.get("research_problem_ref", ""), "research_problem_ref"
        ),
    )
    validate_director_decision(decision)
    return decision


def validate_director_decision(decision: DirectorDecision) -> None:
    """Check only Host-owned scheduling invariants."""

    if not isinstance(decision, DirectorDecision):
        raise DirectorProtocolError("decision must be a DirectorDecision")
    if decision.next_step_kind:
        if decision.next_step_kind not in NEXT_STEP_KINDS:
            raise DirectorProtocolError(
                "unsupported next_step_kind", field="next_step_kind"
            )
        if not decision.obligation_ids or not decision.branch_id.strip():
            raise DirectorProtocolError(
                "memory dispatch requires obligation_ids and branch_id",
                field="obligation_ids",
            )
        if (
            decision.next_step_kind in {"INITIAL_PLAN", "REPLAN"}
            and decision.agent_id != "planner"
        ):
            raise DirectorProtocolError(
                "planning purpose requires Planner", field="next_step_kind"
            )
        if decision.agent_id == "planner" and decision.next_step_kind not in {
            "INITIAL_PLAN",
            "REPLAN",
        }:
            raise DirectorProtocolError(
                "Planner requires INITIAL_PLAN or REPLAN", field="next_step_kind"
            )
        if decision.next_step_kind == "REPLAN" and not decision.change_refs:
            raise DirectorProtocolError(
                "REPLAN requires concrete change_refs", field="change_refs"
            )
    if sum(t.agent_id == "planner" for t in (decision, *decision.parallel_tasks)) > 1:
        raise DirectorProtocolError("a batch can have at most one Planner")
    if decision.action is DirectorAction.DISPATCH:
        specialist_spec(decision.agent_id)
    if decision.agent_id == "rci" and decision.action is DirectorAction.DISPATCH:
        if decision.research_operation not in {"NEW_PROBLEM", "CONTINUE_PROBLEM"}:
            raise DirectorProtocolError(
                "RCI requires an explicit research operation",
                field="research_operation",
            )
        if (
            decision.research_operation == "CONTINUE_PROBLEM"
            and not decision.research_problem_ref
        ):
            raise DirectorProtocolError(
                "continued RCI requires research_problem_ref",
                field="research_problem_ref",
            )
    elif decision.research_operation or decision.research_problem_ref:
        raise DirectorProtocolError(
            "research operation fields are only for RCI; "
            "omit or clear both research_operation and research_problem_ref for other agents",
            field="research_operation"
            if decision.research_operation
            else "research_problem_ref",
        )
    if decision.action is DirectorAction.STOP:
        if decision.stop_reason and decision.stop_reason not in DIRECTOR_STOP_REASONS:
            raise DirectorProtocolError("unsupported stop_reason", field="stop_reason")
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
        ):
            if getattr(decision, field):
                raise DirectorProtocolError(
                    "STOP must not dispatch an agent or select inputs", field=field
                )
        return

    if decision.stop_reason:
        raise DirectorProtocolError(
            "DISPATCH must not carry a stop_reason", field="stop_reason"
        )
    if decision.action is not DirectorAction.DISPATCH:
        raise DirectorProtocolError("unsupported Director action", field="action")
    if len(decision.parallel_tasks) > 1:
        raise DirectorProtocolError("at most two tasks may run concurrently")
    for index, task in enumerate(decision.parallel_tasks):
        if task.action is not DirectorAction.DISPATCH or task.parallel_tasks:
            raise DirectorProtocolError("parallel tasks must be non-nested dispatches")
        try:
            validate_director_decision(task)
        except DirectorProtocolError as exc:
            raise DirectorProtocolError(
                str(exc), field=f"parallel_tasks/{index}/{exc.field}".rstrip("/")
            ) from exc
    if not decision.prompt_brief.strip():
        raise DirectorProtocolError(
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
    if not decision.stop_reason or not decision.rationale.strip():
        raise DirectorProtocolError(
            "STOP requires a structured stop_reason and a concrete rationale",
            field="action",
        )
    if decision.stop_reason == "BUDGET_EXHAUSTED" and not (
        proof_states_remaining == 0 or research_rounds_remaining == 0
    ):
        raise DirectorProtocolError(
            "Host budget is not exhausted; choose a bounded DISPATCH using the committed evidence.",
            field="action",
        )


def validate_director_reference_bindings(
    decision: DirectorDecision, workdir: Path
) -> None:
    """Require continuation pointers to be declared, existing workspace files.

    Reading suggestions are advisory; the continuation pointer is executable
    input and must be checked before committing a dispatch.
    """
    from pace.artifacts.workspace import is_call_path, path_has_symlink_component

    root = Path(workdir).resolve()
    for index, task in enumerate((decision, *decision.parallel_tasks)):
        if task.next_step_kind:
            from pace.contracts.research.memory import binding, check_source_ownership

            for item in (*task.input_refs, *task.archive_refs, *task.change_refs):
                check_source_ownership(root, binding(root, item))
        ref = task.research_problem_ref
        if not ref:
            continue
        path = Path(ref)
        field = (
            "research_problem_ref"
            if index == 0
            else f"parallel_tasks/{index - 1}/research_problem_ref"
        )
        if (
            ref not in task.input_refs
            or ref != ref.strip()
            or path.is_absolute()
            or PureWindowsPath(ref).drive
            or "\\" in ref
            or any(part in {".", ".."} for part in ref.split("/"))
            or is_call_path(path)
            or path.name.startswith("private_")
        ):
            raise DirectorProtocolError(
                "research_problem_ref must be an exact run-relative question file "
                "listed in input_refs; put question descriptions in prompt_brief",
                field=field,
            )
        candidate = root / path
        if (
            path_has_symlink_component(root, candidate)
            or not candidate.resolve().is_relative_to(root)
            or not candidate.is_file()
        ):
            raise DirectorProtocolError(
                f"research_problem_ref does not name an existing workspace file: {ref}",
                field=field,
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
